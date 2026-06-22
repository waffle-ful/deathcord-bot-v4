# 設定リファレンス（環境変数・ID・モデル・定数）

変数名を探すときに最初に見るページ。全プロジェクト横断の設定を 1 箇所に集約。

## 環境変数（全一覧）

### main.py（Render.com 常駐 Bot）

| 変数名 | 必須 | 使用箇所 | 説明 |
|--------|----|--------|-----|
| `DISCORD_BOT_TOKEN` | ✅ | L32 | Bot トークン（Gateway + REST） |
| `MONGO_URL` | ✅ | L33 | **⚠️ 名前が batch と違う**。MongoDB Atlas 接続 URI |
| `CHANNEL_ID` | ✅ | L34 | 通知チャンネル ID（`NOTIFY_CHANNEL_ID`）。`notification_task` が使用 |
| `GEMINI_API_KEY` | ✅ | L35 | Google Gemini API キー |
| `HOME_GUILD_ID` | ○ | L72 | 許可ギルド ID。デフォルト `1128769816820465766` |
| `PORT` | ○ | L26 | aiohttp ヘルスサーバー用。デフォルト `10000`。Render が自動設定 |
| `GITHUB_TOKEN` | ○ | L2611, L2672 | `/retroreport` `/focus` で workflow dispatch 用 |
| `GITHUB_REPO` | ○ | L2612, L2673 | 同上。`owner/repo` 形式 |

### batch/*.py（GitHub Actions）

| 変数名 | 必須 | 使用バッチ | 説明 |
|--------|----|----------|-----|
| `DISCORD_BOT_TOKEN` | ✅ | fetch_discord_logs, post_summary, retro_summarize, focus_summary | 本番と同一 Token（REST のみ利用） |
| `MONGODB_URI` | ✅ | ほぼ全バッチ | **⚠️ main.py の `MONGO_URL` と異名同義** |
| `GEMINI_API_KEY` | ✅ | summarize, analyze_*, enrich_memories, retro_summarize, focus_summary | Gemini API |
| `DISCORD_GUILD_ID` | ✅ | fetch_discord_logs, retro_summarize, focus_summary | 対象サーバー |
| `DISCORD_CHANNEL_IDS` | ○ | fetch_discord_logs, retro_summarize, focus_summary | カンマ区切り。空なら全ch |
| `EXCLUDE_CHANNEL_IDS` | ○ | 同上 | 除外 ch。NSFW は自動除外 |
| `SUMMARY_CHANNEL_ID` | ✅ | post_summary, retro_summarize, focus_summary | 日報投稿先 |
| `TARGET_DATE` | ✅ | retro_summarize | workflow_dispatch input（YYYY-MM-DD） |
| `FOCUS_TYPE` | ✅ | focus_summary | workflow_dispatch input（`member` or `keyword`） |
| `FOCUS_TARGET` | ✅ | focus_summary | メンバー ID or キーワード文字列 |
| `FOCUS_NAME` | ✅ | focus_summary | 表示名 |

### market_report.py

| 変数名 | 必須 | 使用箇所 | 説明 |
|--------|----|--------|-----|
| `DISCORD_WEBHOOK_URL` | ✅ | L39 | 日次レポート投稿先 Webhook |
| `DISCORD_BOT_TOKEN` | ○ | L43 | 月曜予想投票でメッセージ ID 取得。金曜集計では必須 |
| `GEMINI_API_KEY` | ✅ | L40 | AI コメント生成 |
| `MONGODB_URI` | ○ | L41 | 予想データ保存。未設定なら予想機能 skip |
| `SUMMARY_CHANNEL_ID` | ○ | L42 | 予想投票・結果投稿先 |

### ai_news_bot.py

| 変数名 | 必須 | 使用箇所 | 説明 |
|--------|----|--------|-----|
| `GEMINI_API_KEY` | ✅ | L10 | ニュース分析 |
| `NEWS_WEBHOOK_URL` | ✅ | L9 | ニュース投稿先 Webhook |

### cleanup_bot.py

