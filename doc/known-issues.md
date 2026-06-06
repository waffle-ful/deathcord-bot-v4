# 既知の問題・技術的負債・罠

トラブル対応・リファクタ検討時に最初に読む。

## 🔴 優先度高（実害が出ている / 出る可能性）

### 1. `personality_analyze.yml` と `daily_tasks.yml` の重複実行（🚨 daily_tasks タイムアウトの直接原因）

> ✅ **2026-04-25 対応済み**（commit `4cac7e2`, `5a747ce`）

**現象**: 両者とも cron `0 15 * * *`（JST 0:00）で `analyze_personality.py` を走らせる

**実害（2026-04-24 確認）**:
```
The job has exceeded the maximum execution time of 30m0s  ← daily_tasks がタイムアウト
expected 'packfile'                                        ← タイムアウトでランナー強制終了
RPC failed; HTTP 500 curl 22                               ← 後始末の git 処理が失敗
```
- `analyze_personality.py` が UTC 15:00 に **2プロセス同時実行** される
- Gemini API レート制限を取り合い → 双方が 503/リトライを繰り返す → 実行時間が膨張
- `daily_tasks.yml` の `timeout-minutes: 30` を超過し、後続の `post_summary.py`・`enrich_memories.py` が**一切実行されない**（日報投稿・長期記憶補完が毎日失敗している）
- MongoDB への同時書き込みで稀にレースコンディション

**影響の連鎖**:
```
personality_analyze.yml (同時起動)
        ↓
analyze_personality.py × 2 が Gemini API を取り合う
        ↓
daily_tasks.yml が 30 分でタイムアウト
        ↓
post_summary.py・enrich_memories.py が未実行
        ↓
日報投稿なし・長期記憶補完なし（毎日）
```

**実施した対応**:
- `personality_analyze.yml` の schedule を無効化（`4cac7e2`）
- `analyze_personality.py` / `enrich_memories.py` の `fetch_summaries` に `limit=30` を追加し最新30件（≈2.5日分）に絞ることでプロンプトの肥大化を防止
- `analyze_personality.py` のフォールバックモデルを `gemma-3-27b-it` → `gemma-4-26b-a4b-it` に変更（busy 503 時に同世代へ退避）
- `enrich_memories.py` の埋め込みモデルを `text-embedding-004` → `gemini-embedding-001` に修正（main.py と同一ベクトル空間に統一）
- `daily_tasks.yml` の `timeout-minutes` を 30 → 60 に拡張

### 2. Gemini `preview` モデルの廃止リスク

> ✅ **2026-04-25 対応済み**（commit `1a8c605`, `a103e7d`）

**現象**: `gemini-3.1-flash-lite-preview` は preview 指定モデル。Google は予告なく廃止する

**影響**:
- ある日突然メイドが「（メイドは今、席を外しております…）」で固定化
- ログには `404` / `not found` 系のエラー

**実施した対応**:
- `MODEL_BOOSTER` を `gemini-3.1-flash-lite-preview` → `gemma-4-31b-it` に変更
- `MODEL_FALLBACK` を `gemma-4-31b-it` → `gemma-4-26b-a4b-it` に変更（busy 503 時に同世代・別インスタンスへ退避）
- gemma-4 は TPM 無制限のため、プロンプトへ読み込む情報量の制約が解消
- gemma-4 は thinking 機能自体が非対応（`ThinkingConfig` を渡すと 400 INVALID_ARGUMENT）
- 応答速度は `gemma-4-26b-a4b-it`（MoE・実推論 4B 相当）をメインにすることで改善。`gemma-4-31b-it` は 503 時フォールバック

**対応**:
- 定期的に `/listmodels` コマンドで利用可能モデルを確認
- フォールバックチェーンの 2 段目 `gemma-4-31b-it` は常に動く状態を維持
- 切り替え時は `MODEL_BOOSTER` の値を 1 箇所変えるだけ（main.py L41）

**一方で対策不十分な箇所**:
- `batch/*.py` 内の `gemma-4-31b-it`, `gemma-3-27b-it` もハードコード
- モデル ID の集中管理（constants ファイル）が欲しい

### 3. MongoDB インデックス `memories_vector_index` の手動作成依存

> ✅ **2026-04-25 対応済み**（commit `094402c`）

**現象**: Atlas Vector Search のインデックスは UI から手動作成が必要。コード側にセットアップスクリプトがない

