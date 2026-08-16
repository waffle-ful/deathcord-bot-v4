# 利用規約 ⇔ 実装 突合表（2026-08-16）

`doc/terms-and-privacy.md` の各記述が、実際のコードのどこに対応するかの根拠。
規約を改定するときは、この表を更新して「書いてあることが本当か」を再検証すること。

## 旧ドラフトの誤り（修正済み）

| # | 旧ドラフトの記述 | 実態 | 根拠 |
|---|---|---|---|
| 1 | 「サードパーティ製AIサービスのAPI（Google, **OpenAI, Anthropic, Alibaba Cloud等**）を利用」 | **Googleのみ**。他社SDK・APIキー・呼び出しはリポジトリ内に一切存在しない | `grep -i 'openai\|anthropic\|dashscope\|qwen\|alibaba\|claude-'` → 0件。使用モデルは `CLAUDE.md:82-85`（`gemini-*` / `gemini-embedding-001`）のみ |
| 2 | 「**商用APIポリシー**に基づき処理され、原則として**AIモデルの再学習には利用されません**」 | **逆**。無料枠（Unpaid Services）運用のため、Googleの製品改善・機械学習に利用され、人間レビュアーが読む可能性がある | Gemini API Additional Terms（<https://ai.google.dev/gemini-api/terms>）。無料枠運用であることは `CLAUDE.md:87`（モデル連鎖の理由＝free-tier 容量429回避）と、恒久策として「課金Tier1」が未実施であることから確認 |
| 3 | 「これらのデータは…**個人を特定する目的では使用されません**」（一方で §2 に「同一ユーザーの識別」） | 文面が自己矛盾。実際は Discord ユーザーID・表示名を主キーとして永続保存している | `main.py:2835-2847`（author_id/author_name を保存）、`doc/data-model.md:20`（`users._id` = Discord user ID）。→「実世界の個人を特定する目的では利用しない」に限定して整合させた |

## 事実確認できた記述

| 規約の記述 | 根拠 |
|---|---|
| 発言ログは30日で自動削除 | `main.py:2577-2579` `expireAfterSeconds=30*24*3600`（TTLインデックス `ttl_30d`、基準は `created_at`＝記録時刻） |
| 保存されるのはテキスト本文のみ・2000文字まで | `main.py:2842` `"content": message.content[:2000]`。添付・画像は保存対象外 |
| 全チャンネルの発言を記録 | `main.py:2832-2847` は `on_message` の共通経路（チャンネル限定なし） |
| 要約（日報）は永続保持 | `doc/data-model.md:200`「削除は一切ない（`retro_date` 以外）」 |
| 記憶・主張・会話履歴は原文に近い | `doc/data-model.md:63-83`（`butler_history` は原文、`memories.content`・`claims.content` は要旨） |
| プロフィールに誕生日・趣味等が入る | `doc/data-model.md:41-52`（`birthday`, `hobbies`, `relations`, `memo`） |
| ニックネーム対応表（あだ名→正式名） | `doc/data-model.md:146-164`、`system._id="nickname_map"` |
| 監査ログを参照する | `main.py:3093`, `main.py:3100`, `main.py:3262`（`view_audit_log` / `audit_logs()`） |
| `/focus`・`/retroreport` は30日超の履歴をDiscordから再取得 | `batch/focus_summary.py:469-483`（lookback日数分をチャンク取得）、`batch/retro_summarize.py:116` |
| `/privacy` は性格推定を停止する | `main.py:4563-4575`（`personality_optout` を書く）。尊重側：`batch/analyze_personality.py:624`, `batch/analyze_nonbooster.py:210`, `batch/enrich_memories.py:71`, `batch/focus_summary.py:650` |
| `/privacy` でも日報・mimicは止まらない | `mimic_cmd`（`main.py:4762-4816`）と `_build_mimic_utterances`（`main.py:619-634`）に `personality_optout` チェックが無い。要約系も `personality_optout` は見ない（※未同意者の除外は 2026-08-16 に `consent_util` で別途実装。`/privacy` 由来の除外は依然として未対応） |
| `/相性` は公開表示・第三者2人を指定可 | `main.py:4451-4506`（`followup.send(embed=embed)` に `ephemeral` 指定なし。引数 `member`/`member2` で他人同士を指定できる） |
| `/mimic` は本人の実発言を材料に、本人の名前とアイコンで代弁 | `main.py:600-634`（`messages_col` から直近12件＋`butler_history`＋`claims`）、`main.py:4793-4807`（`ephemeral=False`／`avatar_url`）、`main.py:4810-4816`（`mimic_log` 記録） |
| `/myprofile`・`/clearmaid` | `main.py:4173-4175`（ephemeral）、`main.py:4578-4589` |
| 保存場所が国外を含む | Render.com（常駐bot）・GitHub Actions（バッチ）・MongoDB Atlas。`CLAUDE.md:10-11`, `doc/data-model.md:3` |
| 添付・画像の中身は保存も送信もしない | `attachments` の参照は `main.py:3231-3232` の1箇所のみ（無敵機能の再投稿用にURLをメモリ保持）。Geminiへ画像を送る経路（`inline_data`/`mime_type`/`from_bytes`）は0件＝テキストのみ送信 |
| DMも記録対象 | `on_message`（`main.py:2817-2847`）にホームギルド判定が無く、`message.guild is None` を許容して `guild_id: ""` で保存している＝DMも `messages` に入る |

