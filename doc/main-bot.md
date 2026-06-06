# main.py の内部構造

3,100 行・140KB の単一ファイル Bot。**この規模は意図的**（[architecture.md](./architecture.md) 参照）。セクションは以下の順で並んでいる：

```
  1- 29   imports + 起動時 Webサーバー
 31- 96   環境変数・モデル ID・ランク・ブースター・Bump 設定
101-269   PERSONALITIES 辞書（6人格）
270-282   人格 getter/setter（system コレクション）
285-327   ホラー・ミミック確率（現在は 0.0）・SCARY_MESSAGES
329-530   ミミック機能（セッション管理 + Webhook 偽装）
540-712   claims / memories（主張・記憶）抽出・保存・Vector Search
714-800   ニックネーム解決 / 要約取得 / smart_summary 構築
805-840   動的レートリミッター（12RPM）
842-855   butler_history getter/setter
858-922   プロフィール自動抽出（PROFILE_EXTRACT_PROMPT）
926-981   プロフィール整形 / 会話履歴整形（★印付与）
984-1003  _call_model / _is_503
1005-1075 会話キュー（_maid_queue, 単一 worker）
1077-1133 非ブースター自発話しかけ
1136-1222 非ブースター性格分析（30発言ごと）
1225-1320 _build_prompt（応答の心臓部）
1323-1404 _maid_respond_inner / maid_respond_cmd
1408-1490 ユーティリティ（XP計算、get_rank_info、nickname適用）
1492-1540 MongoDB 接続 / MyBot クラス / setup_hook / 招待スナップショット
1542-1690 RANK_COLORS / ランクアップ AI メッセージ / update_member_role
1693-1832 on_message（メイン）
1835-1917 on_raw_message_edit / on_interaction / on_raw_message スタブ
1919-2057 Bump 処理（重複防止 + check_bump + check_bump_webhook）
2059-2822 スラッシュコマンド 20 個
2920-2978 on_member_join（招待追跡 + ボーナス）
2981-3057 weekly_ranking_task / notification_task（無限ループ）
3059-3100 エントリポイント（429 バックオフ付き）
```

## 全関数一覧（抜粋・カテゴリ別）

**完全版は検索で追跡できるよう、分類とキー情報のみまとめる。**

### AI 応答パイプライン（ホット経路）

| 行 | 関数 | async | 責務 |
|----|------|------|-----|
| L1225 | `_build_prompt(uid, display_name, content, channel_context)` | ✓ | 応答プロンプト組立（中心） |
| L1323 | `_maid_respond_inner(message, is_booster)` | ✓ | on_message 経由の応答本体 |
| L1378 | `maid_respond_cmd(interaction, content)` | ✓ | /maid コマンド経由の応答本体 |
| L1035 | `maid_respond_queued(message, is_booster)` | ✓ | キューに投入 |
| L1014 | `_maid_queue_worker()` | ✓ | キュー消費単一ワーカー |
| L1059 | `_run_ai_booster(prompt)` | ✓ | 主モデル → フォールバック → 3回リトライ |
| L984 | `_call_model(model, prompt)` | ✓ | 単一モデル呼び出し + _rate_record |
| L1000 | `_is_503(e)` | | 503 / UNAVAILABLE / high demand の判定 |
| L816 | `_rate_get_wait_seconds()` | | 12RPM 上限に近づくほど待機延長 |
| L838 | `_rate_record()` | | タイムスタンプ記録 |
| L1051 | `_typing_delay(text)` | | 文字数ベースの表示遅延 |

### プロファイル・記憶・主張（バックグラウンド）

| 行 | 関数 | async | 責務 |
|----|------|------|-----|
| L878 | `extract_and_save_profile(uid, ...)` | ✓ | PROFILE_EXTRACT_PROMPT で会話→プロフィール |
| L674 | `_extract_claims_and_memories(uid, ...)` | ✓ | 主張保存 + AI判定で記憶候補抽出 |
| L573 | `save_claim(uid, text)` | ✓ | `$push claims`（CLAIM_PATTERNS 判定） |
| L594 | `save_memory(uid, text, category)` | ✓ | embedding 付き `$push memories` |
| L556 | `_get_embedding(text)` | ✓ | gemini-embedding-001 でベクトル化 |
| L632 | `_get_recent_memories(uid, top_k)` | ✓ | 最新から単純取得 |
| L637 | `search_memories(uid, query, top_k)` | ✓ | MEMORY_TRIGGER_WORDS ヒット時に Vector Search |
| L1136 | `_analyze_nonbooster_realtime(uid, name)` | ✓ | 30発言ごとに非ブースター性格分析 |
| L714 | `resolve_nickname(name)` | ✓ | エイリアス → 正式名 |
| L733 | `get_nickname_map()` | ✓ | system.nickname_map 読取 |
| L742 | `get_latest_summary()` | ✓ | summaries の最新を created_at 降順で取得（is_retro/retro_date 除外） |
| L773 | `build_smart_summary(summary)` | | セクション優先度順に並び替え |

