# アーキテクチャ全体像

## システム構成

本プロジェクトは **3 つの実行環境** に分かれる：

```
┌──────────────────┐   ┌────────────────────┐   ┌───────────────────┐
│ Render.com       │   │ GitHub Actions     │   │ MongoDB Atlas     │
│ (常駐・無料枠)    │   │ (cron スケジュール) │   │ (永続ストレージ)    │
│                  │   │                    │   │                   │
│  main.py         │──▶│  batch/*.py (10個) │◀─▶│  discord_bot_db   │
│  (3100行)        │   │  market_report.py  │   │   ├─ users        │
│  ├ Discord       │   │  ai_news_bot.py    │   │   ├─ system       │
│  │  Gateway      │   │  cleanup_bot.py    │   │   └─ summaries    │
│  ├ MongoDB Motor │   │                    │   │                   │
│  ├ Gemini API    │   │  8 個の workflow   │   │  (vector index)   │
│  └ aiohttp :PORT │   │                    │   │                   │
└──────────────────┘   └────────────────────┘   └───────────────────┘
         │                       │                        ▲
         │                       ▼                        │
         │             ┌──────────────────┐               │
         └────────────▶│ Discord API      │               │
                       │ (Gateway + REST) │               │
                       └──────────────────┘               │
                                                          │
                              ┌─────────────────────┐     │
                              │ Google Gemini API   │─────┘
                              │ (多モデル使い分け)    │
                              └─────────────────────┘
```

## コンポーネント責務

### Render 常駐 Bot（`main.py`）

**唯一のイベントループ**。Discord Gateway を掴み続け、以下を処理：

| 責務 | 主要ハンドラ | 実装 |
|------|------------|-----|
| XP 加算・連続参加ボーナス | `on_message` | `calculate_xp_gain`, `STREAK_BONUSES` |
| ロール自動付与（19段階ランク） | `on_message` → `update_member_role` | `RANK_STAGES` |
| Bump 検知（6 Bot 対応） | `on_message`, `on_raw_message_edit`, **`on_interaction`** | `BOT_CONFIG`, `check_bump*` |
| AI メイド応答（6人格） | `on_message`（メンション）/ `/maid` | `_maid_queue_worker` + `_run_ai_booster` |
| 長期記憶・プロフィール蓄積 | バックグラウンドタスク | `extract_and_save_profile`, `_extract_claims_and_memories` |
| ミミック機能 | `/mimic` コマンド | Webhook で偽装投稿（5分間） |
| 招待追跡・招待ボーナス | `on_member_join` + 起動時スナップショット | `_invite_snapshot` |
| 週次ランキング投稿 | `weekly_ranking_task`（10分ポーリング） | 日曜 JST 正午 |
| Bump 忘れ通知 | `notification_task` | 10分毎チェック |
| `/retroreport` `/focus` | GitHub API で workflow_dispatch | REST でトリガー |

**起動順序**:
1. `aiohttp` ヘルスサーバーを `PORT` で立ち上げ（Render のポートチェック対策）
2. `client.start(TOKEN)` — Gateway 接続
3. `setup_hook` でスラッシュコマンドを `HOME_GUILD_ID` に同期（**グローバルでなくギルド**）
4. 429 発生時は `close() → 指数バックオフ → 再接続`（最大1h）

### GitHub Actions バッチ（定期実行）

| workflow | cron (UTC) | JST | 呼び出しスクリプト | 処理 |
|----------|-----------|-----|-----------------|------|
| `summarize.yml` | `0 */2 * * *` | 2h 毎 | fetch→summarize→update_mongodb | 2h 分ログ要約→ MongoDB 保存 |
| `daily_tasks.yml` | `0 15 * * *` | 0:00 | analyze_personality → analyze_nonbooster → post_summary → enrich_memories | 性格分析・日報投稿・記憶補完 |
| `personality_analyze.yml` | `0 15 * * *` | 0:00 | analyze_personality のみ | **daily_tasks と重複⚠️** |
| `nikkei-report.yml` | `0 7 * * 1-5` | 16:00 平日 | market_report.py | 市場レポート + 月曜予想投票 + 金曜集計 |
| `ai_news.yml` | `0 1,4,7,10,13 * * *` | 10,13,16,19,22時 | ai_news_bot.py | Yahoo RSS 要約 + `last_top_url.txt` コミット |
| `cleanup.yml` | `0 15 * * *` | 0:00 | cleanup_bot.py | 脱退者の自己紹介削除（**別 Bot Token**） |
| `retro_report.yml` | manual | - | batch/retro_summarize.py | 指定日の日報を遡及生成 |
| `focus_summary.yml` | manual | - | batch/focus_summary.py | メンバー/キーワード絞り込み要約 |