## 規約同意ゲート（2026-08-16 実装／実機未検証）

`main.py` に allow-list 方式の同意ゲートを実装。**実装済みだが、まだ一度も実行していない。**

| 要素 | 場所 |
|---|---|
| 設定・同意済みリスト（in-memory）・fail-closed 読込 | `_load_tos_gate` / `_tos_gate` / `_tos_agreed_ids` |
| 同意パネル（永続View） | `TosConsentView`（`tos:agree` / `tos:decline`）。`setup_hook` で `add_view` 登録 |
| 判定ヘルパー | `tos_allows(user_id)`。ゲート無効時は常に True。**記録も LLM 送信も、未同意者を扱う全箇所はこれを通す** |
| 記録ガード（二重の保険） | `on_message` 内、`messages` insert の直前。未同意なら `return`（XP・AI応答・`messages` への記録から外れる） |
| **会話文脈ガード** | `_maid_respond_inner`（メイド応答のたびに `channel.history(limit=12)` を読む）と `_recent_channel_text`（弁護・レスバ文脈）。未同意者の行をプロンプトに載せない |
| **`/mimic` ガード** | `mimic_cmd`。未同意者は物真似の対象にできない（過去の `messages` を LLM へ送らないため） |
| **バッチ側フィルタ** | `batch/consent_util.py`（2026-08-16 追加）。要約系は Discord REST 直読みで記録ガードを通らないため、こちらで別途除外する |
| コマンド | `/規約ゲート設定` `/規約ロック`(dry_run既定True) `/規約ロック解除` `/規約ゲート状態` `/規約同意付与` |

### バッチ側フィルタ（`batch/consent_util.py`）

**発見の経緯**：`on_message` の記録ガードは `messages` コレクションしか塞がない。日報・`/focus`・`/retroreport` は **Discord REST API から生ログを直接読む**ため、未同意者の発言がそのまま Gemini（無料枠＝学習利用あり）へ送られ `summaries` に永久保存されていた。実装当初のコメント「要約からも自動的に外れる」は**事実誤認**だった（2026-08-16 に修正）。

| 適用先 | 箇所 | 効果 |
|---|---|---|
| 日報（4時間ごと） | `batch/fetch_discord_logs.py:main` + author ループ | 未同意者の発言を logs.json に入れない |
| `/focus` | `batch/focus_summary.py:fetch_logs` | 同上 |
| `/retroreport`・backfill | `batch/retro_summarize.py:fetch_day_logs` | 同上（`retro_backfill.py` は同関数を経由） |
| 性格分析 | `batch/analyze_personality.py:select_targets` / `compute_cohort_percentiles` | 未同意者を母集団から除外 |
| 非ブースター分析 | `batch/analyze_nonbooster.py:main` | 同上 |
| 記憶・主張の抽出 | `batch/enrich_memories.py:select_targets` | 同上 |