| 変数名 | 必須 | 使用箇所 | 説明 |
|--------|----|--------|-----|
| `CLEANUP_BOT_TOKEN` | ✅ | L6 | **別 Bot Token**（main.py とは別の Bot） |
| `INTRO_CHANNEL_ID` | ✅ | L8 | 掃除対象チャンネル |

## 変数名の不整合 ⚠️

| main.py | batch / market | 統一すべきだが現状の運用 |
|---------|---------------|--------------------|
| `MONGO_URL` | `MONGODB_URI` | **両方を secrets に登録** する必要あり |
| `CHANNEL_ID` | — | main.py 専用。通知用 |
| — | `SUMMARY_CHANNEL_ID` | batch / market 専用。日報用 |
| — | `NEWS_WEBHOOK_URL` | ai_news.py 専用 |
| — | `DISCORD_WEBHOOK_URL` | market_report.py 専用 |

## Discord ID 一覧（ハードコード）

### ギルド

| ID | 名称 | 定義箇所 |
|----|------|--------|
| `1128769816820465766` | ホームギルド（デフォルト値） | main.py L72 |

### チャンネル

| ID | 用途 | 定義箇所 |
|----|------|--------|
| `1467851526252007651` | `GENERAL_CHANNEL_ID` ランキング・ランクアップ投稿 | main.py L71 |
| `1477343773251080433` | `BUTLER_CHANNEL_ID` ブースター専用メイドチャンネル | main.py L85 |

### ロール

#### ランクロール（19段階、L47-68）

| 行 | ランク名 | XP 閾値 | ロール ID |
|----|---------|--------|----------|
| L48 | アソシエイト | 200 | `1417840680994209915` |
| L49 | シニア | 500 | `1417840548374380576` |
| L50 | マネージャー | 1,000 | `1417842764535697531` |
| L51 | シニアマネージャー | 2,000 | `1417842979472670761` |
| L52 | エグゼクティブ | 4,000 | `1417843148893327380` |
| L53 | シニアエグゼクティブ | 7,000 | `1417843208058179644` |
| L54 | プラチナム | 12,000 | `1417843277612318731` |
| L55 | ルビー | 20,000 | `1417843313423290401` |
| L56 | サファイア | 32,000 | `1417844875574906962` |
| L57 | エメラルド | 50,000 | `1417845225149304894` |
| L58 | ダイヤモンド | 75,000 | `1417845526929608815` |
| L59 | エグゼクティブダイヤモンド | 108,000 | `1417845836360061009` |
| L60 | ダブルダイヤモンド | 150,000 | `1417846243769712700` |
| L61 | トリプルダイヤモンド | 200,000 | `1417846555079213166` |
| L62 | クラウン | 260,000 | `1417846850429386792` |
| L63 | パートナー | 330,000 | `1417847008198266982` |
| L64 | シニアパートナー | 410,000 | `1417847222589980692` |
| L65 | マネージングパートナー | 500,000 | `1482043661939245218` |
| L66 | プレジデント | 600,000 | `1482044038205931520` |

#### 特殊ロール

| ID | 用途 | 定義箇所 |
|----|------|--------|
| `1420309723273756704` | `BOOSTER_ROLE_ID` サーバーブースター | main.py L83 |
| `1417840244711100566` | `GRADUATE_REMOVE_ROLE_IDS` 卒業時削除ロール | main.py L80 |

### Bump Bot（検出対象、6 個）

| Bot ID | 名前 | クールダウン | キーワード | 定義箇所 |
|--------|------|-----------|----------|--------|
| `302050872383242240` | DISBOARD | 7200s | "表示順をアップしたよ", "Bump done" | main.py L90 |
| `761562078095867916` | ディス速 | 3600s | "をアップしたよ" | L91 |
| `1402811962211176488` | Dislist | 7200s | "あなたのサーバーを", "移動しました" | L92 |
| `850493201064132659` | Discord Cafe | 3600s | "表示順位を上げました" | L93 |
| `1240964440581603370` | Fortify | 3600s | "に移動しました", "掲載順位を更新しました" | L94 |
| `903541413298450462` | Dicoall | 3600s | "最上段に更新されました" | L95 |