### XP・ランク・ロール

| 行 | 関数 | async | 責務 |
|----|------|------|-----|
| L1436 | `calculate_xp_gain(content, last_content)` | | 発言 → XP 量（30/50/70 or スパム1） |
| L1450 | `get_rank_info(xp)` | | xp → (rank_name, floor, next_floor, progress%) |
| L1630 | `update_member_role(member, current_xp, channel)` | ✓ | ランクアップ検出 + ロール付与 + Embed 投稿 |
| L1561 | `_generate_rankup_message(member, rank_name, next_info, personality_key, personality)` | ✓ | AI でランクアップ祝福メッセージ生成 |
| L1474 | `apply_nickname(member, title)` | ✓ | Discord nickname に「二つ名」埋め込み |
| L1471 | `build_nickname(title, base_name)` | | `「{title}」{base_name}` を 32文字以内に |

### Bump 検出

| 行 | 関数 | async | 責務 |
|----|------|------|-----|
| L1940 | `check_bump(message)` | ✓ | 通常の Bot メッセージ → XP +100 |
| L1991 | `check_bump_webhook(message)` | ✓ | Webhook 送信の Bump（Fortify等） |
| L1926 | `_is_bump_already_processed(message_id)` | ✓ | 重複防止（500件メモリ制限） |
| L1461 | `extract_embed_text(embed)` | | Embed 全文連結（キーワード検出用） |

### ミミック機能

| 行 | 関数 | async | 責務 |
|----|------|------|-----|
| L399 | `_send_mimic(channel, session, text)` | ✓ | Webhook で「（AIの推測）」付き送信 |
| L420 | `_run_mimic_session(channel, session)` | ✓ | セッション初回の深層心理発言 |
| L452 | `_mimic_react(channel, session, trigger_text)` | ✓ | チャンネル流れへの反応 |
| L484 | `trigger_mimic(message)` | ✓ | 自然発生（現在 MIMIC_CHANCE=0.0 で無効） |
| L362 | `_build_mimic_profile(doc)` | | users ドキュメント → プロフィール文字列 |
| L390 | `_build_mimic_history(doc)` | | butler_history → 発言リスト |

### 起動・サーバー・バックグラウンド無限タスク

| 行 | 関数 | async | 責務 |
|----|------|------|-----|
| L20 | `start_web_server()` | ✓ | aiohttp ヘルスサーバーを PORT で起動 |
| L1516 | `init_invite_snapshot(bot)` | ✓ | 起動時に全ギルドの招待コード uses を記録 |
| L2981 | `weekly_ranking_task()` | ✓ | 日曜 12:00 JST に週次ランキング投稿 |
| L3028 | `notification_task()` | ✓ | 10分毎に Bump Bot クールダウン確認 |
| L3063 | `_main()` | ✓ | エントリ（429 指数バックオフ） |

### イベントハンドラ

| 行 | ハンドラ | 主要分岐 |
|----|---------|--------|
| L1697 | `on_message` | Bot 判定 → Bump / BUTLER_CHANNEL / メンション → メイド / XP加算 / 自発話しかけ確率 |
| L1835 | `on_raw_message_edit` | Bump キーワード検出 → check_bump or check_bump_webhook |
| L1886 | `on_interaction` | **Dislist/ディス速 のスラッシュ応答検出**（on_message に来ないため必須） |
| L2933 | `on_member_join` | 招待コード差分検出 → invite_count++ → INVITE_BONUSES |

### スラッシュコマンド（全 20 個）

| 行 | コマンド | 引数 | 権限 |
|----|---------|------|-----|
| L2063 | `/rank` | `member?` | 全員 |
| L2087 | `/top` | なし | 全員 |
| L2104 | `/luckytitle` | なし | 全員（日付シード） |
| L2119 | `/as` | `title: str` | 全員 |
| L2137 | `/maid` | `message: str` | 全員 |
| L2144 | `/personality` | Select メニュー | 全員 |
| L2159 | `/myprofile` | なし | 全員 |
| L2286 | `/editprofile` | モーダル | ブースター |
| L2298 | `/clearmaid` | なし | ブースター |
| L2312 | `/setxp` | `member, xp` | 管理者 |
| L2334 | `/report` | `date?` | ブースター（AND home guild） |
| L2430 | `/mimic` | `member` | ブースター |
| L2506 | `/stopmimic` | なし | ブースター |
| L2575 | `/retroreport` | `date: YYYY-MM-DD` | 管理者 or ブースター + home guild + GITHUB_TOKEN |
| L2646 | `/focus` | `member?, keyword?` | 同上 |
| L2721 | `/addnick` | `nickname, realname` | 管理者 |
| L2741 | `/removenick` | `nickname` | 管理者 |
| L2761 | `/listnick` | なし | 管理者 |
| L2776 | `/summarystatus` | なし | 管理者 |
| L2799 | `/listmodels` | なし | 管理者 |