- **真実の所在**：規約バージョンは `system._id="tos_gate"` の `version` を読む（`TOS_VERSION` を batch にハードコードしない）。ゲートの `enabled` もこのdocから取る。★`main.py` の `TOS_VERSION` を上げても、`_save_tos_gate()` が再実行されるまで doc 側は古い版のまま。**改定時は必ず `/規約ゲート設定` 等を叩き直す**こと。
- **fail-closed**：`MONGODB_URI` 未設定・接続失敗・クエリ失敗はすべて `SystemExit(1)`＝その回の要約を中止する。判定できないなら送らない側に倒す。ゲートが `enabled=False`（未導入）のときだけ従来どおり全員を対象にする。
- **env 配管**：`summarize.yml` の Fetch ステップに `MONGODB_URI` を追加済み。他の workflow（focus / retro / daily_tasks）は元から設定済み。
- 除外件数は `[fetch] 規約未同意により除外: N件` としてログに出る（Actions のログで効いているか確認できる）。

**未同意者へのメンションのマスキング（`ConsentFilter.mask_content`）**

同意済みユーザーの発言に含まれる `<@未同意者ID>` を、LLM へ送る前に `[非公開ユーザー]` へ置換する。REST 取得3本の `content` に適用（`[fetch] 未同意者へのメンションを伏せ字化: N件`）。

- **他人の発言そのものは消さない**。A の発言は A のものであり、B のために消すのは A の発言の検閲になる。消すのは B の ID だけ。
- ID は一意なので**誤爆ゼロ**。`<@!123>` 形式も対象。ロール `<@&123>` / チャンネル `<#123>` / 絵文字 `<:name:123>` はマッチしない。
- ★**表示名・ニックネームには手を出さない**（意図的な設計）。`nickname_map` に無い愛称は拾えず網羅できないうえ、一般名詞と同じ名前で誤爆して要約が壊れる。中途半端に効く対策は「効いているつもり」を作るぶん有害。
- **限界**：これは完全な対策ではない。「他人の発言に含まれる第三者への言及」は原理的に防げず（防ぐには発言者側を検閲することになる）、同意ゲートは「自分の発言」の同意であって「自分について語られること」の同意ではない。この限界は**未同意者だけでなく同意済みメンバーにも等しく当てはまる**。規約 §6 に明記済み。
- **根本的な緩和策は課金 Tier1 への移行**（下記「未解決」1）。学習利用・人間レビューの前提が消えれば、この問題の危険度そのものが一段下がる。

**設計上の要点**
- **allow-list 方式**：`@everyone` から送信系を deny し、`同意済み`ロールにだけ allow。既定が「喋れない」なので **botが落ちていても未同意者が素通りしない**。逆方式（未同意ロールを全員に配る）は、付与漏れで fail-open になるうえ、role overwrite が「全deny→全allow」の順で解決される仕様上、他ロールの明示 allow に負ける
- **権限の格上げを防ぐ**：`@everyone.send_messages is False` のチャンネル（お知らせ等）は対象外。ここに同意済みallowを付けると読み専chが喋れるchに化ける
- **スナップショット**：適用前の三値（True/False/None）を `system._id="tos_lock_snapshot"` に保存してから破壊操作。`/規約ロック解除` で完全復元（元々上書きが無かったchは上書きごと削除）
- **bot自身の沈黙防止**：`@everyone` deny は bot にも効く。管理者権限が無い場合は `guild.self_role` にも allow を付ける
- **スレッドも塞ぐ**：`send_messages` に加え `send_messages_in_threads` / `create_public_threads` / `create_private_threads`
- **dry_run が既定**：素通りできるロール（明示allow保持者）を列挙してから本適用する