**影響**:
- 新環境に移行・再構築時に忘れると `search_memories` が常に fallback 経路（最新 N 件）になる
- ユーザーには静かに劣化（エラーは出ない・`[WARN] vector search` ログのみ）

**実施した対応**:
- `setup_mongo_index.py` を作成。`MONGODB_URI=<uri> python setup_mongo_index.py` の1コマンドで Atlas に `memories_vector_index` を自動作成
- `pymongo.operations.SearchIndexModel` を使用。既存インデックスがあればスキップ
- `README.md` に初期セットアップ手順として記載

**手動作成手順（Atlas UI）**:
```
Database → Collection(users) → Search Indexes → Create Search Index → Vector Search
  fields: [{ "type": "vector", "path": "memories.embedding", "numDimensions": 768, "similarity": "cosine" }]
  name: memories_vector_index
```
（`gemini-embedding-001` が 768 次元を返すと想定）

### 4. `MONGO_URL` と `MONGODB_URI` の変数名揺れ

> ✅ **2026-04-25 対応済み**（commit `094402c`）

**現象**:
- main.py は `MONGO_URL`
- batch/ と market は `MONGODB_URI`

**影響**:
- Render と GitHub Actions の両方に別名で同じ URI を登録する必要がある
- どちらか片方で変更したときに同期を忘れがち

**実施した対応**:
- `main.py` L33 を `MONGO_URL = os.environ.get("MONGO_URL") or os.environ.get("MONGODB_URI")` に変更
- どちらの変数名で設定されていても動作するようになった
- 長期的な `MONGODB_URI` への完全統一は未対応（Render 側の変数名変更が必要なため）

### 5. Gateway 二重接続による本番切断

**現象**: batch スクリプトで `discord.py` をインポートして `client.start()` すると、同一 Token が既に Gateway 接続している本番 Bot を蹴り落とす

**過去の事故**: `batch/fetch_discord_logs.py` が旧実装で `discord.py` を使っていて、バッチ実行毎に本番 Bot が切れていた

**現状の対策**:
- `requirements-batch.txt` に `discord.py` を含めない
- `batch/fetch_discord_logs.py` は `requests` で REST のみ
- 各 workflow YAML のコメントに「キャッシュ無効化」メモ

### 6. `SCARY_CHANCE` / `MIMIC_CHANCE` 発動による Cloudflare / Discord BAN（🚨 実績あり）

**現象**: ホラー応答（`SCARY_CHANCE`）または自然発生ミミック（`MIMIC_CHANCE`）が発火すると、**どこかでループが発生**し、短時間に大量の Discord / Gemini API 呼び出しが連鎖する。結果、Cloudflare レベルで Bot が IP ブロック、および Discord からアカウント BAN を受ける

**過去の事故**: 実際に稼働中に両方が発火して BAN を食らった履歴あり。緊急対応として `main.py` L288-289 で `SCARY_CHANCE = 0.0` / `MIMIC_CHANCE = 0.0` に設定して機能停止している

**現状**:
- 自動発火経路は封印 → **2026-06-06（commit `e4da578`）で死にコードごと削除済み**（項目 20 参照）。`SCARY_CHANCE`/`MIMIC_CHANCE`/`SCARY_MESSAGES`/`MIMIC_PROMPT_TEMPLATE`/`trigger_mimic` は repo から消えた
- 手動発火（`/mimic` コマンド）は安全（単発・5分タイマー・Webhook 偽装）なので維持。共有ヘルパ（`_send_mimic` 等）は手動経路が使うため温存
- ⚠️ 自動発火を**復活させる場合**は下記「必須対策」を満たした上で新規実装すること（旧コードはもう無いので参照不可）

**未解明の点**:
- ループの発生箇所が特定できていない
- `trigger_mimic` が自分自身の出力に反応してメンションループしている可能性
- `_send_mimic` の Webhook 投稿が `on_message` に拾われて再発火している可能性
- Cloudflare レベルで切られているので、ログ収集自体が困難

**復活させる場合の必須対策**:
1. 発火トリガーに **Bot / Webhook メッセージを確実に除外**（`message.author.bot`, `message.webhook_id` 両方チェック）
2. 同一チャンネルでの連続発火を強く制限（cooldown ≥ 10 分 / チャンネル）
3. **再帰防止フラグ**: 処理中チャンネルを set で保持し、処理完了まで次を発火させない
4. 動作前にまず**サンドボックステスト**（dry-run モード）で観測
5. `CLOUDFLARE BAN` 時の復旧手順をドキュメント化（Discord サポート / Cloudflare IP 変更）

