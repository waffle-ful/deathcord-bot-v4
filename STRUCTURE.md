# プロジェクト構造

Discord コミュニティBot「空気くん」のファイル構成まとめ。実行環境は **Render.com 常駐 (main.py)** + **GitHub Actions バッチ** の二本立て。

## ディレクトリツリー

```
finance/
├── main.py                        [3,100行 / 140KB] ⭐ 本体Bot（Render常駐）
├── market_report.py               [  520行]         日次マーケットレポート
├── ai_news_bot.py                 [  139行]         AIニュース要約Bot
├── cleanup_bot.py                 [   65行]         自己紹介ch掃除Bot（別トークン）
│
├── batch/                          ← GitHub Actions 用バッチ本体
│   ├── fetch_discord_logs.py      [216行]           REST APIでログ収集
│   ├── summarize.py               [191行]           Gemini要約生成
│   ├── update_mongodb.py          [ 68行]           要約をMongo保存
│   ├── post_summary.py            [211行]           日報をDiscord投稿
│   ├── analyze_personality.py     [301行]           ブースター性格分析
│   ├── analyze_nonbooster.py      [192行]           非ブースター簡易分析
│   ├── enrich_memories.py         [246行]           claims/memories補完
│   ├── retro_summarize.py         [292行]           過去日報の遡及生成
│   ├── focus_summary.py           [510行]           特定メンバー/キーワード要約
│   └── requirements-batch.txt                       （root側と同内容のコピー）
│
├── .github/workflows/              ← cron スケジュール定義
│   ├── summarize.yml                               2時間毎：fetch→summarize→mongo
│   ├── daily_tasks.yml                             JST 0:00 毎日：性格分析＋日報投稿
│   ├── personality_analyze.yml                     JST 0:00 毎日：性格分析のみ（重複）
│   ├── nikkei-report.yml                           平日16:00：マーケットレポート
│   ├── ai_news.yml                                 3時間毎：AIニュース
│   ├── cleanup.yml                                 JST 0:00 毎日：自己紹介ch掃除
│   ├── retro_report.yml                            手動実行：過去日報の遡及作成
│   └── focus_summary.yml                           手動実行：絞り込み要約
│
├── requirements.txt                                main.py用（discord.py等）
├── requirements-batch.txt                          batch/*.py用（discord.py無し）
├── requirements-market.txt                         market_report.py用（yfinance等）
│
├── .python-version                                 3.12.1
├── README.md                                       （ほぼ空）
├── CLAUDE.md                                       Claude Code用アーキテクチャガイド
├── STRUCTURE.md                                    このファイル
└── last_top_url.txt                                ai_news_bot.py の履歴（自動更新）
```

## コンポーネント関係図

```
        ┌────────────────────────── Discord サーバー ──────────────────────────┐
        │                                                                      │
        │  ┌────────────────────┐                        ┌──────────────────┐  │
        │  │ ユーザー発言        │                        │ Bump系Bot応答    │  │
        │  └─────────┬──────────┘                        └────────┬─────────┘  │
        │            │ on_message / on_interaction                 │            │
        └────────────┼──────────────────────────────────────────────┼───────────┘
                     ▼                                              ▼
            ┌────────────────────────────────────────────────────────────┐
            │                    main.py（Render 常駐）                  │
            │  ┌────────────┐ ┌──────────────┐ ┌──────────────────────┐  │
            │  │ XP/ランク   │ │ Bump検知     │ │ AIメイド応答          │  │
            │  │ streak     │ │ ✨リアクション│ │ 6人格・記憶検索・論破 │  │
            │  └────────────┘ └──────────────┘ └──────┬───────────────┘  │
            │  ┌──────────────────────────────────────┼───────────────┐  │
            │  │ 会話キュー（_maid_queue, 単一worker） │               │  │
            │  │ レートリミッタ（12RPM, typing遅延で吸収）             │  │
            │  └──────────────────────────────────────┼───────────────┘  │
            │  スラッシュコマンド: /rank /top /maid /personality ...    │  │
            │  /retroreport /focus → GitHub API で workflow dispatch    │  │
            └──────┬──────────────────────────────┬──────────────┬──────┘
                   │ Motor (async)                │ google-genai │ aiohttp
                   ▼                              ▼              │ health
         ┌─────────────────────┐          ┌──────────────┐       │ :10000
         │   MongoDB Atlas      │          │ Gemini API   │       ▼
         │  discord_bot_db      │          │ (google-genai)│   Renderポート
         │  ├─ users            │          │              │     チェック
         │  │   .xp/.profile    │          │ 主要モデル:  │
         │  │   .butler_history │          │  3.1-flash-  │
         │  │   .memories[]     │          │  lite-preview│
         │  │   .claims[]       │          │  gemma-4-31b │
         │  ├─ system           │          │  embedding-  │
         │  │   .personality    │          │  001         │
         │  │   .nickname_map   │          └──────────────┘
         │  └─ summaries        │                  ▲
         │      .created_at ★   │                  │
         └──────────▲───────────┘                  │
                    │                              │
                    │ （バッチ書込）                 │
         ┌──────────┴───────────────────────────────┴──────────────┐
         │            GitHub Actions（cron スケジュール）             │
         │                                                          │
         │  2時間毎 │ fetch_discord_logs ─▶ summarize ─▶ update_mongo│
         │  日次0:00│ analyze_personality ─▶ analyze_nonbooster       │
         │        │  ─▶ post_summary ─▶ enrich_memories              │
         │  日次0:00│ cleanup_bot (別Bot token)                       │
         │  3時間毎 │ ai_news_bot（last_top_url.txt を自動push）       │
         │  平日16h│ market_report（yfinance + AI + 週次予想投票）    │
         │  手動  │ retro_summarize / focus_summary                 │
         └──────────┬───────────────────────────────────────────────┘
                    │ REST API only（Gateway禁止・token競合対策）
                    ▼
            Discord REST API
```

