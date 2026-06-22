# バッチパイプライン

GitHub Actions 上で実行される全ての定期処理のまとめ。`batch/*.py` と root 直下の副業スクリプト（`market_report.py`, `ai_news_bot.py`, `cleanup_bot.py`）を統括。

## 8 個の workflow

| YAML | cron (UTC) | JST | 呼び出し | 所要時間目安 |
|------|-----------|-----|---------|------------|
| `summarize.yml` | `0 */2 * * *` | 2h毎 | fetch → summarize → update_mongodb | ~15 min |
| `daily_tasks.yml` | `0 15 * * *` | 0:00 | analyze_personality → analyze_nonbooster → post_summary → enrich_memories | ~30 min |
| `personality_analyze.yml` | `0 15 * * *` | 0:00 | analyze_personality **（daily_tasks と重複 ⚠️）** | ~15 min |
| `nikkei-report.yml` | `0 7 * * 1-5` | 平日16:00 | market_report.py | ~10 min |
| `ai_news.yml` | `0 1,4,7,10,13 * * *` | 10, 13, 16, 19, 22 時 | ai_news_bot.py | ~5 min |
| `cleanup.yml` | `0 15 * * *` | 0:00 | cleanup_bot.py（別トークン） | ~5 min |
| `retro_report.yml` | manual (dispatch) | - | batch/retro_summarize.py | ~30 min |
| `focus_summary.yml` | manual (dispatch) | - | batch/focus_summary.py | ~20 min |

**毎日 JST 0:00 に 3 つの workflow が一斉起動**（`daily_tasks`, `personality_analyze`, `cleanup`）。GitHub Actions の同時実行キューに注意。

## パイプライン 1: 2時間毎の要約（summarize.yml）

### データフロー

```
fetch_discord_logs.py (batch/)
    ├ 環境変数: DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DISCORD_CHANNEL_IDS, EXCLUDE_CHANNEL_IDS
    ├ REST API で過去 HOURS_BACK=2 時間のメッセージ取得（Gateway 禁止！）
    ├ 全テキストチャンネル + アクティブスレッドを対象
    ├ NSFW / EXCLUDE_CHANNEL_IDS / Bot / webhook を除外
    └ 出力: /tmp/logs.json { fetched_at, hours_back, message_count, messages[] }

summarize.py (batch/)
    ├ 入力: /tmp/logs.json
    ├ モデル: gemma-4-31b-it → gemma-3-27b-it（503 時フォールバック）
    ├ プロンプト: SUMMARY_SYSTEM_PROMPT（## セクション構造）
    │   - 全体の雰囲気, 主なトピック, 感情の波, 注目の発言, 今日の内輪ネタ,
    │   - メンバーの人間関係, ユーザーの感情状態, 直近の話題, 会話の特徴,
    │   - ニックネーム・愛称マッピング（JSON形式で末尾に）
    ├ リトライ: 503 で 20s × attempt、最大3回
    └ 出力: /tmp/summary_result.json { summary, message_count, created_at, fetched_at, nickname_map }

update_mongodb.py (batch/)
    ├ 入力: /tmp/summary_result.json
    ├ インデックス作成: created_at DESC（冪等。最新判定はこの降順ソート）
    ├ 新ドキュメントを insert（is_latest フラグは廃止・書かない）
    ├ nickname_map merge: {**ai_map, **existing_map}（手動登録優先！）
    └ 出力: summaries コレクション + system.nickname_map
```

### 重要な設計上の選択

- **Gateway 絶対禁止**: `batch/fetch_discord_logs.py` は `requests` のみ。`discord.py` を import すると同トークンで本番 Bot が切れる。`requirements-batch.txt` から `discord.py` を意図的に除外
- **一時ファイル `/tmp/*.json`**: GitHub Actions のステップ間データ渡し手段。ジョブが終わると消える
- **nickname_map の merge 優先順**: AI 検出 < 手動（`/addnick`）。`update_mongodb.py` L56 の `{**ai_map, **existing_map}` はこの順序が**絶対**

