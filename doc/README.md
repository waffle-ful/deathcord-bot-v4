# ドキュメント目次

このフォルダは **finance Bot (空気くん)** の将来の修理・拡張に資する横断分析資料です。
**修正・機能追加を行う前に必ず該当するドキュメントを読むこと。**

## 読む順番

| # | ファイル | 読むタイミング |
|---|---------|--------------|
| 1 | [architecture.md](./architecture.md) | 全体像を把握したいとき・最初の1回 |
| 2 | [data-model.md](./data-model.md) | DB スキーマを触るとき・ユーザーデータを使う機能を足すとき |
| 3 | [main-bot.md](./main-bot.md) | `main.py` を修正するとき（関数一覧・状態・コマンド全網羅） |
| 4 | [batch-pipeline.md](./batch-pipeline.md) | GitHub Actions / `batch/*.py` を修正するとき |
| 5 | [prompts-and-personalities.md](./prompts-and-personalities.md) | AI 応答・人格を調整するとき |
| 6 | [config-reference.md](./config-reference.md) | 環境変数・ID・モデル名を探すとき（一覧リファレンス） |
| 7 | [extension-guide.md](./extension-guide.md) | 「〇〇機能を追加したい」と思ったとき |
| 8 | [known-issues.md](./known-issues.md) | トラブル発生時・動作が怪しいとき |

## このプロジェクトの要点（30秒版）

- **何**: 単一 Discord サーバー専用の多機能コミュニティ Bot（Japanese only）
- **本体**: `main.py` (3,100行) を Render.com の無料枠で常駐
- **バッチ**: GitHub Actions で 2 時間毎要約・日次分析・週次マーケット
- **AI**: Google Gemini（`gemini-3.1-flash-lite-preview` 中心、多モデル混在）
- **DB**: MongoDB Atlas（`discord_bot_db` / 3 コレクション）
- **特徴**: 6 人格切替メイド / XP・ランク / Bump 検知 / AI 日報 / 記憶 Vector Search

## 🚨 触る前に必ず把握すべき「実績のある事故」

以下は **過去に実際に BAN / クラッシュに至った** 案件。詳細は [known-issues.md](./known-issues.md) の🔴優先度高を参照：

- **項目 6**: `SCARY_CHANCE` / `MIMIC_CHANCE` の自動発火 → ループ → Cloudflare / Discord BAN実績あり。現在 `0.0` で封印中。**復活させる場合は事故再発リスク極大**
- **項目 7**: ログ取得バッチ (`fetch_discord_logs.py`) はサーバー成長時に Discord API 上限を超え、**本番 Bot ごと BAN される構造的リスク**。メンバー増える前に構造変更が必要
- **項目 5**: バッチで `discord.py` を `import` すると本番 Bot が Gateway から切断される。過去に発生済み

## 注意

- 行番号は分析時点（2026-04-24）のもの。コードが変われば当然ずれる。**重要な変更前には grep で確認すること**。
- モデル ID (`gemini-3.1-flash-lite-preview` など) は preview/alias で、Google 側の廃止で突然動かなくなるリスクあり。
- `main.py` は**単一ファイル設計**が意図的（モジュール分割すると `@client.tree.command` の登録順で事故る）。分割しない。