## グローバル状態（モジュールレベル変数）

**壊さないための最重要リスト**。これを勝手に書き換える関数を追加すると同期が壊れる。

| 変数 | 型 | 書き手（関数） | 読み手 | 注意 |
|------|---|------------|-------|-----|
| `_invite_snapshot` | `dict[str, int]` | `init_invite_snapshot`, `on_member_join` | `on_member_join` | 起動時スナップショットが取れない場合は招待追跡不能 |
| `_mimic_sessions` | `dict[int, dict]` | `mimic_cmd`, `stopmimic_cmd`, `_auto_end` | `mimic_cmd`, `_mimic_react` | チャンネル ID キー。5分タイマーで自動削除 |
| `_rate_timestamps` | `list[datetime]` | `_rate_record` | `_rate_get_wait_seconds` | 古いタイムスタンプを取り除くのは read 時 |
| `_maid_queue` | `asyncio.Queue` | `maid_respond_queued` | `_maid_queue_worker` | `_QUEUE_MAX=5` 超でサイレントドロップ |
| `_maid_queue_processing` | `bool` | `_maid_queue_worker`, `maid_respond_queued` | `maid_respond_queued` | worker 30s アイドルで False に戻る |
| `_processed_bump_ids` | `set` | `_is_bump_already_processed` | 同上 | 500件超で古い半分 drop |
| `users_col`, `system_col`, `summaries_col` | Motor collection | モジュールトップで 1 回生成 | 全体 | 再生成不可（イベントループ固有） |
| `client` | `MyBot` | モジュールトップで 1 回生成 | 全体 | **絶対に再生成しないこと**（コマンドが消える） |

## 重要な定数（行番号）

### 環境変数・ID系
- `TOKEN, MONGO_URL, NOTIFY_CHANNEL_ID, GEMINI_API_KEY` — L31-35
- `HOME_GUILD_ID` — L72
- `GENERAL_CHANNEL_ID = 1467851526252007651` — L71（ランクアップ・ランキング投稿先）
- `BOOSTER_ROLE_ID = 1420309723273756704` — L83
- `BUTLER_CHANNEL_ID = 1477343773251080433` — L85（ブースター専用メイドチャンネル）
- `GRADUATE_REMOVE_ROLE_IDS = {1417840244711100566}` — L80

### XP・ランク
- `RANK_STAGES` — L47-68（19段階。順序変更厳禁）
- `BUCKET_LABELS` — L1412（スタッフ + 全ランク名）
- `STREAK_BONUSES = {3: 100, 7: 300, 30: 1000}` — L79
- `INVITE_BONUSES = {1: 200, 3: 500, 5: 1000, 10: 2000}` — L2924 付近
- `XP_COOLDOWN_SECONDS = 60` — L1410
- XP 計算ルール（L1436-1448）:
  - 連投スパム (`(.)\1{9,}`): 1 XP
  - 長文 (>200 文字): 70 XP
  - 通常 (15-200): 50 XP
  - 短文 (<15): 30 XP
  - 直前と同内容: 0 XP

### AI モデル
- `MODEL_BOOSTER = "models/gemini-3.1-flash-lite-preview"` — L41
- `MODEL_FALLBACK = "models/gemma-4-31b-it"` — L42
- `MODEL_GENERAL_FB = "models/gemma-4-26b-a4b-it"` — L43
- Embedding: `"models/gemini-embedding-001"` — L560（直書き）

### レート制限・キュー
- `_RATE_LIMIT_RPM = 12` — L813
- `_QUEUE_MAX = 5` — L1011
- `BUTLER_HISTORY_MAX = 5` — L86（× 2 で会話 10 件保持）

### 確率
- `SCARY_CHANCE = 0.0` / `MIMIC_CHANCE = 0.0` — L288-289（**現在無効化**）
- `NB_TALK_CHANCE = 0.005` / `NB_TALK_CHANCE_TOPIC = 0.02` — L292-293