## パイプライン 2: 日次タスク（daily_tasks.yml）

### 4 つのバッチを直列実行

```
analyze_personality.py
    ├ 条件: users.conv_count >= 10（ブースター対象）
    ├ 3段階分析:
    │   ① TONE_PROMPT: butler_history（生発言）から口調・語彙・テンション
    │   ② CONTEXT_PROMPT: summaries（直近7日）から性格・背景・人間関係
    │   ③ MERGE_PROMPT: 既存 profile と統合
    └ 書込: users.profile.{tone, communication_style, vocabulary, personality, background, relations, interests_vibe}

analyze_nonbooster.py
    ├ 対象: 非ブースター（conv_count 閾値なし）
    ├ summaries から簡易分析（ANALYZE_PROMPT）
    └ 書込: users.simple_profile.{tone_tags, vibe, personality, background, relations, frequent_members}

post_summary.py
    ├ 入力: summaries の最新（created_at 降順、is_retro/retro_date 除外）
    ├ セクション優先度順に並び替え（SECTION_ORDER: 直近の話題 > 感情の波 > ...）
    ├ Discord Embed を 6000 文字制限 + 25 フィールド制限で分割
    └ 出力: Discord SUMMARY_CHANNEL_ID に投稿（最大10Embed/メッセージ）

enrich_memories.py
    ├ 対象: 全ブースターユーザー
    ├ summaries（直近7日）から各ユーザーの claims / memories を抽出
    ├ EXTRACT_PROMPT で JSON 出力
    ├ memories には gemini-embedding-001 で embedding 付与
    └ 書込: users.claims（最大20件）, users.memories（最大40件・新しい順）
```

### 使用モデル

| スクリプト | メインモデル | フォールバック |
|----------|-----------|--------------|
| summarize.py | `gemma-4-31b-it` | `gemma-3-27b-it` |
| analyze_personality.py | `gemma-4-31b-it` | `gemma-3-27b-it` |
| analyze_nonbooster.py | `gemma-4-31b-it` | `gemma-4-26b-a4b-it` |
| enrich_memories.py | `gemma-4-31b-it` | `gemma-4-26b-a4b-it` |
| Embedding | `gemini-embedding-001`（3072次元） | なし（失敗で skip） |

## パイプライン 3: 手動トリガー

### retro_report.yml

```
/retroreport 2026-03-01 (Discord)
    ↓
main.py retroreport_cmd (L2575)
    ├ 既存 retro_date チェック → あれば再投稿して終了
    └ POST /repos/{GITHUB_REPO}/actions/workflows/retro_report.yml/dispatches
      inputs: { target_date: "2026-03-01" }

retro_summarize.py (batch/)
    ├ 環境変数: TARGET_DATE, DISCORD_GUILD_ID, CHANNEL_IDS, EXCLUDE_IDS, GEMINI_API_KEY, MONGODB_URI, SUMMARY_CHANNEL_ID
    ├ REST API で指定日 JST 24時間分のメッセージ取得
    ├ 文脈: 前後 CONTEXT_DAYS=2 日の要約を MongoDB から取得
    ├ Gemini で RETRO_SUMMARY_PROMPT を実行
    ├ 既存 retro_date ドキュメントがあれば delete → insert（重複防止）
    └ Discord 投稿（SUMMARY_CHANNEL_ID）+ MongoDB 保存（is_retro=True）
```

### focus_summary.yml

`/focus @member` または `/focus keyword=Among Us` からトリガー。メンバー指定時は `users.profile/claims/memories` にも書き戻す。

