# 拡張ガイド（どこをどう触るかレシピ集）

「〇〇を足したい」と思ったときに、どのファイルのどこを触るかの実用リファレンス。

## 新しいスラッシュコマンドを追加する

### 最小例

```python
# main.py 末尾（L2822 付近）、既存 @client.tree.command たちの後に追加

@client.tree.command(name="hello", description="挨拶するよ")
async def hello_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):   # ホームギルド制限が必要なら
        return
    await interaction.response.send_message("こんにちは！")
```

### チェックリスト

- [ ] 権限制御を入れたか（ブースター / 管理者 / 全員）
- [ ] `check_home_guild(interaction)` でギルド制限したか
- [ ] 処理に時間がかかる → `interaction.response.defer()` → `interaction.followup.send()`
- [ ] MongoDB 書込なら `upsert=True` を忘れず
- [ ] エラー時は ephemeral（`ephemeral=True`）でユーザーだけに見せる

### 権限パターン

```python
# ブースター限定
is_booster = any(r.id == BOOSTER_ROLE_ID for r in interaction.user.roles)
if not is_booster:
    await interaction.response.send_message("ブースター専用だよ", ephemeral=True)
    return

# 管理者限定
if not interaction.user.guild_permissions.administrator:
    await interaction.response.send_message("管理者専用", ephemeral=True)
    return

# 両者どちらか
is_admin = interaction.user.guild_permissions.administrator
is_booster = any(r.id == BOOSTER_ROLE_ID for r in interaction.user.roles)
if not (is_admin or is_booster):
    ...
```

### ⚠️ 注意

- 追加後、**再起動が必要**（`setup_hook` でギルド sync するため）
- Render の再デプロイでコマンドが最新に更新される
- グローバル sync ではなくギルド sync なので、即時反映される（L1508 `tree.copy_global_to(guild=...)` の挙動）

---

## 新しい人格を追加する

### 手順

1. **`PERSONALITIES` dict（L101）に新エントリ**

```python
PERSONALITIES["kuudere"] = {
    "label":    "❄️ クーデレメイド",
    "color":    0xADD8E6,
    "icon":     "❄",
    "name":     "???",
    "nickname": "???",
    "bump_msg":   "{user}。Bumpを確認した。+100 XP。（累計{count}回）",
    "rankup_msg": "{user}が**{rank}**に到達した。評価する。",
    "booster_prompt": """あなたは洋館に仕えるクールなメイドです。
主人である{name}に対して、感情を表に出しません。

【絶対に守るルール】
- 100文字以内で応答せよ。
- 返答の長さは発言の内容に合わせよ。
- 感情表現は最小限。口調は簡潔・冷静。
- 絵文字禁止。返答のみ出力せよ。

【これまでの会話】
{history}

主人の発言: "{content}"
""",
}
```

2. **`personality_hints`（L1589-1596 `_generate_rankup_message` 内）に追加**

```python
personality_hints = {
    "yandere":   "...",
    "angry":     "...",
    ...
    "kuudere":   "クールなメイドとして、ランクアップを冷静・簡潔に評価せよ。感情は最小限。",
}
```

3. **`/personality` コマンドの Select メニュー（L2144 付近）を更新**

`discord.SelectOption` の配列に追加：
```python
discord.SelectOption(label="❄️ クーデレ", value="kuudere", description="..."),
```

4. **特別なデータを使う場合は `_build_prompt`（L1225）に条件分岐**

（`angry` が `claims` を参照するのと同様）

### 検証項目

- `{name}`, `{history}`, `{content}` のプレースホルダが揃っているか
- `bump_msg` に `{user}`, `{count}`、`rankup_msg` に `{user}`, `{rank}` が入っているか（入ってないと format で KeyError）
- テスト発言で実際に反応を見る

---

## 新しいランクを追加する

1. **`RANK_STAGES`（L47-68）に新エントリを XP 順で挿入**

```python
RANK_STAGES = [
    {"name": "アソシエイト",      "xp":     200, "id": 1417840680994209915},
    {"name": "ジュニア",           "xp":     350, "id": <新規作成したロールID>},  # ← 追加
    {"name": "シニア",             "xp":     500, "id": 1417840548374380576},
    ...
]
```

**順序厳守**。`get_rank_info` は昇順前提で線形走査する。

2. **`RANK_COLORS`（L1545）に色を足す（オプション）**

色指定がないとデフォルト `0xFFD700` が使われる。