## データフロー

### ① AIメイドの返答（ホット経路）

```
on_message
  └─ maid_respond_queued
       └─ _maid_queue.put() ──▶ _maid_queue_worker
                                    └─ _maid_respond_inner
                                         ├─ channel.history(12件)で直近文脈取得
                                         ├─ _build_prompt
                                         │    ├─ PERSONALITIES[key].booster_prompt
                                         │    ├─ users.profile + simple_profile
                                         │    ├─ users.butler_history
                                         │    ├─ users.memories（vector search）
                                         │    ├─ users.claims（論破人格のみ）
                                         │    ├─ system.nickname_map
                                         │    └─ summaries（created_at降順, smart_summary）
                                         ├─ _run_ai_booster
                                         │    ├─ gemini-3.1-flash-lite（主）
                                         │    └─ gemma-4-31b（503フォールバック）
                                         ├─ typing() + rate_wait延長
                                         └─ message.reply + profile抽出をbackground
```

### ② 日報パイプライン（2時間毎）

```
fetch_discord_logs.py   /tmp/logs.json
    │ REST API (Gateway禁止)
    ▼
summarize.py            /tmp/summary_result.json
    │ Gemini要約 + nickname_map抽出
    ▼
update_mongodb.py       MongoDB
    │ summaries にinsert（created_at降順で最新判定）
    │ system.nickname_map マージ（既存優先）
    ▼
（日次0:00にpost_summary.pyが日報ch投稿）
```

### ③ 手動トリガーのバッチ

```
/retroreport YYYY-MM-DD    →  main.py から GitHub API
                            →  retro_report.yml dispatch
                            →  retro_summarize.py 実行
                            →  MongoDB保存 + Discord投稿

/focus @member / keyword   →  focus_summary.yml dispatch
                            →  focus_summary.py 実行
                            →  人物ならprofile自動反映
```

## モジュール責務

| ファイル | 実行環境 | 責務 | 依存 |
|---|---|---|---|
| `main.py` | Render.com 常駐 | Gatewayイベント / XP / AI返答 / Bump検知 / スラッシュコマンド | `discord.py` `motor` `google-genai` `aiohttp` |
| `batch/fetch_discord_logs.py` | GitHub Actions | 2h分のログをREST APIで取得 | `requests` |
| `batch/summarize.py` | GitHub Actions | Gemini要約生成 | `google-genai` |
| `batch/update_mongodb.py` | GitHub Actions | summariesコレクションにinsert | `pymongo` |
| `batch/post_summary.py` | GitHub Actions | 日報Embedを投稿 | `requests` `pymongo` |
| `batch/analyze_personality.py` | GitHub Actions | ブースター性格を二軸分析（口調＋文脈） | `google-genai` `pymongo` |
| `batch/analyze_nonbooster.py` | GitHub Actions | 非ブースターの簡易プロファイル生成 | `google-genai` `pymongo` |
| `batch/enrich_memories.py` | GitHub Actions | 過去要約からclaims/memories補完＋embedding付与 | `google-genai` `pymongo` |
| `batch/retro_summarize.py` | GitHub Actions | 任意の過去日の日報を作成 | 全部 |
| `batch/focus_summary.py` | GitHub Actions | 特定メンバー/キーワードの絞り込み要約 | 全部 |
| `market_report.py` | GitHub Actions | 平日16時に市場レポート+AIコメント | `yfinance` `matplotlib` `google-genai` |
| `ai_news_bot.py` | GitHub Actions | RSSからAIニュース要約を生成・投稿 | `feedparser` `google-genai` |
| `cleanup_bot.py` | GitHub Actions | 自己紹介ch自動整理（別トークン） | `discord.py` |