```
focus_summary.py
    ├ 環境変数: FOCUS_TYPE, FOCUS_TARGET, FOCUS_NAME
    ├ FOCUS_TYPE="member": 特定メンバーの全発言を集めて分析
    │   → プロンプト: MEMBER_FOCUS_PROMPT
    │   → 書戻: users.profile 更新 + claims + memories（embedding 付与）
    ├ FOCUS_TYPE="keyword": keyword を含む発言を収集して分析
    │   → プロンプト: KEYWORD_FOCUS_PROMPT
    └ Discord 投稿
```

## 副業スクリプト（root 直下）

### market_report.py（平日 16 時）

**非常に複雑な 520 行スクリプト**。平日の市場レポート + 月曜予想投票 + 金曜集計。

```
main() (L478)
    ├ get_market_data(): yfinance で日経/S&P/VIX/USDJPY の月次データ
    ├ create_dashboard(): matplotlib で3パネルグラフ（/tmp/dashboard.png）
    ├ personality を system から取得（メイン Bot と共有）
    ├ generate_ai_comment(): 人格に応じた市場コメント
    │   ├ gemini-2.5-flash-lite で検索グラウンディング
    │   └ 失敗時 gemma-3-27b-it → gemini-2.5-flash-lite でフォールバック
    ├ send_daily_report(): Webhook で投稿（dashboard.png 添付）
    │
    ├ 月曜日のみ:
    │   ├ generate_prediction_hint(): 今週の注目材料を検索
    │   └ post_prediction_poll(): 投票メッセージ + 📈/📉 リアクション
    │       └ save_prediction_message(): MongoDB market_predictions に保存
    │
    └ 金曜日のみ:
        └ collect_and_announce_results():
            ├ market_predictions の未解決を取得
            ├ 投票メッセージのリアクション集計
            ├ 終値との比較で正解判定
            ├ 正解者に users.$inc(xp: 50)
            └ 結果発表 Embed 投稿
```

**使用モデル**:
- `gemma-3-27b-it` (MODEL, L44) — メイン
- `gemini-2.5-flash-lite` (MODEL_SEARCH, L46) — 検索グラウンディング
- `gemini-flash-lite-latest` (MODEL_SEARCH_ALT, L47) — エイリアス fallback

**Webhook vs Bot Token**:
- 日次レポート: **Webhook のみ**（`DISCORD_WEBHOOK_URL`）
- 月曜投票: Bot Token 優先（メッセージ ID 取得のため）、失敗時 Webhook
- 金曜集計: **Bot Token 必須**（リアクション読取）

### ai_news_bot.py（3 時間毎）

```
main() (L75)
    ├ 3つの Yahoo RSS を取得（business, it, science）各カテゴリ5記事
    ├ 特異点判定: last_top_url.txt とトップ記事 URL を比較
    │   → 同じならスキップ（処理コスト節約）
    ├ analyze_trends(): gemini-2.5-flash で「加速主義的経済分析」
    │   └ 語尾【ぺポッ】【ポヨッ】絶対維持・700文字以内
    ├ Discord に投稿（NEWS_WEBHOOK_URL）
    └ last_top_url.txt を更新 → GitHub Actions でコミット & push（[skip ci]）
```

`last_top_url.txt` を git commit するため `permissions: contents: write` が必要（`ai_news.yml` L11-12）。

### cleanup_bot.py（毎日 JST 0:00）

```
on_ready()
    ├ guild.members でメンバー一覧を取得
    ├ intro_channel の過去 300 メッセージをスキャン
    ├ 投稿者が現在のメンバーでないメッセージを削除
    └ Bot 投稿はスキップ
```

**別 Token（`CLEANUP_BOT_TOKEN`）を使う理由**:
- メインと分離することで、大量削除がレート制限に当たっても本番 Bot が影響を受けない
- `discord.py` を使うが別トークンなので Gateway 競合は起きない

## エラー処理・リトライ戦略

### 共通パターン（全 batch スクリプト）

