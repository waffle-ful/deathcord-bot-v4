# 空気くん (finance bot)

多機能 Discord ボット。詳細は [`doc/`](doc/) を参照。

## 初期セットアップ（新環境構築時）

### 1. 依存パッケージのインストール

```bash
pip install -r requirements.txt          # main.py (Render)
pip install -r requirements-batch.txt    # batch/*.py (GitHub Actions)
pip install -r requirements-market.txt   # market_report.py
```

### 2. MongoDB Atlas — Vector Search インデックスは不要

メモリ検索機能（`search_memories`）は **main.py 内の in-Python cosine** に移行済み。
保存済み記憶の embedding（`gemini-embedding-001`）を取得し、メッセージとの
コサイン類似度を Python 側で計算して関連記憶を選ぶため、**Atlas の Vector Search
インデックスは不要**（`setup_mongo_index.py` は非推奨。実行しなくてよい）。

旧 `memories_vector_index` は残っていても無害（参照されない）。

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