## MongoDB スキーマ

**Database**: `discord_bot_db`

| コレクション | キー | 主なフィールド | 書込元 | 読込元 |
|---|---|---|---|---|
| `users` | `_id` = Discord user ID | `xp`, `bump_count`, `streak_days`, `profile{}`, `simple_profile{}`, `butler_history[]`, `memories[]`（+embedding）, `claims[]`, `title`, `conv_count` | main.py, batch/analyze_* | main.py |
| `system` | `_id` = `"personality"` / `"nickname_map"` / bot_id | `value` / `map{}` / `last_bump_at` | main.py, batch/update_mongodb | main.py |
| `summaries` | 自動 | `summary`, `message_count`, `created_at`, `is_retro?`, `retro_date?` | batch/update_mongodb, batch/retro_summarize | main.py（返答時）, batch/analyze_* |

**インデックス**:
- `users.xp_desc`（`/top` ランキング用）
- `summaries.created_at_desc`（最新要約の判定はこの降順ソート）
- `memories_vector_index`（Atlas Vector Search・手動作成前提）

## スケジュール一覧（UTC基準）

| workflow | cron | JST換算 | 頻度 |
|---|---|---|---|
| `summarize.yml` | `0 */2 * * *` | 2時間毎 | 日12回 |
| `daily_tasks.yml` | `0 15 * * *` | JST 0:00 | 日1回 |
| `personality_analyze.yml` | `0 15 * * *` | JST 0:00 | 日1回（daily_tasksと重複⚠️） |
| `cleanup.yml` | `0 15 * * *` | JST 0:00 | 日1回 |
| `nikkei-report.yml` | `0 7 * * 1-5` | 平日16:00 | 週5回 |
| `ai_news.yml` | `0 1,4,7,10,13 * * *` | 3時間毎（朝〜夕方） | 日5回 |
| `retro_report.yml` | manual | - | オンデマンド |
| `focus_summary.yml` | manual | - | オンデマンド |

## ⚠️ 要注意事項

**workflow の重複**:
- `personality_analyze.yml` は `daily_tasks.yml` 内のステップと同じ処理を二重実行している

**token共有の注意**:
- `main.py` と batch スクリプトは同じ `DISCORD_BOT_TOKEN` を使う。
- バッチがGateway接続すると本番Botが切断されるため `batch/fetch_discord_logs.py` は REST API のみ使用必須。
- `cleanup_bot.py` のみ別Bot（`CLEANUP_BOT_TOKEN`）を使用。

**モデルID のハードコード**:
- `gemini-3.1-flash-lite-preview`, `gemma-4-31b-it`, `gemma-4-26b-a4b-it`, `gemini-embedding-001` 等が各ファイルに直書き。モデル差し替え時は grep で全置換が必要。

## 環境変数まとめ

| 変数名 | 用途 | 必須箇所 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | メインBotトークン | main.py, batch/*, market, ai_news |
| `CLEANUP_BOT_TOKEN` | 掃除Bot専用トークン | cleanup_bot.py |
| `MONGO_URL` / `MONGODB_URI` | MongoDB Atlas接続文字列（ **main.pyとbatchで変数名が違う** ） | 全体 |
| `GEMINI_API_KEY` | Gemini API | 全体 |
| `CHANNEL_ID` | 通知先ch | main.py |
| `HOME_GUILD_ID` | 唯一認可のサーバーID | main.py |
| `DISCORD_GUILD_ID` | batch用 guild ID | batch/* |
| `DISCORD_CHANNEL_IDS` | 取得対象ch（CSV・空なら全ch） | batch/fetch_discord_logs |
| `EXCLUDE_CHANNEL_IDS` | 除外ch（CSV） | batch/fetch_discord_logs |
| `SUMMARY_CHANNEL_ID` | 日報投稿先 | batch/post_summary, market |
| `DISCORD_WEBHOOK_URL` | マーケット投稿先 | market_report |
| `NEWS_WEBHOOK_URL` | ニュース投稿先 | ai_news_bot |
| `INTRO_CHANNEL_ID` | 自己紹介ch | cleanup_bot |
| `GITHUB_TOKEN`, `GITHUB_REPO` | workflow_dispatch 実行用（`/retroreport` `/focus`） | main.py |
| `PORT` | Render health server | main.py |
| `TARGET_DATE` / `FOCUS_*` | workflow_dispatch入力 | retro / focus |