```python
def _extract_retry_wait(err_str: str) -> float:
    m = re.search(r"retry in ([\d.]+)s", err_str)
    return float(m.group(1)) + 2.0 if m else 30.0

for model, label in [(MODEL, "main"), (MODEL_FB, "fallback")]:
    for attempt in range(5):
        try:
            response = client.models.generate_content(model=model, ...)
            return response.text
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                time.sleep(_extract_retry_wait(err))
            elif "503" in err or "UNAVAILABLE" in err:
                time.sleep((attempt + 1) * 15)
            else:
                break  # 未知のエラーは fallback モデルへ
```

- 429 レスポンスから `retry in 22.5s` の形式を正規表現で抽出
- 503 は線形に増やす
- 全モデル・全リトライ失敗で `RuntimeError` または skip

### Discord REST API（fetch_discord_logs.py）

- 429 → `Retry-After` ヘッダに従って待機
- 403 → 権限なし → skip
- 404 → チャンネル不在 → skip
- その他 5 回リトライ

## MongoDB 書込マトリックス

| コレクション | summarize | update_mongodb | post_summary | analyze_personality | analyze_nonbooster | enrich_memories | retro_summarize | focus_summary | market_report |
|-------------|-----------|---------------|--------------|---------------------|--------------------|-----------------| ----------------|---------------|---------------|
| `summaries` | ─ | ✏️ insert | 📖 read | 📖 read | 📖 read | 📖 read | ✏️ replace | 📖 read | ─ |
| `system` | ─ | ✏️ nickname_map | ─ | ─ | ─ | ─ | ─ | ─ | 📖 personality |
| `users.profile` | ─ | ─ | ─ | ✏️ set | ─ | ─ | ─ | ✏️ set (member) | ─ |
| `users.simple_profile` | ─ | ─ | ─ | ─ | ✏️ set | ─ | ─ | ─ | ─ |
| `users.claims` | ─ | ─ | ─ | ─ | ─ | ✏️ push | ─ | ✏️ push | ─ |
| `users.memories` | ─ | ─ | ─ | ─ | ─ | ✏️ push + embed | ─ | ✏️ push + embed | ─ |
| `users.xp` | ─ | ─ | ─ | ─ | ─ | ─ | ─ | ─ | ✏️ $inc (金曜) |
| `market_predictions` | ─ | ─ | ─ | ─ | ─ | ─ | ─ | ─ | ✏️ insert/update |

## 拡張時の注意点

### 新しいバッチを追加するとき

1. `batch/` に Python ファイルを追加（名前は `xxx.py`）
2. `.github/workflows/xxx.yml` を作成、`pip install -r requirements-batch.txt`
3. secrets に依存 → GitHub Settings → Actions → secrets に追加
4. **Gateway 禁止**: `discord.py` を `import` しない。`requests` で REST を叩く
5. `/tmp/*.json` は同一 job 内でのみ共有可。ジョブを分けると消える
6. 長時間処理は `timeout-minutes:` を明示

### workflow の cron 衝突を避ける

- 同時刻に複数 workflow を走らせると GitHub Actions のキューに詰まる
- 現状 JST 0:00 に 3 つ走っているが、1 分ずつずらすほうが望ましい

### モデル ID の扱い

- `gemma-*` 系と `gemini-*` 系が混在（Gemini プレビューは廃止リスク高）
- 新規バッチ追加時は `MODEL`, `MODEL_FALLBACK` 定数を top に置く（他バッチと合わせる）
- モデル変更時は **すべての batch/*.py を grep して一括置換**

## 既知の問題

- `personality_analyze.yml` と `daily_tasks.yml` が同じ時刻に `analyze_personality.py` を二重実行する
  - 同時なのでレースコンディションは軽微だが、Gemini 呼出が倍になる
  - 解決案: `personality_analyze.yml` を削除するか、cron をずらす
- `batch/requirements-batch.txt` は root と同内容のコピー。どちらを正にするか曖昧（workflow は root を参照）
- モデル ID が各ファイルに直書き。centralize 候補