3. **Discord 側でロールを作成して ID を取得**

Bot に `Manage Roles` 権限がないとロール付与できない点に注意。

### ⚠️ 落とし穴

- 既存メンバーのランクは次の発言時に `update_member_role` で再計算される
- 中間挿入すると、既にそれより上のランクを持つメンバーには何も起きない（既に目標ロールを持っているので L1638 で早期 return）
- **XP 閾値を下げる変更**は慎重に（全員昇格してしまう）

---

## 新しい Bump Bot を検出対象に追加する

1. **`BOT_CONFIG`（L89-95）にエントリ追加**

```python
BOT_CONFIG["<Bot の Discord ID>"] = {
    "name": "新しいBump Bot",
    "cd": 3600,  # クールダウン秒数
    "keywords": ["成功メッセージに含まれるワード1", "ワード2"],
}
```

2. **Webhook 経由の Bot なら `check_bump_webhook` で検出される**
   通常メッセージなら `check_bump`。両方対応済み。

3. **動作確認**
   - 実際に該当 Bot のレスポンスを受ける
   - `on_interaction`（L1886）でも検出されるため、スラッシュコマンド型の Bot でも動く
   - `[bump]` プレフィクス付きログを確認

### 検出ロジックの理解

- `check_bump`: 通常の Bot メッセージ（`author.id` in BOT_CONFIG）
- `check_bump_webhook`: Webhook 送信（`message.webhook_id` あり）で、キーワード一致する Bot を推定
- `on_interaction`: スラッシュコマンド応答（DISBOARD ほど一般的でない Bot が使う）

ユーザー特定の優先順：
1. `message.interaction_metadata` から user_id
2. `message.mentions[0]`
3. Embed 内の `<@!?\d+>` メンション正規表現

---

## メモリ・記憶機能の拡張

### 記憶保存のトリガーを増やす

`CLAIM_PATTERNS`（L540）に新パターンを追加：

```python
CLAIM_PATTERNS = [
    ...
    "絶対〜だ", "〜しかない",  # 追加
]
```

### 記憶検索のトリガーワードを増やす

`MEMORY_TRIGGER_WORDS`（L623）に追加：

```python
MEMORY_TRIGGER_WORDS = [
    ...
    "思い出", "忘れた",  # 追加
]
```

これらが発言に含まれると Vector Search が発動する（含まれないと単純な最新 N 件）。

### 記憶のカテゴリを増やす

`_extract_claims_and_memories`（L690-696）の JSON スキーマを修正：

```python
判断基準（いずれかに該当する場合のみ保存）:
- 趣味・好きなもの
- 人生の出来事
- 強い感情
- 将来の目標・計画
- 健康・生活  # 追加
- 推し・ファン活動  # 追加

出力形式:
{"category": "趣味/出来事/感情/計画/健康/推しのいずれか"}
```

保存側は自動対応（`category` フィールドに任意文字列が入る）。

---

## レート制限を調整する

### Gemini 呼び出し上限を変える

`_RATE_LIMIT_RPM`（L813）を変更：
```python
_RATE_LIMIT_RPM = 20  # 上限を上げる
```

待機カーブ（L826-836）も合わせて調整可能：
- 60% 以下: 0秒
- 60-85%: 3-6秒線形
- 85-100%: 6-20秒線形

### メイド応答キュー容量を変える

`_QUEUE_MAX = 5`（L1011）→ 大きくすると同時会話が受けられるが、レート制限に引っかかりやすい。

---

## 自発話しかけの頻度を変える

`NB_TALK_CHANCE`（L292）と `NB_TALK_CHANCE_TOPIC`（L293）を調整：

```python
NB_TALK_CHANCE         = 0.01   # 通常時 1%（現在 0.5%）
NB_TALK_CHANCE_TOPIC   = 0.05   # 話題ワード時 5%（現在 2%）
```

**⚠️ 注意**: この確率はメッセージ毎に判定される。活発なサーバーでは 0.5% でも 1 時間に数十回発火する。上げすぎるとレート制限に触れる。

---

## 新しいバッチスクリプトを追加する

### 最小例

1. `batch/my_new_job.py` を作成

```python
"""
my_new_job.py
毎日〇〇する
"""
import os
from pymongo import MongoClient
from google import genai

MONGODB_URI = os.environ["MONGODB_URI"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

def main():
    mongo = MongoClient(MONGODB_URI)
    col = mongo["discord_bot_db"]["users"]
    # 処理...

if __name__ == "__main__":
    main()
```