**絶対ルール**: バッチは **Discord Gateway に接続してはならない**。本番 Bot と同一 Token を Gateway で繋ぐと本番 Bot が切断される。`batch/fetch_discord_logs.py` は REST API のみを使用するよう明示的に書き直されている（`requirements-batch.txt` にも `discord.py` を含めない）。

### 外部依存

| 依存先 | 用途 | 認証 |
|-------|-----|------|
| Discord API | Gateway + REST | `DISCORD_BOT_TOKEN` / `CLEANUP_BOT_TOKEN` |
| MongoDB Atlas | 永続 DB (3 collections + vector index) | `MONGO_URL` / `MONGODB_URI`（**名前揺れ注意**） |
| Google Gemini API | LLM 全般（7+ モデル使い分け） | `GEMINI_API_KEY` |
| GitHub Actions API | `/retroreport` `/focus` で workflow dispatch | `GITHUB_TOKEN` + `GITHUB_REPO` |
| yfinance | 市場データ | なし（無認証） |
| Yahoo ニュース RSS | ニュース取得 | なし |

## 3 つの主要データフロー

### ① メイド応答のホット経路（ユーザー体験を決める）

```
[Discord user @mentions] 
    ↓ on_message
maid_respond_queued  ──▶  _maid_queue (max 5)  ──▶  _maid_queue_worker (single)
                                                        ↓
                                          _maid_respond_inner
                                                        ↓
                          [直近12メッセージ] ─→ _build_prompt ←─ [MongoDB]
                                                        ↓       users.profile
                                                  _run_ai_booster  users.memories (vector search)
                                                        ↓        users.claims (angry人格のみ)
                                            Gemini 3.1 flash-lite  system.nickname_map
                                               (503→ gemma-4-31b)  summaries (created_at降順)
                                                        ↓
                                    typing(rate_wait+delay) ＋ reply
                                                        ↓
                                [async] extract_and_save_profile
                                [async] _extract_claims_and_memories
                                [async] 30発言毎 _analyze_nonbooster_realtime
```

**レート制限は行動的に隠蔽**: 12 RPM 上限に近づくと `typing()` の表示時間を最大 20 秒まで引き延ばすことで、ユーザーには「ゆっくり返事している」ように見せる。技術的なエラー返しはしない。

### ② 日報パイプライン（2 時間毎）

```
GitHub Actions (summarize.yml, 2h毎)
    ↓
fetch_discord_logs.py           ──▶ /tmp/logs.json      (REST API, Gateway禁止)
    ↓
summarize.py                    ──▶ /tmp/summary_result.json  (gemma-4-31b-it)
    │  └─ nickname_map も抽出（## ニックネーム セクション）
    ↓
update_mongodb.py               ──▶ summaries.insert（最新は created_at 降順で判定）
    │                           ──▶ system.nickname_map (merge, 既存優先)
    │
    (daily JST 0:00, daily_tasks.yml)
    │
    ├→ analyze_personality.py   ──▶ users.profile (ブースター, conv_count>=10)
    ├→ analyze_nonbooster.py    ──▶ users.simple_profile (非ブースター)
    ├→ post_summary.py          ──▶ Discord Embed投稿 (SUMMARY_CHANNEL_ID)
    └→ enrich_memories.py       ──▶ users.claims, users.memories (+embedding)
```

### ③ 手動トリガー（Bot → GitHub Actions）

```
Discord ユーザー
    │  /retroreport 2026-03-01
    ↓
main.py retroreport_cmd
    ↓
MongoDB summaries に該当 retro_date があれば再投稿して終了
    ↓ なければ
POST https://api.github.com/.../retro_report.yml/dispatches  (GITHUB_TOKEN)
    ↓
GitHub Actions: retro_summarize.py
    ├─ REST で該当日のログ取得
    ├─ 前後2日分の要約を文脈として読み込み
    ├─ Gemini で遡及要約生成
    ├─ MongoDB summaries に is_retro=True で保存
    └─ Discord に Embed 投稿
```