## Gemini モデル使用箇所集計

### モデル ID インデックス

| モデル ID | 用途 | 使用箇所 |
|----------|-----|--------|
| `models/gemini-3.1-flash-lite-preview` | ユーザー対面の応答（メイン） | main.py L41 `MODEL_BOOSTER` |
| `models/gemma-4-31b-it` | 503 フォールバック + バッチ要約・分析 | main.py L42, batch/summarize L20, batch/analyze_personality L31, batch/analyze_nonbooster L28, batch/enrich_memories L28, batch/retro_summarize L23, batch/focus_summary L38 |
| `models/gemma-4-26b-a4b-it` | バックグラウンド処理（軽量） | main.py L43 `MODEL_GENERAL_FB`, batch/analyze_nonbooster L29, batch/enrich_memories L29, batch/retro_summarize L24, batch/focus_summary L39 |
| `models/gemma-3-27b-it` | 二次フォールバック・市場分析 | batch/summarize L21, batch/analyze_personality L32, market_report L44 `MODEL`, batch/focus_summary L339 |
| `models/gemini-embedding-001` | 記憶 embedding（全箇所で統一・3072次元） | main.py `_get_embedding`, batch/enrich_memories L30, batch/focus_summary（embed_content 呼び出し） |
| `models/gemini-2.5-flash-lite` | 検索グラウンディング | market_report L46 `MODEL_SEARCH` |
| `models/gemini-flash-lite-latest` | 検索グラウンディング エイリアス | market_report L47 `MODEL_SEARCH_ALT` |
| `models/gemini-2.5-flash` | AI ニュース分析 | ai_news_bot L68 |

### 呼び出し頻度（一日あたり推定）

| 頻度 | モデル | 用途 |
|------|-------|-----|
| 数百〜数千 | `gemini-3.1-flash-lite-preview` | メイド応答（ユーザー発言毎） |
| 数十〜数百 | `gemini-embedding-001` | 記憶保存毎 |
| ~12 | `gemma-4-31b-it` (summarize) | 2時間毎 |
| 数十 | `gemma-4-31b-it` (analyze_personality) | 日次バッチ |
| ~5 | `gemini-2.5-flash` | AI ニュース（3時間毎） |
| ~5 | `gemma-3-27b-it` / `gemini-2.5-flash-lite` | 市場レポート（平日） |

### ⚠️ 懸念

- **`preview` 付きモデルは Google が事前告知なく廃止する可能性**。実際に動かなくなったら即座に差し替え必要
- **`gemma-4-*` 系列は Gemma（オープンモデル）** を Google がホストしているもの。バージョニングが不安定
- モデル ID はコード内に直書き。**centralize された定数ファイルはない**

## XP 関連定数

| 定数 | 値 | 定義 | 用途 |
|------|---|-----|-----|
| `XP_COOLDOWN_SECONDS` | 60 | L1410 | 同一ユーザーの連続 XP 加算禁止秒数 |
| `_RATE_LIMIT_RPM` | 12 | L813 | Gemini API 呼び出し上限（1分） |
| `_QUEUE_MAX` | 5 | L1011 | メイド応答キュー最大待ち |
| `BUTLER_HISTORY_MAX` | 5 | L86 | 会話履歴のペア数（実際は × 2 で 10 件） |
| `BOOSTER_XP_MULTIPLIER` | 1.5 | L84 | ブースターの XP 倍率 |

### XP 獲得ルール（`calculate_xp_gain`, L1436-1448）

| 条件 | XP |
|------|---|
| 内容が空 or 直前と同じ | 0 |
| 連続10文字以上のスパム（`(.)\1{9,}`） | 1 |
| 長文（201文字〜） | 70 |
| 通常（15〜200文字） | 50 |
| 短文（〜14文字） | 30 |
| Bump 検出時 | +100（別経路） |

### 連続参加ボーナス

| 日数 | ボーナス |
|-----|--------|
| 3 日 | +100 XP |
| 7 日 | +300 XP |
| 30 日 | +1,000 XP |