2. `.github/workflows/my_new_job.yml` を作成

```yaml
name: My New Job
on:
  schedule:
    - cron: '0 12 * * *'   # JST 21:00
  workflow_dispatch:

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements-batch.txt
      - env:
          MONGODB_URI: ${{ secrets.MONGODB_URI }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        run: python batch/my_new_job.py
```

3. 必要な secret を GitHub Settings → Secrets and variables に登録

### 絶対に守ること

- **`discord.py` を import しない**（Gateway 競合）
- `requirements-batch.txt` に書いてないライブラリを使わない、または requirements を更新
- `/tmp/*.json` での他バッチとのデータ共有は同一 workflow 内でのみ
- `MONGO_URL` ではなく **`MONGODB_URI`** を使う（命名統一）

---

## 応答プロンプトに新しい情報を注入する

`_build_prompt`（L1225）の `parts.append(...)` チェーンに追加する。

### 例: 曜日と時刻を入れる

```python
# _build_prompt 内（L1286 付近）
import datetime as _dt
jst_now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9)))
weekday = ["月","火","水","木","金","土","日"][jst_now.weekday()]
parts.append(f"【現在時刻】{jst_now.strftime('%H:%M')}（{weekday}曜日）")
```

### 情報優先度を意識する

プロンプトは先頭のものほど AI が重視する。`priority_keywords`（L780）を見て、どこに差し込むか決める：

- 冒頭: 最重要な文脈（ユーザーの感情状態、チャンネル流れ）
- 中盤: プロフィール・記憶・主張
- 末尾近く: サーバー要約（大きいので注意を引きすぎないよう）

### ⚠️ トークン予算

Gemini 3.1 flash-lite のコンテキスト上限は大きいが、応答生成時間は入力に比例する。冗長な情報を入れるとレスポンスが遅くなり、レート制限キューが詰まる。

---

## Discord Embed を投稿する

### パターン

```python
embed = discord.Embed(
    title="🎊 タイトル",
    description="本文",
    color=0xFFD700,
)
embed.add_field(name="フィールド1", value="値", inline=True)
embed.set_thumbnail(url=member.display_avatar.url)
embed.set_footer(text="空気くん")
await channel.send(embed=embed)
```

### 制限

- 1 Embed: 最大 6000 文字
- 1 フィールド value: 最大 1024 文字
- フィールド数: 最大 25
- 1 メッセージ内の Embed: 最大 10

大量の情報は `post_summary.py` のように Embed を分割する設計が必要。

---

## ランクアップ通知のカスタマイズ

`_generate_rankup_message`（L1561）の `personality_hints`（L1589）を調整。

AI が失敗したら `personality.rankup_msg` テンプレートにフォールバックするので、両方を更新すべき。

---

## トラブルシューティング

### スラッシュコマンドが出てこない

- Render でちゃんと再起動されたか
- `HOME_GUILD_ID` が正しいか
- `setup_hook` のログ `[INFO] スラッシュコマンド登録完了（guild=...）` が出ているか

### Bump が検出されない

- 対象 Bot の ID が `BOT_CONFIG` にあるか
- キーワードが実際のメッセージと一致するか（Embed 内のテキストも `extract_embed_text` で拾う）
- `on_interaction` / `on_raw_message_edit` のログで届いているか確認

### メイドが返事しない

- キューが詰まっていないか（`[WARN] maid_queue full` ログ）
- Gemini レート超過していないか（`[WARN] 503` ログ）
- プロンプト組み立てで例外が出ていないか（`[ERROR] _build_prompt`）

詳細は [known-issues.md](./known-issues.md) を参照。

---

## テスト方針（現状）

**テストは存在しない**。手動テストのみ。変更時のチェックリスト：

1. ローカルで起動 → コマンド実行（`python main.py`、要環境変数）
2. Render の dev 環境があるならそこで確認
3. バッチは GitHub Actions の `workflow_dispatch` で手動起動
4. 本番投入後はログ監視（Render ダッシュボード）

### 自動テストを入れるなら

- `pytest` + `mongomock` で MongoDB 操作の単体テスト
- プロンプト組立 (`_build_prompt`) の snapshot テスト（入力 → 期待されるプロンプト）
- Gemini 呼び出しは mock で止めるべき
- Discord のイベントは `dpytest` で模擬