`/focus` も同じパターン（`focus_summary.yml`）。GitHub API 呼び出しは main.py 内部で `aiohttp` を使用。

## 設計上の重要な決定

### なぜ `main.py` は単一ファイルなのか

- `@client.tree.command` デコレータは**インポート時**にコマンドツリーへ登録される
- モジュール分割すると import 順で登録漏れが起きやすい
- 再起動時の `client` 再生成でコマンドが消失する事故を防ぐため、client はグローバル唯一とし、429 時は `close() → reconnect` のみ（`_main` エントリ）
- 結果: 3100 行の巨大ファイルだが**単一モジュールが正**

### なぜバッチは Gateway 禁止なのか

- Discord は 1 つの Bot Token につき Gateway 接続を 1 つしか許さない
- バッチで `discord.py` の `client.start()` を呼ぶと、本番 main.py が Gateway から蹴られる
- 過去に `batch/fetch_discord_logs.py` が `discord.py` を使っていて本番を落とした履歴あり
- 対策: REST API (`requests`) のみで Discord を叩く。`requirements-batch.txt` に `discord.py` を入れない
- `cleanup_bot.py` だけは別 Token (`CLEANUP_BOT_TOKEN`) を使うので `discord.py` で OK

### なぜレート制限を技術エラーでなく遅延で隠すのか

- Gemini 3.1 flash-lite preview の RPM 上限が 15 前後（プレビュー モデルは TPM/RPM が低い）
- 「レート超過で返事できませんでした」というユーザー体験は致命的
- 代替: `_rate_timestamps` で過去 60 秒の呼び出し数を数え、`typing()` を最大 20s 引き延ばし
- キュー（`_maid_queue`, max 5）で同時応答を直列化。溢れたら**静かに捨てる**（ユーザーには無応答に見えるが、エラーメッセージよりマシ）

### なぜ human-like な遅延があるのか

- メイド AI は「キャラクター」なので即レスは不自然
- `_typing_delay()` は文字数に比例（1〜4秒）、レート待機と加算
- `random.uniform(0.5, 1.5)` のジッターでテンポを人間化

## 主要コンポーネント間の契約

以下は**変更すると壊れる重要な相互依存**:

| 契約 | 書き手 | 読み手 | 壊れると何が起きるか |
|------|-------|-------|-----------------|
| 最新要約は `created_at` 降順ソートで選択（`is_retro`/`retro_date` 除外） | batch/update_mongodb.py | main.py `get_latest_summary` | 並びが壊れる/遡及除外漏れ → 誤った要約注入・応答品質低下（known-issue #8 は 2026-06-06 にフラグ廃止で解消） |
| `summaries.summary` の `##` セクション区切り | batch/summarize.py | main.py `extract_summary_sections`, post_summary.py | セクション分割が壊れ、プロンプトに要約が入らない |
| `system.nickname_map.map` の merge 優先順（手動 > AI） | batch/update_mongodb.py L56 | main.py `get_nickname_map` | AI の誤検出が手動登録を上書きし、人物識別が狂う |
| `users.memories[].embedding` のモデル/次元の統一 | main.py, batch/enrich_memories.py, batch/focus_summary.py | main.py `search_memories`（in-Python cosine） | クエリと記憶で embedding モデルが違うと次元不一致でその記憶がスキップされ想起精度が落ちる（全箇所 `gemini-embedding-001` 統一が前提） |
| `RANK_STAGES` の昇順 | main.py 定数 | `get_rank_info`, `update_member_role` | 降順で挿入すると全員のランクが壊れる |
| `BOT_CONFIG` キーと Bot の user_id 一致 | main.py 定数 | `check_bump` | ID 間違えると Bump 検知がシカト |
| `HOME_GUILD_ID` が有効なギルド | 環境変数 | `setup_hook` でコマンド sync | 別ギルドに登録される / sync 失敗 |
| Discord Token が main.py・batch で同一（cleanup 除く） | 環境変数 | 全体 | **Gateway 二重接続で本番切断** |

## 参考

- モジュール個別詳細: [main-bot.md](./main-bot.md), [batch-pipeline.md](./batch-pipeline.md)
- DB スキーマ: [data-model.md](./data-model.md)
- 定数・環境変数: [config-reference.md](./config-reference.md)
