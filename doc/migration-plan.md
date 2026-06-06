# messages コレクション導入プラン

**目的**: 空気くんの応答品質を 1 ミリも下げずに Discord API 負荷を 95% 削減し、BAN リスクを排除する。

**設計原則**:
- 空気くん応答の品質ソース 9 項目のうち、変更が及ぶのは 2 項目（`channel_context`, `summaries`）のみ
- その 2 項目も「取得先が Discord API → MongoDB」に変わるだけで**素材のメッセージは完全に同じ**
- 並走期間を設け、新旧どちらかが落ちても動く状態を維持
- ロールバック容易性を最優先

## 目次

- [Phase 0: 設計確定](#phase-0-設計確定)
- [Phase 1: main.py に書込追加（安全な先行実施）](#phase-1-mainpy-に書込追加安全な先行実施)
- [Phase 2: channel_context を MongoDB 読取に切替](#phase-2-channel_context-を-mongodb-読取に切替)
- [Phase 3: 新バッチ `fetch_from_db.py` を作成](#phase-3-新バッチ-fetch_from_dbpy-を作成)
- [Phase 4: summarize.yml を新バッチに切替](#phase-4-summarizeyml-を新バッチに切替)
- [Phase 5: 後始末と監視](#phase-5-後始末と監視)
- [タイムライン](#タイムライン)
- [ロールバック手順](#ロールバック手順)
- [品質検証チェックリスト](#品質検証チェックリスト)

---

## Phase 0: 設計確定

### messages コレクションスキーマ

```python
{
    "_id": str,                 # Discord message ID（文字列）。重複防止の自然キー
    "guild_id": str,            # サーバー ID（将来マルチサーバー化への備え）
    "channel_id": str,          # チャンネル ID
    "channel_name": str,        # 要約時の可読性のため保存
    "author_id": str,           # Discord user ID
    "author_name": str,         # display_name
    "content": str,             # 本文（最大 2000 文字、Discord の上限と一致）
    "timestamp": datetime,      # 発言時刻（UTC, message.created_at）
    "is_thread": bool,          # スレッド内発言なら true
    "parent_channel_id": str,   # スレッドの親 ch（非スレッドなら空文字）
    "created_at": datetime,     # DB 挿入時刻（TTL 用）
}
```

**設計根拠**:
- `_id` に Discord message ID を使う → 重複 insert がエラーになり重複排除が自動
- `content` は**切り詰めない**（2000字 Discord 上限）。要約・文脈品質を最大化
- `author_name` は保存時のスナップショット。後でユーザーがニックネーム変更しても当時の名前が残る
- `timestamp` と `created_at` を分ける → TTL は挿入時刻基準だが、クエリは発言時刻基準で行える
- スレッド判定と親 ch 情報を持つ → `summaries` のセクション分類が現行と同等にできる

### インデックス

```python
# ① channel_context クエリ用（最頻繁）
messages_col.create_index([("channel_id", 1), ("timestamp", -1)], background=True)

# ② 要約バッチ用（2h 分全ch スキャン）
messages_col.create_index([("guild_id", 1), ("timestamp", -1)], background=True)

# ③ TTL 自動削除（30日）
messages_col.create_index("created_at", expireAfterSeconds=30*24*3600, background=True)
```

**TTL 30日の根拠**:
- Atlas 無料枠 512MB に収める（試算: 30日で ~45MB、余裕あり）
- `retro_summarize` と `focus_summary` は TTL 超過日を扱う可能性あり → **旧 REST 版を保守継続**
- 30 日を超える過去をどうしても扱いたい場合は、`retro_summarize` の REST 版を明示的に呼ぶ

### 対象メッセージ

| 種別 | 蓄積する? | 理由 |
|-----|---------|------|
| 通常メッセージ（人間） | ✅ | 空気くんの主食 |
| スレッド内メッセージ | ✅ | 現行の要約も拾っている |
| Bot メッセージ | ❌ | 既存の `on_message` 冒頭で return |
| Webhook メッセージ | ❌ | 同上 |
| NSFW チャンネル | ❌ | `fetch_discord_logs.py` と同じフィルタ |
| EXCLUDE_CHANNEL_IDS の ch | ❌ | 設定尊重 |
| BUTLER_CHANNEL_ID | △ | ブースター私的会話。含めないのが無難（`butler_history` で十分） |

### ストレージ試算

```
仮に: 3,000 発言/日 × 平均 200 bytes（日本語 100文字 + メタデータ）= 600KB/日
30 日保持: 600KB × 30 = 18 MB
最悪ケース (10,000 発言/日): 60 MB
```

Atlas 512MB に対して十分余裕。既存の `users`, `summaries` と合わせても問題なし。

### 変数名ポリシー

- コレクション名: `messages`
- main.py 参照名: `messages_col`
- 環境変数: 既存の `MONGO_URL` / `MONGODB_URI` を使用（新設しない）

---

## Phase 1: main.py に書込追加（安全な先行実施）

**所要**: 30 分（コード変更）+ 48h（蓄積確認）

### なぜ書込だけ先行するか

- 読取を変えない限り**現行動作に一切影響しない**（ロールバックも insert を消すだけ）
- 書込先が壊れていても main.py は止めない（`try-except` でラップ）
- 48h で 2h 分以上のデータが溜まり、Phase 2/3 の切替が安全にできる

### 変更箇所

**① `main.py` L1496 付近（MongoDB コレクション追加）**

```python
# 変更前
mongo_client_db = AsyncIOMotorClient(MONGO_URL)
db             = mongo_client_db["discord_bot_db"]
users_col      = db["users"]
system_col     = db["system"]
summaries_col  = db["summaries"]

# 変更後
mongo_client_db = AsyncIOMotorClient(MONGO_URL)
db             = mongo_client_db["discord_bot_db"]
users_col      = db["users"]
system_col     = db["system"]
summaries_col  = db["summaries"]
messages_col   = db["messages"]         # ← 新規
```

**② `setup_hook`（L1503-）にインデックス作成を追加**

```python
async def setup_hook(self):
    print("[INFO] インデックス作成中...")
    await users_col.create_index([("xp", -1)], name="xp_desc", background=True)
    # messages コレクションのインデックス
    await messages_col.create_index(
        [("channel_id", 1), ("timestamp", -1)], name="ch_ts", background=True
    )
    await messages_col.create_index(
        [("guild_id", 1), ("timestamp", -1)], name="guild_ts", background=True
    )
    await messages_col.create_index(
        "created_at", name="ttl_30d", expireAfterSeconds=30*24*3600, background=True
    )
    # ... 以下既存 ...
```

**③ `on_message`（L1697-）の冒頭に記録処理を追加**

```python
@client.event
async def on_message(message: discord.Message):
    try:
        # Resumeループ対策: 30秒以上前のメッセージは処理しない
        msg_age = (datetime.datetime.now(datetime.timezone.utc) - message.created_at).total_seconds()
        if msg_age > 30:
            return

        if message.author.bot:
            # ... 既存の Bump 処理 ...
            return

        # === 新規: messages コレクションに記録 ===
        # 失敗しても以降の処理は継続（BAN対策が本体を止めないよう保護）
        try:
            is_thread = hasattr(message.channel, 'parent_id') and message.channel.parent_id is not None
            await messages_col.insert_one({
                "_id":               str(message.id),
                "guild_id":          str(message.guild.id) if message.guild else "",
                "channel_id":        str(message.channel.id),
                "channel_name":      getattr(message.channel, "name", ""),
                "author_id":         str(message.author.id),
                "author_name":       message.author.display_name,
                "content":           message.content[:2000],
                "timestamp":         message.created_at,
                "is_thread":         is_thread,
                "parent_channel_id": str(message.channel.parent_id) if is_thread else "",
                "created_at":        datetime.datetime.now(datetime.datetime.UTC if hasattr(datetime.datetime, 'UTC') else datetime.timezone.utc),
            })
        except Exception as log_e:
            # 重複 (_id) や一時的な DB エラーは無視
            if "duplicate" not in str(log_e).lower():
                print(f"[WARN] messages log failed: {log_e}")

        # ... 以降既存の処理 ...
```

### 除外条件の実装

EXCLUDE_CHANNEL_IDS が main.py 側に未定義（batch 専用）なので、Phase 1 では全ch を蓄積する。TTL 30日で自動整理されるので問題ないが、明示的に絞りたい場合は main.py に定数として追加：

```python
# main.py 定数セクション
EXCLUDE_LOGGING_CHANNELS: set[int] = set(
    int(x) for x in (os.environ.get("EXCLUDE_LOGGING_CHANNELS", "")).split(",") if x.strip()
)

# on_message 内の insert 前
if message.channel.id in EXCLUDE_LOGGING_CHANNELS:
    return  # ログ記録スキップ、XP 等は継続
```

**推奨**: Phase 1 では除外なし。様子を見て Phase 5 で追加。

### デプロイ後の検証

```bash
# MongoDB Atlas コンソールで
db.messages.countDocuments({})                    # 蓄積数
db.messages.find().sort({timestamp: -1}).limit(5) # 最新5件の中身確認
db.messages.getIndexes()                          # インデックス作成確認
```

確認項目:
- [ ] 1 時間後に数百件以上蓄積
- [ ] 24 時間後に TTL index が反映（`ttl_30d` インデックスが存在）
- [ ] ブースターチャンネル (`BUTLER_CHANNEL_ID`) のメッセージも蓄積されているか確認（後で除外判断）
- [ ] スレッド内発言の `is_thread: true` と `parent_channel_id` が正しい

### ロールバック

変更を revert するだけ。MongoDB にデータが残っても害はなく、30 日で TTL 自動削除。

---

## Phase 2: channel_context を MongoDB 読取に切替

**所要**: 30 分
**前提**: Phase 1 で 48h 以上蓄積済み

### 変更箇所

**`main.py` L1328-1345**

```python
# 変更前
channel_context = ""
try:
    ctx_lines = []
    async for m in message.channel.history(limit=12, before=message):
        if m.author.bot:
            continue
        author_str = m.author.display_name
        text       = re.sub(r"<@!?\d+>", "", m.content).strip()
        if text:
            ctx_lines.append(f"{author_str}: {text}")
        if len(ctx_lines) >= 10:
            break
    if ctx_lines:
        ctx_lines.reverse()
        channel_context = "\n".join(ctx_lines)
except Exception as ce:
    print(f"[WARN] channel_context取得失敗: {ce}")

# 変更後
channel_context = ""
try:
    # 同じチャンネルの直前 12 件を MongoDB から取得（応答対象メッセージを除く）
    cursor = messages_col.find({
        "channel_id": str(message.channel.id),
        "_id": {"$ne": str(message.id)},
    }).sort("timestamp", -1).limit(12)

    docs = await cursor.to_list(length=12)
    if docs:
        ctx_lines = []
        for d in reversed(docs):  # 古い順に並べ直す
            text = re.sub(r"<@!?\d+>", "", d.get("content", "")).strip()
            if text:
                ctx_lines.append(f"{d.get('author_name', '')}: {text}")
            if len(ctx_lines) >= 10:
                break
        if ctx_lines:
            channel_context = "\n".join(ctx_lines)
    else:
        # フォールバック: DB に未蓄積の場合は Discord REST（移行期の安全弁）
        ctx_lines = []
        async for m in message.channel.history(limit=12, before=message):
            if m.author.bot:
                continue
            text = re.sub(r"<@!?\d+>", "", m.content).strip()
            if text:
                ctx_lines.append(f"{m.author.display_name}: {text}")
            if len(ctx_lines) >= 10:
                break
        if ctx_lines:
            ctx_lines.reverse()
            channel_context = "\n".join(ctx_lines)
except Exception as ce:
    print(f"[WARN] channel_context取得失敗: {ce}")
```

### 品質検証

Phase 2 デプロイ直後に複数チャンネルでメンション → メイド応答を試す。

応答内容が「直前の会話を自然に踏まえている」かを目視確認：
- 会話の流れを汲んだリアクション
- 直前に出た固有名詞を使うか
- 話題の切り替わりを認識できているか

### 効果測定

Render のログで `[WARN] channel_context取得失敗` が増えていないか監視。

### ロールバック

REST 呼出のコードを戻すだけ。

---

## Phase 3: 新バッチ `fetch_from_db.py` を作成

**所要**: 1〜2 時間
**前提**: Phase 1 から 48h 以上経過（2h 分が確実に蓄積）

### 新ファイル `batch/fetch_from_db.py`

既存 `batch/fetch_discord_logs.py` と**同じ `/tmp/logs.json` 形式**で出力する。これで `summarize.py` を変更せずに済む。

```python
"""
fetch_from_db.py

MongoDB messages コレクションから過去 HOURS_BACK 時間分のメッセージを取得し、
/tmp/logs.json として保存する。

【重要】
旧 fetch_discord_logs.py の REST 版を置き換える。Discord API を一切叩かない。
出力形式は旧版と完全互換（summarize.py 側の変更不要）。
"""

import os
import json
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient

MONGODB_URI       = os.environ["MONGODB_URI"]
DISCORD_GUILD_ID  = os.environ["DISCORD_GUILD_ID"]
CHANNEL_IDS_RAW   = os.environ.get("DISCORD_CHANNEL_IDS", "")
CHANNEL_IDS       = {c.strip() for c in CHANNEL_IDS_RAW.split(",") if c.strip()}
EXCLUDE_IDS_RAW   = os.environ.get("EXCLUDE_CHANNEL_IDS", "")
EXCLUDE_IDS       = {c.strip() for c in EXCLUDE_IDS_RAW.split(",") if c.strip()}
HOURS_BACK        = int(os.environ.get("HOURS_BACK", "2"))
OUTPUT_PATH       = "/tmp/logs.json"
DB_NAME           = "discord_bot_db"


def main():
    print(f"[fetch_from_db] Querying MongoDB (last {HOURS_BACK}h)")
    mongo = MongoClient(MONGODB_URI)
    col   = mongo[DB_NAME]["messages"]

    since = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)

    query: dict = {
        "guild_id":  DISCORD_GUILD_ID,
        "timestamp": {"$gte": since},
    }
    if CHANNEL_IDS:
        # include 方式（指定 ch のみ）。parent_channel_id もチェックしてスレッドを親と同じ扱い
        query["$or"] = [
            {"channel_id": {"$in": list(CHANNEL_IDS)}},
            {"parent_channel_id": {"$in": list(CHANNEL_IDS)}},
        ]

    docs = col.find(query).sort("timestamp", 1)

    messages = []
    for d in docs:
        if d.get("channel_id") in EXCLUDE_IDS or d.get("parent_channel_id") in EXCLUDE_IDS:
            continue
        messages.append({
            "channel":   d.get("channel_name", ""),
            "author":    d.get("author_name", ""),
            "author_id": d.get("author_id", ""),
            "timestamp": d["timestamp"].isoformat() if isinstance(d.get("timestamp"), datetime) else d.get("timestamp", ""),
            "content":   d.get("content", ""),
        })

    print(f"[fetch_from_db] Total messages: {len(messages)}")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "fetched_at":    datetime.now(timezone.utc).isoformat(),
            "hours_back":    HOURS_BACK,
            "message_count": len(messages),
            "messages":      messages,
        }, f, ensure_ascii=False, indent=2)
    print(f"[fetch_from_db] Saved {len(messages)} messages to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
```

### 互換性の担保

既存 `batch/summarize.py` が期待する形式:
```json
{
  "fetched_at": "...", "hours_back": 2, "message_count": N,
  "messages": [
    {"channel": "...", "author": "...", "author_id": "...", "timestamp": "...", "content": "..."}
  ]
}
```

新バッチもこの形式で出力 → `summarize.py` / `update_mongodb.py` は**一切変更不要**。

### ローカル検証

```bash
cd finance
export MONGODB_URI="..."
export DISCORD_GUILD_ID="1128769816820465766"
python batch/fetch_from_db.py
cat /tmp/logs.json | head -30
# 旧版と同じ形式であることを確認
```

`batch/summarize.py` もローカルで通して、要約が生成できることを確認。

---

## Phase 4: summarize.yml を新バッチに切替

**所要**: 15 分
**前提**: Phase 3 ローカル検証完了

### 変更箇所

**`.github/workflows/summarize.yml`**

```yaml
# 変更前
- name: Fetch Discord logs
  env:
    DISCORD_BOT_TOKEN:    ${{ secrets.DISCORD_BOT_TOKEN }}
    DISCORD_GUILD_ID:     ${{ secrets.DISCORD_GUILD_ID }}
    DISCORD_CHANNEL_IDS:  ${{ secrets.DISCORD_CHANNEL_IDS }}
    EXCLUDE_CHANNEL_IDS:  ${{ secrets.EXCLUDE_CHANNEL_IDS }}
  run: python batch/fetch_discord_logs.py

# 変更後
- name: Fetch messages from MongoDB
  env:
    MONGODB_URI:          ${{ secrets.MONGODB_URI }}
    DISCORD_GUILD_ID:     ${{ secrets.DISCORD_GUILD_ID }}
    DISCORD_CHANNEL_IDS:  ${{ secrets.DISCORD_CHANNEL_IDS }}
    EXCLUDE_CHANNEL_IDS:  ${{ secrets.EXCLUDE_CHANNEL_IDS }}
  run: python batch/fetch_from_db.py
```

**secret 変更**: `DISCORD_BOT_TOKEN` は不要になる（が、削除不要。他 workflow で使用）

### 初回実行時の注意

- Phase 1 から **必ず 48h 以上** 経過してから Phase 4 に進む
- 最初の1回は `workflow_dispatch` で手動実行してログ確認
- 失敗したら即 revert

### 検証

```
MongoDB summaries コレクション:
  最新 1 件の created_at が実行直後の時刻か
  summary 本文が「##」で始まるか
  message_count が妥当（数百〜数千）か
```

### 副次的に確認すべきこと

- `batch/analyze_personality.py` と `batch/enrich_memories.py` は `summaries` を読むだけなので、入力品質が落ちなければそのまま動く
- `post_summary.py` も同じ

---

## Phase 5: 後始末と監視

**所要**: 継続的
**前提**: Phase 4 が 1 週間安定稼働

### やること

- [ ] **channel_context の REST fallback を削除**
  - Phase 2 の fallback コードを除去。`messages` コレクションが空なら応答のチャンネル文脈が空になるだけで、致命的ではない
  - ロールバック期間後（2 週間目安）

- [ ] **known-issues.md 項目 7 を「対応済み」に更新**
  - 現状の緊急対策（cron 4h 化等）は Phase 4 完了後に戻しても OK
  - どう変わったかの記録を残す

- [ ] **`batch/fetch_discord_logs.py` は残す**
  - `retro_summarize.py` / `focus_summary.py` が TTL 超過日を扱うため
  - ただし内部的には使っていないので、不要ならコメントで「legacy, used only by retro/focus」と明記

- [ ] **`batch/requirements-batch.txt` の確認**
  - `fetch_from_db.py` は `pymongo` のみ必要。既に入っているので追加不要
  - `requests` は retro/focus 系で残しておく

- [ ] **監視項目**
  - MongoDB ストレージ使用量（月次確認）
  - `messages` コレクションのドキュメント数（`db.messages.countDocuments({})`）
  - `summarize.yml` の実行時間（変更前 ~15min → 変更後 ~5min 想定）
  - Discord API の `[WARN] Rate limited` ログがゼロになること

### オプション拡張（やっても良い改善）

1. **`butler_history` と `messages` の統合**
   - `butler_history` はメイド会話だけの独立配列。`messages` に `is_butler_interaction: true` フラグで一元管理できる
   - ただし既存データのマイグレーションが必要なので、急ぐ必要はない

2. **`channel_context` の件数を動的調整**
   - 現状 12 件固定。会話密度が高いチャンネルでは増やす・低いチャンネルは減らす、など
   - MongoDB からの取得なので柔軟に制御可能

3. **`/search <keyword>` コマンド新設**
   - `messages` から全文検索。Atlas Search index を追加すれば可能
   - ユーザー価値が高く、負荷も低い

---

## タイムライン

```
Day  0  Phase 0 完了（この doc）
Day  0  Phase 1 実装・デプロイ（main.py 書込追加）
Day  2  Phase 1 検証完了（48h 蓄積）
Day  2  Phase 2 実装・デプロイ（channel_context 切替）
Day  3  Phase 2 品質確認
Day  3  Phase 3 実装（fetch_from_db.py 作成）
Day  3  Phase 3 ローカル検証
Day  4  Phase 4 workflow 切替（手動実行で検証）
Day  4-7 Phase 4 安定性監視
Day 14  Phase 5 後始末（REST fallback 削除、doc 更新）
```

最短で **1 週間**、安全に行って **2 週間** で完了。

---

## ロールバック手順

各 Phase ごとに独立してロールバック可能：

| Phase | ロールバック方法 | 影響 |
|-------|--------------|-----|
| Phase 1 | main.py の insert 削除 | なし（書込のみ停止） |
| Phase 2 | main.py の channel_context 取得を REST に戻す | なし（品質は同等） |
| Phase 3 | 新ファイル削除 | なし（workflow 未切替） |
| Phase 4 | workflow yml を `fetch_discord_logs.py` に戻す | Discord 負荷増（元に戻る） |
| Phase 5 | fallback 復活 | なし |

**どの Phase でロールバックしても、旧動作にピクセル完全に戻れる**。

---

## 品質検証チェックリスト

### Phase 2 完了時（channel_context 切替）

以下シナリオで応答を比較：

1. **直前の雑談を拾えるか**
   - 友人 A「カレー食べたい」 → 友人 B「俺も」 → `@空気くん どうする？`
   - 期待: カレーの話題を認識した応答

2. **固有名詞を引き継げるか**
   - 「ドラクエ10のラスボス倒したー」 → `@空気くん` と続く
   - 期待: ドラクエ10に言及する応答

3. **スレッド内での文脈**
   - スレッド内で数件やり取り → `@空気くん`
   - 期待: スレッド内流れを踏まえた応答（スレッドの messages も蓄積されている前提）

4. **BUTLER_CHANNEL_ID 内**
   - ブースターが続けてメッセージ送信 → メイド自動応答
   - 期待: 会話の流れが保たれる

### Phase 4 完了時（要約切替）

- [ ] 変更直後に `summaries.find({is_latest: True})` を確認、`summary` が `## 全体の雰囲気` で始まる
- [ ] `message_count` が妥当（日中帯なら数百〜数千）
- [ ] セクションが 9 つ揃っている
- [ ] ニックネームマップに異常な追加がない
- [ ] 3 日後、`post_summary.py` 経由の日報投稿が通常通り出る
- [ ] `analyze_personality.py` が **その要約を使って** 性格分析を更新できている

### 切替前後の A/B 比較（任意だが推奨）

Phase 4 の 1 日前に現行の要約を手元にコピー。切替後の要約と目視比較：

- セクション構成の一致
- 情報密度（登場人物数・トピック数）
- ニックネーム認識精度

---

## 付録: なぜこの設計になったか

### 代替案 1: main.py 側の性格分析も real-time 化する

**却下理由**:
- `analyze_personality.py` は **既に MongoDB ベースで動作中**（Discord API 無依存）
- 問題は **要約の素材である Discord ログ取得**にあり、性格分析ロジック自体は変える必要なし
- 今回の変更で要約の素材が DB 化されれば、性格分析も自動的に DB ベースになる（ `analyze_personality.py` 内部には一切手を入れない）

### 代替案 2: バッチと main.py で Bot Token を分離

**別案として有効だが、今回の一次対応ではない**:
- 分離は「万一 BAN されても本番が生存」する防御層として有効
- しかし Discord 負荷自体をゼロ化すれば BAN 自体が起きないため、優先度は下がる
- Phase 4 完了後に余裕があれば追加実施を推奨

### 代替案 3: messages コレクションではなく butler_history を拡張

**却下理由**:
- `butler_history` は **ユーザー × メイド** の対話専用で、全チャンネルのメッセージを含む設計ではない
- 意味論が違うものに詰め込むと将来破綻する
- `messages` を独立させる方が明確で、後々の拡張（全文検索・統計分析）にも対応しやすい

### なぜ TTL 30 日か

- 要約は 2h 毎 → 実質使うのは最新 2h だけ
- `channel_context` は直近 12 件 → 数時間〜数日あれば十分
- 30 日あれば `retro_summarize` の大半のリクエストにも応えられる（それ以上古いのは REST で取得）
- Atlas 512MB に余裕で収まる
- 短すぎると運用中の不慮の障害（24h バッチ遅延など）で穴が空くリスクあり