**対応優先度**: 現状の封印で実害なし。**無理に復活させる必要はない**。次善策の「死んだコード削除」は **2026-06-06 完了**（項目 20）

### 7. サーバー成長時のログ取得爆発（🚨 スケール限界・構造的問題）

**現象**: 性格分析パイプラインは `fetch_discord_logs.py` で 2 時間毎に**全テキストチャンネル + アクティブスレッドの 2 時間分**のメッセージを REST で取得している。メンバー数・チャンネル数・発言量が増えると API 呼び出し数が線形以上に増加し、**Discord から BAN される確率が上昇**する

**影響の連鎖**:
```
メンバー増
  ↓
チャンネル数増・発言数増
  ↓
fetch_discord_logs.py の REST API 呼び出し回数増
 （全ch × ページネーション × アクティブスレッド分）
  ↓
Discord のレート制限を超過
  ↓
Cloudflare / Discord から IP / Token BAN
  ↓
本番 Bot (main.py) まで巻き添えで死ぬ
  （main.py と同一 Token なので、一度 BAN されると全機能停止）
```

**現状のガード（2026-06-06 追記: 既に3重で有界）**:
- `api_get` に **A: 先手スロットリング**（`X-RateLimit-Remaining<=1` で先に sleep, L60-）、**B: per-channel 上限**（`MAX_MSGS_PER_CHANNEL=500`, L151）、**C: グローバル上限**（`MAX_TOTAL_API_CALLS=300`, L50-52 で `api_get` 入口で打ち切り）が実装済み。BAN爆発リスクは既に有界。
- ※ 旧記述「絶対数の上限は設けていない」は**誤り（解消済み）**。C のグローバル上限が絶対数の天井。

**残る脆弱性**:
- 4時間毎の実行（2026-06-06 に 2h→4h へスローダウン） → 1日6回 × 全ch fetch → 小規模では OK。グローバル上限 300 に当たると要約が部分欠落するため、ch 数の増加で**早期打ち切りが常態化**し得る（その場合は対象ch絞り込み or 上限引き上げを検討）。
- `retro_summarize.py` / `focus_summary.py` も同じロジックを使うため、手動実行が重なると危険

**構造を変える必要性（短中期）**:

1. **本番 main.py が既に見ているメッセージをキャプチャして MongoDB に蓄積**
   - `on_message` で発言を `messages` コレクション（仮称）に直接保存
   - 2 時間毎のバッチは MongoDB から読む → Discord API 呼び出し **不要**
   - 既存の `butler_history` と統合する形が自然
   - 代償: MongoDB ストレージが増える（Atlas 無料枠 512MB を超える可能性）

2. **対象チャンネルを明示的に絞る**
   - 現在 `DISCORD_CHANNEL_IDS` 環境変数が空だと「全ch」。成長したら**必須化**
   - 雑談・重要チャンネルのみに限定
   - `EXCLUDE_CHANNEL_IDS` ではなくインクルード方式へ

3. **要約生成のインクリメンタル化**
   - 現状: 2 時間分を毎回フルで再取得
   - 改善: 「前回の `fetched_at` 以降」だけ取得（差分取得）
   - コード変更: `after_snowflake` を DB の前回値から読む

4. **バッチと本番の Token 分離**
   - 別 Bot を作ってバッチ専用 Token を発行（`cleanup_bot.py` と同じパターン）
   - BAN されてもバッチだけが止まる・本番 Bot は生存
   - ただし Bot 招待権限と Intent 設定が必要で運用コスト増

**構造を変える必要性（長期）**:

5. **性格分析をサーバー全体バッチ → ユーザー個別 Stream 処理へ移行**
   - 現状: `analyze_personality.py` は全ブースターをループ（conv_count>=10）
   - 改善: main.py `on_message` 経由で N 発言毎に該当ユーザーのみ分析（`_analyze_nonbooster_realtime` と同じパターン）
   - 既に非ブースター向けにはこの方式（L1817 の 30発言毎）。ブースターにも拡張する
   - 利点: バッチ時の集中アクセスがなくなる