### 6人格
- `PERSONALITIES` — L101-269（`yandere`, `angry`, `tsundere`, `baka`, `serious`, `counselor`）
- `DEFAULT_PERSONALITY = "yandere"` — L271
- `RANK_COLORS` — L1545（ランク名 → 16進色）

## Gemini 呼び出しマップ

| 行 | モデル | temperature | max_output_tokens | 用途 |
|----|-------|------------|------------------|------|
| L439 | MODEL_BOOSTER | 0.85 | 120 | ミミック初回発言 |
| L471 | MODEL_BOOSTER | 0.85 | 120 | ミミック反応発言 |
| L508 | MODEL_BOOSTER | 0.9 | 100 | 自然発生ミミック（無効化中） |
| L560 | `gemini-embedding-001` | – | – | 記憶 embedding |
| L699 | MODEL_BOOSTER | 0.1 | 50 | 記憶保存判定（JSON 返却） |
| L887 | MODEL_BOOSTER | 0.1 | 300 | プロフィール抽出（JSON） |
| L1128 | MODEL_BOOSTER（`_call_model` 経由） | 0.8 | 300 | 非ブースター自発話しかけ |
| L988（`_call_model` 内） | 引数モデル | 0.8 | 300 | メイド応答本体（主経路） |
| L1617 | MODEL_BOOSTER（`_call_model` 経由） | 0.8 | 300 | ランクアップ AI メッセージ |

## 例外処理の方針

プロジェクト全体で**「ユーザー体験を壊さない」が最優先**。:
- 基本: `print` でログ → 静かに continue
- メイド応答でだけは `「（メイドは今、混乱しております…）」` / `「（メイドは今、席を外しております…）」` といった**キャラクターを壊さないエラー文**を返す
- 503 は `_is_503` で検出して待機＆リトライ
- 429 は Discord 側は `_main` で指数バックオフ

メインで catch している例外:

| 行 | 箇所 | 対応 |
|----|-----|-----|
| L1063-1074 | `_run_ai_booster` モデル呼び出し | 503 → リトライ / 非503 → break → fallback |
| L1350 | `_build_prompt` 失敗 | 「（メイドは今、混乱しております…）」で返す |
| L1519 | 起動時 `init_invite_snapshot` | 招待取得失敗でも続行 |
| L1685 | `update_member_role` 内のロール付与 | ログだけで継続 |
| L1698, L1836, L1887 | 各イベントハンドラ | traceback 出力 |
| L3075 | `_main` `discord.errors.HTTPException` 429 | 指数バックオフ → `client.close()` → リトライ |

## バックグラウンドタスク管理

`setup_hook` で起動される永続タスク（L1512-1514）:
- `notification_task()` — 10分毎
- `weekly_ranking_task()` — 10分ポーリング（日曜12時で投稿）
- `init_invite_snapshot()` — 起動時 1 回

`asyncio.create_task` で ad-hoc 起動されるもの:
| 起動箇所 | タスク | 寿命 |
|---------|-------|-----|
| `maid_respond_queued` L1048 | `_maid_queue_worker` | アイドル 30s で終了 |
| `_maid_respond_inner` L1370, L1373 | profile/claims 抽出 | 単発 |
| `on_message` L1817 | 非ブースター性格分析 | 単発 |
| `on_message` L1828 | 自発話しかけ | 単発（2%確率以下） |
| `mimic_cmd` L2490 | ミミックセッション | 単発 |
| `mimic_cmd` L2503 | 5分後自動終了 | 5分 sleep |

これらはエラーで死んでも Bot 全体には影響しない設計。

## 拡張ポイント（新機能を足すときの推奨場所）

| やりたいこと | 触る関数・セクション |
|-----------|----------------|
| 新しいスラッシュコマンド | L2059- セクション末尾に `@client.tree.command` 追加 |
| 新しい人格 | `PERSONALITIES` dict（L101）+ `personality_hints` (L1589) + `/personality` メニュー |
| 新しいランク | `RANK_STAGES` リスト（L47、**順序厳守**）+ `RANK_COLORS` |
| 新しい Bump Bot | `BOT_CONFIG` dict（L88）にエントリ追加。webhook なら `check_bump_webhook` を経由 |
| XP 計算ルール変更 | `calculate_xp_gain` L1436 |
| 記憶トリガー追加 | `MEMORY_TRIGGER_WORDS` L623、`CLAIM_PATTERNS` L540 |
| 自発話しかけ頻度 | `NB_TALK_CHANCE*` L292 |
| レート制限設定 | `_RATE_LIMIT_RPM` L813 |
| プロンプトに追加情報 | `_build_prompt` L1225 の `parts.append(...)` |

詳細なレシピは [extension-guide.md](./extension-guide.md) を参照。