（`STREAK_BONUSES` L79、新しい日の最初の発言時のみ）

### 招待ボーナス

| 招待数 | ボーナス |
|-------|--------|
| 1 人 | +200 XP |
| 3 人 | +500 XP |
| 5 人 | +1,000 XP |
| 10 人 | +2,000 XP |

（`INVITE_BONUSES`、`on_member_join` 周辺）

## 確率設定

| 定数 | 値 | 定義 | 用途 |
|------|---|-----|-----|
| `SCARY_CHANCE` | 0.0 | L288 | ホラー応答（**現在無効化**） |
| `MIMIC_CHANCE` | 0.0 | L289 | 自動ミミック（**現在無効化**） |
| `NB_TALK_CHANCE` | 0.005 | L292 | 非ブースター自発話しかけ（0.5%） |
| `NB_TALK_CHANCE_TOPIC` | 0.02 | L293 | 話題ワード検知時（2%） |

レート制限対策で多くが無効化されている。復活させる場合は [known-issues.md](./known-issues.md) の経緯を参照。

## コレクション名

MongoDB の database + collection 名：

| DB | コレクション | 使用 |
|----|-----------|-----|
| `discord_bot_db` | `users` | 全体 |
| `discord_bot_db` | `system` | 全体 |
| `discord_bot_db` | `summaries` | 全体 |
| `discord_bot_db` | `market_predictions` | market_report のみ |

**⚠️ 旧コレクション名** (`discord_bot` / `cache_records`) を使う古いコードが削除前に存在した。同じ DB を別名で触ってないか移行時は確認すること。

## GitHub Secrets（Actions 設定）

現在 workflow で必要とされる secret 一覧：

| Secret 名 | 使用 workflow | 補足 |
|----------|-------------|------|
| `DISCORD_BOT_TOKEN` | summarize, daily_tasks, nikkei-report, retro_report, focus_summary | 本番 Bot と同一 |
| `CLEANUP_BOT_TOKEN` | cleanup | 別 Bot Token |
| `MONGODB_URI` | summarize, daily_tasks, personality_analyze, nikkei-report, retro_report, focus_summary | |
| `GEMINI_API_KEY` | ほぼ全部 | |
| `DISCORD_GUILD_ID` | summarize, retro_report, focus_summary | |
| `DISCORD_CHANNEL_IDS` | 同上 | オプショナル（空なら全ch） |
| `EXCLUDE_CHANNEL_IDS` | 同上 | オプショナル |
| `SUMMARY_CHANNEL_ID` | daily_tasks, nikkei-report, retro_report, focus_summary | 日報投稿先 |
| `DISCORD_WEBHOOK_URL` | nikkei-report | 市場レポート Webhook |
| `NEWS_WEBHOOK_URL` | ai_news | AI ニュース Webhook |
| `INTRO_CHANNEL_ID` | cleanup | 自己紹介ch |

## Render.com 設定（デプロイ側）

`main.py` を動かす Render 側に必要な環境変数：

```
DISCORD_BOT_TOKEN   = ...
MONGO_URL           = mongodb+srv://...
CHANNEL_ID          = 1467851526252007651  # or 任意の通知ch
GEMINI_API_KEY      = ...
HOME_GUILD_ID       = 1128769816820465766  # or 任意のホームギルド
PORT                = 10000  # Render が自動設定するので省略可
GITHUB_TOKEN        = ghp_...  # /retroreport /focus 用、optional
GITHUB_REPO         = owner/finance  # 同上、optional
```

## Python バージョン

| 環境 | バージョン | 定義 |
|------|----------|-----|
| ローカル / 実行時 | 3.12.1 | `.python-version` |
| summarize, daily_tasks 系 | 3.12 | `.github/workflows/*.yml` |
| ai_news | 3.11 | `ai_news.yml` |
| nikkei-report (market) | 3.11 | `nikkei-report.yml` |
| cleanup | 3.10 | `cleanup.yml` |

**バラつきが大きい**。統一すべきだが、マーケット系と AI ニュース系は古い `pip install` が既に動いているので触らないのが無難。