6. **要約の粒度を `## セクション` 単位で差分にする**
   - 現状: 2 時間毎に要約全体を再生成
   - 改善: 前の要約を「先行文脈」として与え、差分だけ追記させる
   - モデル呼び出しのトークン数削減

**当面の緊急対策**:

> ✅ **2026-06-06 一部対応済み**（cron スローダウン・HOURS_BACK 整合）

- ✅ 2時間毎 cron を 4 時間毎に（`summarize.yml` を `0 */4 * * *`）スローダウン
- ✅ `HOURS_BACK` を env 化し既定 `5`（4h cron + 1h 重ね）に変更。実行スキップ/遅延時の最大1h取りこぼしを吸収。スナップショット要約なので隣接要約の重複は無害
- ⏳ **要・手動対応**: `EXCLUDE_CHANNEL_IDS` secret に botスパム/通知専用 ch の ID を追加（配線は済・値が空。どの ch を除外するかは運用者判断のため GitHub Secrets 側で設定する）

**監視すべきシグナル**:
- `fetch_discord_logs.py` の実行時間が急増（~15min → 30min+）
- Discord REST の 429 `[fetch] Rate limited. Waiting Xs` ログ頻度
- GitHub Actions の `summarize.yml` が timeout-minutes: 15 に当たる
- Cloudflare CAPTCHA ページが返ってくるようになる（`resp.status_code` 異常値）

**⚠️ 重要な制約**: この問題を放置したまま成長させると、**ある日突然本番 Bot ごと BAN されて復旧困難**。成長フェーズに入る前に構造変更を始めるべき

**再発防止**: 新規バッチ追加時に必ず [extension-guide.md](./extension-guide.md) の「新しいバッチスクリプトを追加する」を参照させる

---

## 🟡 優先度中（動くが不健全）

### 8. `summaries.is_latest=True` が複数件になる可能性

> ✅ **2026-06-06 対応済み**（`is_latest` フラグ廃止 → created_at 降順ソートへ）

**原因（旧設計）**: `update_mongodb.py` の操作が以下の 2 手順（アトミックでない）
```python
col.update_many({"is_latest": True}, {"$set": {"is_latest": False}})  # ①
col.insert_one(new_record_with_is_latest=True)                         # ②
```
①と②の間にクラッシュすると、新しい latest なしで古い全件 False になる（次の実行で正常化）。
①が失敗すると複数件 True が残り、`find_one({"is_latest": True})` がどちらか 1 つをランダムに返していた。

**実施した対応**: `is_latest` フラグを**完全廃止**し、最新判定を `created_at` 降順ソートに一本化。
- **書き手**: `update_mongodb.py` から `update_many` 反転・`is_latest` index 作成・新レコードの `is_latest` を削除。`retro_summarize.py` の `is_latest: False` も除去
- **読み手**（3箇所）: `main.get_latest_summary` / `summarystatus_cmd` / `post_summary.fetch_latest_summary` を
  `find_one({"summary": {"$exists": True}, "is_retro": {"$ne": True}, "retro_date": {"$exists": False}}, sort=[("created_at", -1)])` に統一
- **retro 除外が必須**: retro 要約は `created_at` が対象日 12:00 UTC のため、当日午前に `/retroreport` を実行すると通常要約より新しく見える。`is_retro`/`retro_date` の二重除外でガード（古い retro doc が `is_retro` 未保持でも `retro_date` で捕捉）
- **前提**: `created_at` は UTC isoformat 文字列。辞書順=時系列順が成り立つのは全書き手が UTC（`+00:00`）で書くため。**非 UTC で書く writer を追加するとソートが壊れる**ので注意
- **互換**: 既存 DB の古い doc に残る `is_latest` フィールドは無害（誰も読まない）。マイグレーション不要
- アトミック性向上＋競合消滅。created_at index がソートを直接賄うため `find_one({"is_latest": True})` より効率も改善

### 9. `nickname_map` の merge 挙動（AI 誤検出の上書き不可）

**現象**: AI が誤った対応（例: `"アホ": "本名"`）を検出すると、`system.nickname_map.map` に残り続ける

**対応**:
- `/removenick` コマンドで削除（L2741）
- 管理者が定期的に `/listnick` で確認

**改善案**:
- AI 検出分と手動登録分を別ドキュメントで持つ
- AI 分に TTL（2週間で自動消去）を設定

### 10. `_processed_bump_ids` のメモリ管理

