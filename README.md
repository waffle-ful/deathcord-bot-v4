# 空気くん (finance bot)

多機能 Discord ボット。詳細は [`doc/`](doc/) を参照。

## 初期セットアップ（新環境構築時）

### 1. 依存パッケージのインストール

```bash
pip install -r requirements.txt          # main.py (Render)
pip install -r requirements-batch.txt    # batch/*.py (GitHub Actions)
pip install -r requirements-market.txt   # market_report.py
```

### 2. MongoDB Atlas — Vector Search インデックス作成

メモリ検索機能（`search_memories`）は Atlas Vector Search インデックスが必要。
**手動作成の代わりにスクリプトで一発作成できる：**

```bash
MONGODB_URI=<your_uri> python setup_mongo_index.py
```

- 対象コレクション: `discord_bot_db.users`
- インデックス名: `memories_vector_index`
- 次元数: 768（`gemini-embedding-001` の出力次元）
- インデックスが ACTIVE になるまで Atlas UI で数分かかる

インデックスが存在しない場合でも bot は動作するが、
メモリ検索が fallback（最新 N 件）になり関連記憶がヒットしにくくなる。

### 3. 環境変数

| 変数名 | 用途 | 設定先 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Bot トークン | Render + GitHub Secrets |
| `MONGO_URL` / `MONGODB_URI` | MongoDB URI | Render (`MONGO_URL`) + GitHub Secrets (`MONGODB_URI`) |
| `GEMINI_API_KEY` | Gemini API キー | Render + GitHub Secrets |
| `CHANNEL_ID` | 通知チャンネル ID | Render |
| `HOME_GUILD_ID` | ホームサーバー ID | Render |
| `SUMMARY_CHANNEL_ID` | 日報投稿チャンネル ID | GitHub Secrets |
| `DISCORD_WEBHOOK_URL` | Webhook URL | GitHub Secrets |
| `CLEANUP_BOT_TOKEN` | 掃除 Bot トークン | GitHub Secrets |
| `PORT` | ヘルスサーバーポート | Render (自動設定) |
