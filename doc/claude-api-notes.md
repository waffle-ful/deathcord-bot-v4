# Claude API 運用ノート（2026-10-08 時点の調査結果）

空気くんに Anthropic Claude API を組み込むにあたり、公式一次情報から裏取りした料金・データ取扱い・機能の要点。
**数値は時点のもの。モデル変更や単価改定があればここと `main.py` / `batch/claude_util.py` の単価表を同時に更新すること。**

## 1. 料金（USD / 100万トークン）

出典: https://platform.claude.com/docs/en/about-claude/pricing

| モデル | 入力 | 出力 | キャッシュ読み | 5分キャッシュ書き | 1時間キャッシュ書き |
|---|---|---|---|---|---|
| Haiku 5.5（プロンプト ≤100k tok） | 0.10 | 0.50 | 0.01 | 0.125 | 0.20 |
| Haiku 5.5（プロンプト >100k tok） | **0.50** | **2.50** | 0.05 | 0.625 | 1.00 |
| Sonnet 5.5 | 2.00 | 10.00 | **0.10**（2026-10-07 に 0.20→0.10 へ半減） | 2.50 | 4.00 |
| Opus 5.5 | 4.00 | 20.00 | 0.20 | 5.00 | 8.00 |

- **Haiku 5.5 の 100k 段階**: 「そのリクエストの入力合計（通常入力＋キャッシュ読み＋キャッシュ書き）」が 100,000 を超えると**入力・出力とも 5 倍**。
  既に `main.py:_claude_cost_usd` と `claude_util.claude_cost_usd` は段階込みで計算している。
  batch 側は `CLAUDE_MAX_PROMPT_CHARS`（6万字）超を Claude に送らず Gemini へ回す設計で、高単価帯を回避している。
  会話側（main.py）は履歴＋RAG＋人格 system で 100k に届く可能性は低いが、`/focus` 級の長文は batch 経路。
- **Sonnet 5.5 のキャッシュ読み**: 0.10 は Haiku 5.5 ≤100k の 10 倍だが、Haiku >100k 帯（0.05）の 2 倍に過ぎない。
  長い固定 system を毎回読む用途では「Sonnet のキャッシュ読み」が意外に安い。品質が欲しい経路（mimic・レスバ）の候補。
- **Batch API**: 全モデル 50% 引きだが、ユーザー判断で不採用（日報等は即時性を優先）。
- **1M コンテキスト**: Haiku 5.5 以外は 1M まで単価据え置き。Haiku だけ 100k 超で跳ねる。
- **トークナイザ**: 4.7 以降のモデルは旧比で約 30% 多くトークンを消費する。Gemini 時代の文字数感覚で見積もらないこと。

## 2. データ取扱い（規約 §4-1 の根拠）

出典: Anthropic Privacy Center
- https://privacy.claude.com/en/articles/7996868-is-my-data-used-for-model-training
- https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-data

| 項目 | 内容 |
|---|---|
| 学習利用 | **商用 API の入出力は既定で学習に使わない**。例外は「利用者が明示的に許可」「フィードバック送信（👍👎）」のみ |
| 保持期間 | **受信から 30 日以内に自動削除** |
| 例外 | 自動の安全性検知で規約違反の疑いと判定された入出力は**最長 2 年**保持、分類スコアは最長 7 年 |
| ZDR（ゼロ保持） | 別途契約。個人 Console には無関係 |

Gemini 無料枠（学習利用あり・人間レビューあり）との差が、規約 v2 で「以前より安全側」と書ける根拠。
ただし **embedding（gemini-embedding-001）は常に Gemini 無料枠**に残る。保存する要約・記憶だけでなく、**メイドに話しかけた発言そのものが応答のたびに Gemini で埋め込まれる**（`search_memories` と日報RAG のクエリ埋め込み）。また `_call_model(MODEL_BOOSTER)` 直指定の4箇所（mimic 発話・mimic react・/相性コメント・昇格メッセージ）は Gemini が主のまま。規約 §4-2・パネル②・`doc/terms-evidence.md` に明記した。

## 3. Max プラン付属クレジット

出典: https://support.claude.com/en/articles/17154008

- Max 5x: 月 $100 / Max 20x: 月 $200。Messages API・Console Playground・Managed Agents・Agent SDK に使える。
- **請求サイクル末で失効、繰越なし**。付属クレジット → 購入クレジットの順に消費。
- 他にクレジットも auto-reload も無ければ、使い切った時点で API は止まる（**Claude プランに課金されることはない**）。
  ⇒ bot 側のソフト上限 $80 と Console のハード上限 $90 は、二重の保険として維持。
- 適用規約は「Supplemental Credit Terms」（紐付け時に同意）。データ取扱いは商用 API と同じ扱いとして規約に記載したが、Supplemental Credit Terms 本文は未読。**要一読**。

## 4. 現行実装との対応（2026-10-08）

| 実装 | 状態 |
|---|---|
| 会話・裏処理の先頭に Haiku 5.5（`_chat_chain` / `_background_chain`） | main push 済・実機未検証 |
| batch 6 本の先頭に Haiku 5.5（`claude_util.call_claude`、structured outputs） | main push 済・実機未検証 |
| 100k 段階込みの費用計算・月次ソフト上限 $80（Mongo `claude_usage`） | 実装済 |
| 人格ルールを system に分離＋`cache_control` | 実装済・**キャッシュヒット（`usage.cache_read_input_tokens`）未確認** |
| 規約 v2（Anthropic 追記）＋ `CLAUDE_MIN_TOS_VERSION=2` ガード（main/batch） | 本コミット |

## 5. 機能見直しの候補（優先順）

1. **キャッシュヒットの実測**: system に日時や乱数が混ざると無言で無効化される。ログに `cache_read_input_tokens` を出して 0 でないことを確認する。
2. **構造化出力を main.py 側の裏処理にも**: batch は schema 送信済み。記憶抽出・性格分析の JSON 崩れ事故を減らせる。
3. **tool use で RAG を引く**: 今はプロフィール・日報をプロンプトに詰め込んでいる。「この人の記憶を検索」をツール化すれば必要時だけ引く形になり、蒸し返し問題の根本対策になる。
4. **品質が欲しい経路だけ Sonnet 5.5**: mimic・レスバ・/focus の最終文章。キャッシュ読み半減で固定 system の長い経路ほど相対的に安い。
5. **Haiku の 100k 回避を会話側でも**: 念のため `count_tokens` か文字数で 100k 相当（日本語で概ね 7〜8 万字）を超えたら履歴を切る。