**現象**: in-memory set で Bump 重複検出（L1924）。500 件超で古い半分を drop

**影響**:
- Bot 再起動で全消失 → 再起動直後に編集イベントから重複処理が起きる可能性
- 実際には再起動直後に古い編集イベントが来る確率は低いので実害は軽微

**対応案**: 必要なら Redis や MongoDB に TTL=1h で保存

### 11. `_mimic_sessions` の自動終了タイマー重複

**現象**: `/mimic` を連打してセッションを上書きすると、最初の `_auto_end` タスクは残り続ける（`_mimic_sessions` を `pop` するが `return` するだけ）

**影響**: 実害なし（`pop` で None が返ってくるので処理 skip されるだけ）だが、タスクリークにはなっている

**対応**: コード L2495 以下の `_auto_end` を cancel する仕組みを追加

### 12. `notification_task` の 10 分間隔

**現象**: `notification_task` は 10 分毎に全 Bump Bot を走査してクールダウン経過を通知（L3057）。Discord レート制限でメッセージ送信が遅れると、10 分以上の間隔になる

**影響**: Bump 忘れ通知が最大 10 分遅れ（許容範囲）

---

## 🟢 優先度低（設計上の歪みだが運用上問題ない）

### 13. `main.py` の 3,100 行単一ファイル

**意図的な設計**: モジュール分割するとコマンド登録の import 順で事故る（[architecture.md](./architecture.md) 参照）

**デメリット**:
- 編集時にコンフリクトしやすい（複数人開発には不向き）
- IDE でのジャンプが遅い

**対応**: **現状維持を推奨**。分割するならかなりの設計変更が必要

### 14. プロンプトテンプレートの重複

**現象**:
- `batch/summarize.py` `SUMMARY_SYSTEM_PROMPT` と
- `batch/retro_summarize.py` `RETRO_SUMMARY_PROMPT`

が 90% 同じだが別物として維持されている

**対応案**: 共通モジュール `batch/prompts.py` に切り出す

### 15. モデル ID のハードコード分散

**現象**: `gemma-4-31b-it`, `gemma-3-27b-it`, `gemini-3.1-flash-lite-preview` 等が各ファイルに直書き

**対応案**: `batch/constants.py` を作って centralize

### 16. Python バージョンのバラつき

**現象**: main は 3.12.1、 ai_news は 3.11、 market は 3.11、 cleanup は 3.10

**対応**: 全て 3.12 に統一しても問題はないが、動いているものを触るリスクと天秤

### 17. `bump_db.json` の残骸と `bump_count` の二重計上（解消済み）

**履歴**: 旧 `bump_bot_gamified.py` はローカル JSON で Bump を記録していた。MongoDB 移行後、一時期両方が動いて二重計上する期間があった

**現状**: 旧ファイル群は全て削除済み（STRUCTURE.md 参照）。新規環境では問題なし

### 18. `on_raw_message` / `on_raw_interaction_create` の空スタブ

> ✅ **2026-06-06 対応済み**（commit `e4da578`）

旧 `on_socket_raw_receive` の削除跡で `pass` だけのハンドラが残っていた。削除済み。

### 19. `replace_as_title` の空関数

> ✅ **2026-06-06 対応済み**（commit `e4da578`）

「NGワード置換は probot に移行済み」とコメントされた空関数。`/as` コマンドの呼び出し元から呼び出しごと削除済み（動作に影響なし）。

### 20. `SCARY_CHANCE` `MIMIC_CHANCE` 関連の死んだコード

> ✅ **2026-06-06 対応済み**（commit `e4da578`）

自動発火経路は BAN 実績ありで封印されていた（🔴 **項目 6** 参照）。原因調査のため残していた未使用コードを削除済み:
- `SCARY_CHANCE` / `MIMIC_CHANCE`、`SCARY_MESSAGES`、`MIMIC_PROMPT_TEMPLATE`、`trigger_mimic()`、`on_message` の封印 `pass`

純粋削除（0 added / 94 removed）・`py_compile` clean・削除シンボルは repo 全体で 0 hit・手動 `/mimic` 経路（共有ヘルパ）は無傷で維持。

### 21. `last_top_url.txt` の git commit pollution

`ai_news.yml` が毎回 `last_top_url.txt` をコミットするため、git history が `Update history [skip ci]` で埋まる

**対応案**:
- Redis や KV ストアに移行
- または git tag で管理