**実機で最初に確認すべきこと**
1. `/規約ゲート設定` の前に、同意済みロールが**誰にも付いていない**こと（付いていると最初から喋れる）
2. 同意済みロールが**空気くん自身のロールより下**にあること。上にあると `add_roles` が 403 になり、同意ボタンが常に「⚠️ロール付与に失敗」に落ちて手動運用しかできなくなる
3. `/規約ロック`（dry_run）の「素通りできるロール」が空か。RANK_STAGES のランクロールに明示allowが付いていないか要確認
4. 自分で同意ボタン → ロール付与 → 発言できるか
5. ロック後、未同意アカウントが本当に喋れないか／`messages` に入らないか
6. **ロック→解除の往復を実際に一度通してから**、ロックしたまま運用に入ること（復元が壊れていないことを確認する）
7. ロック中に `/規約ロック dry_run:False` を再実行すると拒否されること（二重適用でスナップショットが空上書きされる事故の防止ガード）

## データ削除請求の実行（2026-08-16 実装／実機未検証）

`/データ削除 user_id: delete_xp:` （管理者専用・確認ボタン付き・実行前に件数プレビュー）

**フィールド単位で消す**のが設計の要点。ドキュメントごと消すとランクや順位まで巻き戻り、本人が求めていない副作用が出る。問題なのは人格・発言由来の情報であって活動量ではない。

| | 対象 |
|---|---|
| **消す** | `messages`（author_id一致の全件）／`users` の `profile`・`simple_profile`・`butler_history`・`memories`・`claims`・`title`・`maid_callname`・`last_content`／`system` の mimic_log・nickname_map該当エントリ／`invincible_users` |
| **残す（既定）** | `xp`・`bump_count`・`invite_count`・`streak_days`・`conv_count*`・`name`・`tos_agreed`。`delete_xp:True` で活動データも削除可 |
| **意図的に残す** | `mod_warnings`・`guard_events`・`killswitch_snapshots`（モデレーション証跡）／`summaries`（本文に溶け込んでおり個別削除は不可） |

**★最重要**：`profile` を消すだけでは不十分で、**`personality_optout: True` を必ず立てる**。立てないと、その晩の `analyze_personality` / `analyze_nonbooster` / `enrich_memories` が残った要約から人格・記憶を作り直してしまう（各バッチは `personality_optout != True` で対象を絞っているのでこれで止まる）。`_erase_user_data` はこれを自動でセットする。

**実施記録**：`system` に `type:"erasure_log"`（対象ID・実行者・日時・削除件数のみ。内容は残さない）。履行の証跡になる。

**実機で確認すべきこと**：①テスト用IDでプレビューが正しい件数を出すか ②実行後に `/myprofile` が空になり XP が残っているか ③翌日のバッチ後もプロフィールが再生成されていないか（`personality_optout` が効いているかの確認。**ここが一番の勘所**）

## 未解決 / 今後の判断事項

1. **有料プラン（Paid Tier 1）への移行**：移行すれば §4 の学習利用に関する記述を「学習には利用されない」へ書き換えられる。かつ free-tier の容量429（メイド沈黙の主因）も同時に解消する。**規約とシステム安定性の両方にとって最も効果の大きい一手**。
2. **`/privacy` の適用範囲拡大**：現状は性格推定のみ。日報要約からの除外・`/mimic` 対象外化を実装すれば、規約 §6 の但し書き（⚠️部分）を削除できる。
3. ~~**削除請求の実行手順**~~ → **2026-08-16 実装済**（`/データ削除`）。下記参照。
4. **同意前に集めたデータの扱い**：**方針決定済（2026-08-16）＝当面は「請求があれば削除」のオプトアウト方式**。一括削除はしない。この方針は同意パネル（`_tos_embed` の項目④）と規約 §6 の両方に明記済み。既存メンバーは同意時点で既に数ヶ月分のデータがあり、そこを開示しないと「何に同意したのか」が特定できず同意の効力が弱くなるため、パネル本文に直接書いている（リンク先は読まれない前提）。
   - 残課題：削除請求を実際に処理する運用スクリプトが未整備（上記3と同じ）。請求が来たら `users` の当該ドキュメントと `messages` の `author_id` 一致分を手で消すことになる。
5. **規約改定時の再同意の強制**：`TOS_VERSION` を上げると `_load_tos_gate` の同意済み判定（`$gte`）から自動的に外れて記録は止まるが、**ロールの自動剥奪は未実装**（旧版の同意者はロールが残るので喋れてしまう）。改定時は `/規約ゲート状態` で未同意者を確認し、手動でロールを外す必要がある。
