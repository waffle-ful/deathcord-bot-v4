# 史上最強の記憶・会話機構 包括プラン（2026-07-07）

> 対象: 空気くん（finance/main.py + batch/*）
> 前提: Gemini無料枠（容量429あり・課金Tier1未移行）、Render無料、Mongo Atlas M0(512MB)。
> 本書は「現状診断 → 設計原則 → ターゲットアーキテクチャ → フェーズ別ロードマップ」の順。

---

## 1. 現状診断（2エージェント解剖の統合）

### 1.1 いまの記憶は「3層バラバラ」構造

| 層 | 実体 | 寿命 | 穴 |
|----|------|------|-----|
| 生ログ | `messages` コレクション | **TTL 30日で消滅** | 昇華し損ねた発言は恒久消失 |
| 個人記憶 | `users.memories`(40件MRU) / claims(20件) / profile | 40件で押し出し忘却 | 長期の人物理解が積み上がらない |
| サーバー記憶 | `summaries`(~970件・永久) | 永久 | 個人単位で引けない・トリガ語ゲート依存 |

### 1.2 確定した「認知機能の穴」一覧

**会話側（main.py）**
1. **短期記憶が5往復のみ**: `butler_history` は `BUTLER_HISTORY_MAX=5`。それ以前はLLM抽出memoriesに残った分だけ。
2. **メイド自身の発言・約束が記憶化されない**: 保存はuser発言由来のみ。「メイドが何を約束したか」は5往復で消える＝自分の発言に責任を持てない。
3. **記憶抽出の発火条件バグ級仕様**: `_extract_claims_and_memories` はメイド応答に**全角括弧が無いときだけ**発火(L2201)。応答形によって記憶更新がスキップされる。
4. **チャンネル横断の会話継続不可**: channel_contextは現chのDiscord history 10件を都度fetchするだけ。mimic/resubaセッションもch単位dict。
5. **通常会話にセッション概念なし**: 毎応答ステートレス。「今この一連の会話」という括りが無い。
6. **in-memory状態がRender再起動で全消失**: 進行中resuba/mimic transcript、メイド発言復活キャッシュ、レート履歴。
7. **RAG発火がトリガ語ゲート依存**: `SUMMARY_TRIGGER_WORDS` or 年月が無いと日報RAG不発。自然な想起質問を取りこぼす。
8. **キュー溢れは無言drop**（`_QUEUE_MAX=5`）。

**記憶パイプライン側（batch/）**
9. **enrich_memoriesだけ `is_booster:True` 取り残し**: 性格分析はxp>0全員に拡張済みなのに、非ブースターは長期memories/claimsが日次補充されない。**コード不整合＝実質バグ**。
10. **生ログ30日TTL vs 日次昇華の競争**: 昇華漏れ＝恒久消失。蒸留セーフティネットなし。
11. **昇華レイテンシ**: 雑談→summariesは4-5h、→profile/memoriesは~24h。「さっきの話」は長期記憶に載らない。
12. **モダリティの穴**: DM・VC・リアクション・画像/添付・**アーカイブスレッド**・webhook発言は一切記憶されない。
13. **append-onlyで矛盾記憶が共存**: 意味dedup(near-dup)・矛盾解消(reconsolidation)なし。
14. **768次元の古いembeddingが混在し得る**: 次元ガードで無言スキップ＝想起漏れ。
15. **fetch爆発→BAN→本番bot巻き添え停止**の構造リスク（known-issue #7、大半未実装）。
16. **nickname_mapのAI誤検出が手動登録を汚染**（known-issue #9、TTL未実装）。
17. **doc陳腐化**: data-model.md（memories10件/Atlas index/2h要約）、batch-pipeline.md（Tier3未実装表記）は実装と乖離。

---

## 2. 設計原則

1. **人間の記憶モデルに寄せる**: ワーキングメモリ（セッション）→ エピソード記憶（出来事）→ 意味記憶（人物理解・関係）→ 自己記憶（メイド自身の発言・約束）の4階層＋夜間の**整理睡眠（consolidation）**。
2. **書き込みは安く・確実に、読み出しは賢く**: embedding APIは会話quotaと別枠なので、リトリーブは常時実行してよい。生成LLM呼び出し（quota本丸）は増やさない。
3. **忘却は「消す」ではなく「圧縮して格下げ」**: TTLで消える前に蒸留、40件で押し出す前にアーカイブ。
4. **再起動に強く**: セッション状態はMongoにバックアップ、復元可能に。
5. **無料枠制約の尊重**: Mongo 512MB・Gemini容量429・Render再起動を前提に、全機能が劣化グレースフルに動くこと。
6. **段階deploy・各段で実機検証**: 過去の「実装済み未検証」の山を増やさない。各Phaseに検証手順を必ず付ける。

---

## 3. ターゲットアーキテクチャ

```
┌─ 会話時（リアルタイム・読み）──────────────────────────┐
│ Working Memory   : channel_sessions (Mongo-backed, 直近~30発言,      │
│                    メイド発言込み, TTL 2h, 再起動復元可)              │
│ Memory Router    : クエリ1回のembed → 4ストア並列リトリーブ           │
│   ├ Episodic     : user_memories (独立コレクション・無上限・         │
│   │                importance×recency×relevance スコアリング)        │
│   ├ Semantic     : profile / bigfive / relations（既存＋関係グラフ）  │
│   ├ Server       : summaries（ゲート撤廃・常時軽量リトリーブ）        │
│   └ Self         : maid_memories（自分の発言・約束・promiseトラッカ） │
└──────────────────────────────────────────┘
┌─ 会話後（リアルタイム・書き）──────────────────────────┐
│ 毎応答後に必ず: 抽出(user発言＋メイド発言両方) → embedding付与        │
│ → user_memories / maid_memories へ append（発火条件バグ撤廃）        │
└──────────────────────────────────────────┘
┌─ 夜間バッチ（整理睡眠）────────────────────────────┐
│ consolidation: 意味dedup(cos>0.92を統合) → 矛盾検出→新しい方を優先    │
│ distillation : TTL残り5日のmessagesをユーザー別に蒸留→episodicへ      │
│ reflection   : 週1で「この人について分かったこと」の高次洞察を生成     │
│ decay        : importanceスコア減衰・低スコアはアーカイブ格下げ        │
└──────────────────────────────────────────┘
```

### 各ストアの設計詳細

**A. `channel_sessions`（新規・ワーキングメモリ）**
- キー: `{channel_id}`。フィールド: `transcript[]`（author, content, ts, is_maid）最大30件ローリング、`last_active`。
- 書き: on_messageで対象ch（メイドが直近発話したch）だけ追記。読み: `_build_prompt` の channel_context を Discord fetch からこれに置換（fetch APIコール削減の副次効果）。
- 再起動対策: Mongo常駐なので自然に復元。resuba/mimicセッションも同スキーマに `session_type` 付きで退避（10発言ごと or 終了時に書き込み）。
- TTL index 2h → ワーキングメモリらしい揮発。

**B. `user_memories`（新規コレクション・エピソード記憶）**
- `users.memories`配列(40件)から独立コレクションへ移行。1 doc = 1記憶: `{user_id, text, category, embedding(3072/RETRIEVAL_DOCUMENT), importance(1-10), created_at, last_accessed, access_count, source(chat|enrich|distill), superseded_by}`。
- **無上限**（ただしMongo容量ガード: 1ユーザー500件超で最低スコアからアーカイブ圧縮）。
- リトリーブスコア = `α·relevance(cos) + β·recency(指数減衰) + γ·importance`（generative agents方式）。既存の「閾値0.65＋トリガ語-0.10＋フォールバック」をこれで置換。
- 注入件数は既存の職階連動 `memory_topk(xp)` を維持（パークとして面白いので残す）。
- **embedding空間の統一**: task_type=RETRIEVAL_DOCUMENT/QUERY・3072次元に全記憶を統一。移行スクリプトで旧40件をre-embed（768次元混在を一掃）。

**C. `maid_memories`（新規・自己記憶＋約束トラッカ）**
- メイド応答の抽出時に「自分が言ったこと・約束したこと」も抽出。`{user_id, text, kind(statement|promise), due_hint, embedding, created_at, fulfilled}`。
- 会話時に相手のuser_idで常時リトリーブ→「私、前に〜と申しましたね」が可能に。
- promiseは日次バッチで期限ヒント照合→該当ユーザーの次回会話時にextra_contextへ注入（能動リマインド）。

**D. `summaries` RAG（既存改修）**
- トリガ語ゲート撤廃 → 8文字以上なら常時リトリーブ。**注入判定は閾値で行う**（絶対0.45＋相対gap0.05＋名前needle＋日付一致のハイブリッドは優秀なので維持）。ヒット無しなら黙って注入しない＝quota増ゼロ（embedは別枠・cosineはローカル）。
- 970件全読込は当面OK（3072次元×970≒12MB/回はメモリ注意→`embedding`だけprojectionで取得＋起動時キャッシュ＆差分更新に変更）。

**E. 関係グラフ（意味記憶の拡張・Phase3）**
- summariesから「誰と誰がよく絡むか・どんな関係か」を週次抽出→`system.relation_graph`。プロンプトに「この人は◯◯さんと仲が良い」を注入。simple_profileの`relations/frequent_members`が既にあるので、それの双方向グラフ化＋鮮度管理。

---

## 4. フェーズ別ロードマップ

### Phase 0 — 即日バグ修正（コード数行・リスク極小）
| # | 項目 | 内容 | 検証 |
|---|------|------|------|
| P0-1 | enrich_memoriesのis_booster取り残し | L173のフィルタを `xp>0 & optout除外` に統一 | 手動dispatchで非ブースターにmemories付与を確認 |
| P0-2 | 記憶抽出の発火条件撤廃 | 全角括弧条件(L2201)を外し、sentinel応答以外は常時発火 | 会話→users.memories即時追記を確認 |
| P0-3 | nickname_map汚染対策 | AI由来エントリに `source:"ai", added_at` を付与、90日TTL＋手動は無期限 | mapのmerge動作をユニット確認 |
| P0-4 | 768次元embedding掃除 | 移行スクリプトで旧memoriesをre-embed（finance/reembed_memories.py流用） | 次元不一致スキップのログが消える |
| P0-5 | doc更新 | data-model.md / batch-pipeline.md を実装に同期 | — |

### Phase 1 — 記憶の底上げ（~数日・main.py中心）
| # | 項目 | 内容 |
|---|------|------|
| P1-1 | `user_memories` 独立コレクション化 | 上記B。移行スクリプト＋`search_memories`/`save_memory`/enrichの読み書き差し替え。旧配列は読み取りフォールバックを1週間残して削除 |
| P1-2 | メイド自身の発言・約束の記憶化 | 上記C（`maid_memories`）。抽出プロンプトに「メイドの発言・約束」枠を追加（LLM呼び出し回数は増やさず同一呼び出しで両方抽出） |
| P1-3 | butler_history拡張 | 5往復→トークン予算ベースで最大15往復（直近5往復は全文、6-15往復は各80字に切り詰め）。quota増を最小化しつつ健忘症を大幅緩和 |
| P1-4 | summaries RAGゲート撤廃 | 上記D。embedding起動時キャッシュ化も同時に |
| P1-5 | キュー溢れ通知 | drop時に「(お待ちの方が多いですわ…)」的リアクション1個（API1回・生成ゼロ） |

### Phase 2 — セッションと整理睡眠（~1週間・main.py＋batch）
| # | 項目 | 内容 |
|---|------|------|
| P2-1 | `channel_sessions` 導入 | 上記A。channel_contextのDiscord fetch置換＋resuba/mimicセッションの再起動復元 |
| P2-2 | 夜間consolidation | daily_tasksに追加ジョブ: 意味dedup(cos>0.92統合)→矛盾検出(同カテゴリ・高類似・内容相反は新しい方にsuperseded_by)→importance減衰 |
| P2-3 | TTL前蒸留(distillation) | messagesのTTL残り5日分をユーザー別に蒸留→user_memoriesへ`source:"distill"`で保存。**30日消滅問題の恒久解** |
| P2-4 | promiseリマインド | maid_memoriesのpromise照合→次回会話でextra_context注入 |
| P2-5 | Gemini 2キープール | 既存構想の実装: 抽出・ゲート判定・consolidation等の非中核呼びを2本目キーへ→会話本丸のquota保護（記憶機能追加でLLM呼びが増える分をここで吸収） |

### Phase 3 — 最強化（順次・効果を見ながら）
| # | 項目 | 内容 |
|---|------|------|
| P3-1 | **messages蓄積からのsummaries生成** | fetch_discord_logsのDiscord API依存を撤廃し`messages`コレクションから要約（known-issue #7の恒久解＝BANリスク消滅・要約を30分毎に短縮可能→昇華レイテンシ4-5h→30分） |
| P3-2 | reflection（高次洞察） | 週1で各アクティブユーザーの記憶群から「この人は最近◯◯に凝っている」等の洞察を生成→importance高めでepisodicへ |
| P3-3 | 関係グラフ | 上記E |
| P3-4 | モダリティ拡張 | アーカイブスレッド取得（`threads/archived/public`）、リアクション集計の日報反映。画像/VCは費用対効果が薄いので保留 |
| P3-5 | クロスチャンネル継続 | 「別chでの直前のやり取り」をchannel_sessionsから軽量注入（同一ユーザーが5分以内に別chで話しかけた場合のみ） |

### 実装順の根拠
- P0は**既に壊れているもの**の修理（特にP0-1/P0-2は記憶が「貯まらない」根本原因）。
- P1は読み書き両方の器を作る＝以降の全機能の土台。
- P2のconsolidation/distillationはP1の器が無いと成立しない。
- P3-1は最大の構造改善だがsummarize系の書き直しを伴うため最後。

---

## 5. リスクと制約

| リスク | 対策 |
|--------|------|
| Mongo M0 512MB逼迫（messages 30日分＋user_memories無上限化） | 現使用量を先に計測。user_memoriesはembeddingが重い(3072float≒25KB/件)→**Float32のBinDataに圧縮(1/2)** or 上限500件/人。messages TTLは維持 |
| LLM呼び出し増による429悪化 | 抽出は既存呼び出しへの同梱で増やさない。新規呼び(蒸留・consolidation)は全てバッチ＝GitHub Actionsキー側。リアルタイム増分はゼロ設計 |
| プロンプト肥大→応答劣化・トークン費 | 注入は「ヒットしたものだけ・上限件数固定」を厳守。butler_history拡張は切り詰め方式 |
| 移行時のデータ破壊 | 移行スクリプトはコピー方式（旧配列は残す）→1週間併走→削除 |
| 実装済み未検証の山の再生産 | 各Phase末に本番smoke手順を必ず実施してから次へ（§6） |

## 6. 検証手順（各Phase共通テンプレ）
1. ローカルでユニット相当の関数直叩き（Mongoはテスト用DB名）。
2. main直push→Render自動deploy→ログで起動確認。
3. 実機smoke: ①普通に話しかける ②「覚えてる？」系 ③別chで話しかける ④再起動またぎ、の4シナリオ。
4. 翌日: daily_tasks実行ログ＋Mongoの書き込み結果を目視。

---

---

## 7. 実装状況（2026-07-07・第1次コミット分）

**✅ 実装済み（ローカル・compile確認済／未deploy・実機未検証）**
- **P0-1** enrich_memories: `is_booster:True`限定を撤廃→`xp>0 & optout除外`＋rotation(ROTATION_DAYS=5)＋cap(MAX_USERS_PER_RUN=25)＋`last_enriched`記録。非ブースターも日次で長期記憶が積み上がる。
- **P0-2** 記憶抽出の発火条件バグ撤廃: 全5箇所の `"（" in ai_text` を `startswith("（メイド")` に修正（on_message／slashコマンド／debut／advocacy／idle）。丸括弧を含む正常応答で会話履歴・memories・profileが保存されるように。**slashコマンド経路に `_extract_claims_and_memories` を追加**（従来profileのみだった）。
- **P1-3** butler_history 5→15往復（保存30件）。format_historyで直近5往復は全文・それ以前は80字に切り詰めてトークン節約。
- **P1-4** summaries RAGのトリガー語ゲート撤廃→embed先行＋standout閾値(0.62・env調整可)でも発火＋プロセス内embeddingキャッシュ(_load_summary_docs, TTL30分)。自然な想起質問を拾いつつcasual replyへの過剰注入を抑制。
- **P1-5** キュー溢れを無言dropから⏳リアクション(API1回・生成ゼロ)に。
- **P0-5** doc同期: data-model.md（memories40/butler30/messages TTL30d/要約4h）、batch-pipeline.md（daily_tasks分離・Tier3実装済・enrich全員化）。

**▶ 実行するだけ（コード既存・要ユーザー操作）**
- **P0-4** `reembed_memories.py` を live Mongo に対して実行（768次元等の非3072をre-embed・冪等・DRY_RUN対応）。

**⏳ 未着手（次の増分・live検証ループ必須）**
- P0-3 nickname_map汚染TTL。
- P1-1 `user_memories`独立コレクション化＋importance×recency×relevanceスコア。
- P1-2 `maid_memories`（自己記憶＋promiseトラッカ）。
- P2 `channel_sessions`／夜間consolidation／TTL前distillation／2キープール。
- P3 messages→summaries生成／reflection／関係グラフ／モダリティ拡張。

> 新コレクション系（P1-1/P1-2/P2/P3）は live Discord/Mongo での smoke なしに投入するとデータ破壊リスクがあるため、レビュー＋実機検証を挟む次増分として分離した。第1次コミット分は既存スキーマ内の修正のみで安全。

---

*作成: 2026-07-07 Claude Code（2並列解剖エージェントの調査結果に基づく）*