### 22. `workflow_dispatch` 用の `GITHUB_TOKEN` の権限

**現象**: `/retroreport` `/focus` で main.py から GitHub Actions をトリガーするための `GITHUB_TOKEN` が、現在 fine-grained token として Renders の env に入っている

**注意**:
- PAT が expire したら `/retroreport` が失敗
- ログは `❌ GitHub Actions のトリガーに失敗しました` で出るが、再発行するまで気付きにくい
- Token の有効期限管理を推奨

---

## パターン別の罠

### 非同期処理の罠

- **Motor の cursor は `async for` で回す**。`.to_list(length=N)` で list 化可能だが N を指定しないと全件取得になるので注意
- **`asyncio.create_task()` は fire-and-forget**。失敗してもメインは止まらない（ログだけ出る）
- **`asyncio.to_thread()`** で同期コード（Gemini SDK）を呼び出している。CPU-bound ではなく I/O-bound を意識

### Discord API の罠

- **`message.interaction_metadata` は属性が `user_id` か `user.id` かバージョンで異なる**。L1959 で両対応している
- **`on_raw_message_edit` と `on_message_edit` は別物**。`on_raw_message_edit` は cache 外でも発火。Bump 編集イベントには `on_raw` が必要
- **Webhook 送信は rate limit が別系統**。大量送信でも本 Bot とは別にカウント

### MongoDB の罠

- **`_id` は str で統一**（Discord user ID）。int で検索すると hit しない
- **`$push` の `$slice` 動作**:
  - `$slice: -10` は最新 10 件保持（末尾 10）
  - `$slice: 10` は先頭 10 件保持
  - **本プロジェクトは `-N` を使っている**
- **`find_one({...}) or {}`** のパターンを徹底。None 返却でクラッシュしないよう

### Gemini の罠

- **`response.text` が None の場合がある**（max_output_tokens 到達・safety filter）。必ず None チェック
- **JSON 出力指示は `temperature=0.1`** にしないと崩れる
- **プロンプトに `{variable}` を含めると `.format()` でエラー**。`{{}}` でエスケープ。MIMIC 系プロンプトは `{{` を使っていない（意図的）
- **gemma-4 は `ThinkingConfig` 非対応**（渡すと 400 INVALID_ARGUMENT）。応答が遅い場合はモデルサイズが原因。`gemma-4-26b-a4b-it`（MoE）が日常会話には速度・品質バランス良好

### PyMongo の罠

- **`Database` オブジェクトは bool 評価不可**（pymongo 4.x）。`if not db:` / `if db:` は `NotImplementedError` を送出する。必ず `if db is None:` / `if db is not None:` で比較すること

---

## ✅ 解決済み（market_report.py 予想機能）

> **2026-04-25 対応済み**（commit `01799c0`）

`market_report.py` の日経予想集計・XP付与機能が毎週金曜にクラッシュし、月曜の保存も不安定だった。

**原因**:
1. `if not db` / `if db` を pymongo 4.x の `Database` オブジェクトに使用 → `NotImplementedError`（4箇所）
2. 月曜のリアクション PUT が try/except なし → Discord API タイムアウト時にジョブ全体がクラッシュ
3. 金曜のリアクション GET に `?limit=100` なし → デフォルト 25 件しか集計されない

**修正内容**:
- 全4箇所を `db is None` / `db is not None` に変更
- リアクション PUT を `try/except` で保護
- リアクション GET URL に `?limit=100` を追加

---

## 運用チェックリスト

日次で確認すべきこと：

- [ ] Render ダッシュボードで `main.py` が動いているか
- [ ] `[ERROR]` ログが頻発していないか
- [ ] MongoDB の `summaries` が 2 時間毎に増えているか（停滞 = バッチ失敗）
- [ ] Discord 側で 日次の日報投稿（0:00 JST）が届いているか

月次：

- [ ] Gemini API 使用量の確認
- [ ] MongoDB ストレージ使用量（Atlas の無料枠 512MB）
- [ ] GitHub Actions の minutes 使用量（無料枠 2000分/月）

不具合時：

1. **[WARN]/[ERROR] ログ確認**
2. **該当機能を `extension-guide.md` で辿る**
3. **ここ (`known-issues.md`) で既知の問題か確認**
4. **該当 doc (`main-bot.md` / `batch-pipeline.md`) で詳細な挙動を確認**
5. **コード側に戻る**
