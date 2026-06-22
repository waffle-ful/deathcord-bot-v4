"""
analyze_personality.py

ブースターユーザーの性格推定をハイブリッド方式で実行。

【設計: PERSONALITY_SPEC.md に準拠】
  - 性格は「自由記述で性格を吐かせる」のをやめ、検証済み尺度 TIPI-J（Big Five）を骨格に
    した証拠ベースの推定にする。
  - 主力はチャットログからの自動推論（Stage1: 言語特徴量 → Stage2: TIPI-J項目をLLM採点
    → Stage3: バンド化＋確信度 → Stage4: 安全マージ）。
  - 数値スコアは内部保持のみ。UIには 3段階バンド（低/中/高）＋確信度＋根拠引用だけ出す。
    （テキストからのLLM性格推定の実精度は r≈0.3 と低く、見せかけの精度を避けるため）

【保存フィールド】
  profile.bigfive            : Big Five 推定（band/confidence/evidence/score 各因子）
  profile.tone / vocabulary / communication_style : 口調・文体（stylometry・従来どおり）
  profile.background / relations / interests_vibe  : 社会的文脈（要約由来・従来どおり）
  profile.personality        : bigfive から生成する一言サマリ（後方互換・根拠あり）
"""

import os
import json
import re
import time
import bisect
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient
from google import genai
from google.genai import types

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MONGODB_URI    = os.environ["MONGODB_URI"]
MODEL          = "models/gemma-4-31b-it"
MODEL_FALLBACK = "models/gemma-4-26b-a4b-it"
DB_NAME        = "discord_bot_db"
MIN_CONV_COUNT = 30   # 10→30。10発言で性格を断定するのは統計的に無謀なため引き上げ
SUMMARY_DAYS   = 7

# 全員診断（メイドと話さない人も対象）の制御
MIN_UTTERANCES     = 15  # Big Five推定に必要な最低発言数（messages＋butler合算）
MAX_USERS_PER_RUN  = 25  # 1回のバッチで分析する最大人数（タイムアウト対策）
ROTATION_DAYS      = 5   # 直近この日数内に分析済みの人はローテーションでスキップ
MIN_COHORT         = 8   # 行動パーセンタイル算出に必要な最低母集団（これ未満は沈黙）

# 確信度の順序（安全マージで大小比較に使う）
CONF_RANK = {None: -1, "low": 0, "mid": 1, "high": 2}

# =============================================================================
# TIPI-J（日本語版 Ten Item Personality Inventory / 小塩ら 2012）
#   各項目を 1(まったく違う)〜7(強くそう思う) でLLMが本人に代わって採点する。
#   factor: 因子, reverse: 逆転項目か（採点時 8-x で反転）
# =============================================================================
TIPI_ITEMS = {
    "1":  {"text": "活発で、外向的だと思う",                 "factor": "extraversion",      "reverse": False},
    "2":  {"text": "他人に不満をもち、もめごとを起こしやすいと思う", "factor": "agreeableness",     "reverse": True},
    "3":  {"text": "しっかりしていて、自分に厳しいと思う",     "factor": "conscientiousness", "reverse": False},
    "4":  {"text": "心配性で、うろたえやすいと思う",           "factor": "neuroticism",       "reverse": False},
    "5":  {"text": "新しいことが好きで、変わった考えをもつと思う", "factor": "openness",          "reverse": False},
    "6":  {"text": "ひかえめで、おとなしいと思う",             "factor": "extraversion",      "reverse": True},
    "7":  {"text": "人に気をつかう、やさしい人間だと思う",     "factor": "agreeableness",     "reverse": False},
    "8":  {"text": "だらしなく、うっかりしていると思う",       "factor": "conscientiousness", "reverse": True},
    "9":  {"text": "冷静で、気分が安定していると思う",         "factor": "neuroticism",       "reverse": True},
    "10": {"text": "発想力に欠けた、平凡な人間だと思う",       "factor": "openness",          "reverse": True},
}

FACTOR_JA = {
    "openness":          "開放性",
    "conscientiousness": "誠実性",
    "extraversion":      "外向性",
    "agreeableness":     "協調性",
    "neuroticism":       "情緒不安定さ",
}
# チャットからは推定が弱い因子（確信度を high にしない）
WEAK_FACTORS = {"neuroticism", "conscientiousness"}

# =============================================================================
# プロンプト
# =============================================================================

# ① 口調・語彙（stylometry・従来どおり / 性格科学とは別物として温存）
TONE_PROMPT = """\
以下はDiscordユーザー「{name}」の実際の発言ログです。
この発言から「口調・語彙・テンション」のみを分析してください。
性格や背景の推測は不要です。発言そのものの特徴だけを見てください。
必ずJSON形式のみで返してください。前置き・説明文・```は不要です。

出力形式:
{{
  "tone": "口調の特徴（例: 敬語なし・語尾に「〜ね」多用・テンション高め・短文多め）",
  "communication_style": "話し方の特徴（例: 質問が多い・リアクションが早い・絵文字をよく使う・ツッコミ役）",
  "vocabulary": "よく使う語彙・口癖（例: 「やばい」「草」「〜じゃん」・カタカナ多め）"
}}

【{name}の発言ログ】
{utterances}
"""

# ② 社会的文脈（背景・人間関係・関心 / 要約由来・従来どおり）
CONTEXT_PROMPT = """\
以下はDiscordサーバーの要約ログです（直近{days}日分）。
この中に「{name}」というメンバーの情報が含まれている場合、
「サーバー内の立場・他メンバーとの関係性・関心」を分析してください。
言及が少ない場合は全項目nullにしてください。性格そのものの断定はしないでください。
必ずJSON形式のみで返してください。前置き・説明文・```は不要です。

出力形式:
{{
  "background": "サーバー内の立場・役職（例: シニアマネージャー・創設メンバーの一人）",
  "relations": "よく絡むメンバーや関係性（例: 花子と特に親密・新規メンバーの案内役）",
  "interests_vibe": "関心・雰囲気（例: ゲーム全般・深夜帯によく出没・盛り上げ役）"
}}

【要約ログ】
{summaries}
"""

# ③ Big Five 推定（TIPI-J 10項目をログ証拠から本人に代わって採点）
BIGFIVE_PROMPT = """\
あなたは性格心理学の知見をもつアナリストです。以下はDiscordユーザー「{name}」の実際の発言ログと、
そこから機械的に抽出した言語シグナルです。これらの「観察可能な証拠」だけに基づいて、
心理尺度 TIPI-J の10項目について、本人がどう自己評価しそうかを 1〜7 で推定してください。

採点ルール（厳守）:
- 1=まったく違う / 4=どちらでもない / 7=強くそう思う。
- 各項目に必ず「根拠となった実際の発言の引用」を1つ添えること。引用できない＝証拠が無い項目は
  score を null にすること（推測・捏造・バーナム的な一般論は禁止）。
- ログに表れていない内面は無理に埋めない。null を恐れないこと。
- 言語シグナルはヒント。最終判断は発言内容を優先する。

必ず次のJSON形式のみで返してください。前置き・説明文・```は不要です。
{{
  "items": {{
    "1": {{"score": 1-7またはnull, "evidence": "根拠の実発言（短く引用）またはnull"}},
    "2": {{"score": ..., "evidence": ...}},
    ... "10" まで全項目
  }}
}}

【TIPI-J 10項目（「私は自分自身を、○○ と思う」）】
{items}

【{name} の発言から抽出した言語シグナル】
{signals}

【{name} の発言ログ】
{utterances}
"""


# =============================================================================
# ユーティリティ
# =============================================================================

def _extract_retry_wait(err: str) -> float:
    m = re.search(r"retry in ([\d.]+)s", err)
    return float(m.group(1)) + 2.0 if m else 60.0


def call_gemma(client: genai.Client, prompt: str, max_tokens: int = 500) -> str | None:
    for model, label in [(MODEL, "main"), (MODEL_FALLBACK, "fallback")]:
        for attempt in range(5):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        max_output_tokens=max_tokens,
                    ),
                )
                text = getattr(resp, "text", None)
                if text and text.strip():
                    return text.strip()
                print(f"[WARN] {label}: 空レスポンス attempt{attempt+1}")
                time.sleep(5)
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    wait = _extract_retry_wait(err)
                    print(f"[WARN] 429 ({label}) attempt{attempt+1}, {wait:.1f}s待機...")
                    time.sleep(wait)
                elif "503" in err or "UNAVAILABLE" in err or "high demand" in err.lower():
                    wait = (attempt + 1) * 20
                    print(f"[WARN] 503 ({label}) attempt{attempt+1}, {wait}s待機...")
                    time.sleep(wait)
                else:
                    print(f"[ERROR] {label}: {e}")
                    break
    return None


def parse_json(raw: str, name: str) -> dict | None:
    if not raw:
        return None
    try:
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        result  = json.loads(cleaned)
        if not any(v for v in result.values() if v):
            return None
        return result
    except json.JSONDecodeError:
        print(f"[WARN] JSON parse failed for {name}: {raw[:80]}")
        return None


def get_user_utterances(history: list, limit: int = 150) -> list[str]:
    lines = [h["content"] for h in history if h.get("role") == "user" and h.get("content")]
    return lines[-limit:]


def fetch_summaries(summaries_col, days: int, limit: int = 30) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    docs = list(
        summaries_col.find({"created_at": {"$gte": since.isoformat()}})
                     .sort("created_at", -1)
                     .limit(limit)
    )
    if not docs:
        return ""
    docs.reverse()
    parts = [f"[{d.get('created_at', '')[:10]}]\n{d.get('summary', '')}" for d in docs if d.get("summary")]
    return "\n\n---\n\n".join(parts)


_MENTION_RE = re.compile(r"<@!?\d+>")


def fetch_user_messages(messages_col, uid: str, limit: int = 150) -> list[str]:
    """messagesコレクションからユーザーの一般チャット発言を新しい順→時系列で取得。"""
    docs = list(
        messages_col.find({"author_id": uid}, {"content": 1})
                    .sort("timestamp", -1)
                    .limit(limit)
    )
    out = []
    for d in docs:
        c = _MENTION_RE.sub("", (d.get("content") or "")).strip()
        if c:
            out.append(c)
    out.reverse()
    return out


def get_all_utterances(messages_col, doc: dict, limit: int = 150) -> list[str]:
    """一般チャット(messages)を主、メイド会話(butler_history)を補助に発言を集める。

    messagesは全発言の上位集合（メイドへのメンションも含む）なので主データ源にする。
    古いユーザーでmessagesが空ならbutler_historyにフォールバック。
    """
    msgs = fetch_user_messages(messages_col, str(doc["_id"]), limit)
    if msgs:
        return msgs
    return get_user_utterances(doc.get("butler_history", []), limit)


def needs_rotation(doc: dict, now: datetime, days: int) -> bool:
    """直近days日以内にBig Five分析済みならスキップ（未分析・古いものを優先）。"""
    at = ((doc.get("profile") or {}).get("bigfive") or {}).get("analyzed_at")
    if not at:
        return True
    try:
        return (now - datetime.fromisoformat(at)).days >= days
    except (ValueError, TypeError):
        return True


# =============================================================================
# Stage 1: 言語特徴量（Python・決定論的・再現性あり）
# =============================================================================

GRATITUDE_RE = re.compile(r"ありがと|助か|感謝|嬉し|いいね|最高|すごい|優し|好き|ナイス|お疲れ")
NEGATIVE_RE  = re.compile(r"不安|しんど|つら|無理|怖|心配|だめ|ダメ|最悪|疲れ|鬱|落ち込|きつ|ごめん|すみま")
CURIOSITY_RE = re.compile(r"なんで|なぜ|気にな|知りた|どうやって|どうして|面白そう|試して|新し")
AGGRESSION_RE = re.compile(r"は\?|うざ|きも|黙れ|死ね|くそ|クソ|ふざけ|むかつ|嫌い")
EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF" "\U00002B00-\U00002BFF" "]"
)
TOKEN_RE = re.compile(r"[ぁ-んァ-ヶ一-龠a-zA-Z0-9]+")


def _rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def compute_signals(utterances: list[str]) -> dict:
    """発言リストから因子の事前シグナルを算出する。"""
    n = len(utterances)
    if n == 0:
        return {"n_msgs": 0}

    joined   = "\n".join(utterances)
    lengths  = [len(u) for u in utterances]
    avg_len  = sum(lengths) / n

    exclam   = _rate(sum(1 for u in utterances if "！" in u or "!" in u), n)
    question = _rate(sum(1 for u in utterances if "？" in u or "?" in u), n)
    emoji    = _rate(sum(len(EMOJI_RE.findall(u)) for u in utterances), n)  # 1発言あたり絵文字数
    mention  = _rate(sum(1 for u in utterances if "@" in u or "さん" in u or "くん" in u or "ちゃん" in u), n)

    gratitude  = len(GRATITUDE_RE.findall(joined))
    negative   = len(NEGATIVE_RE.findall(joined))
    curiosity  = len(CURIOSITY_RE.findall(joined))
    aggression = len(AGGRESSION_RE.findall(joined))

    tokens = TOKEN_RE.findall(joined)
    lexdiv = len(set(tokens)) / len(tokens) if tokens else 0.0  # 語彙多様性(異なり語比)

    return {
        "n_msgs": n,
        "avg_len": round(avg_len, 1),
        "exclam_rate": round(exclam, 2),
        "question_rate": round(question, 2),
        "emoji_per_msg": round(emoji, 2),
        "mention_rate": round(mention, 2),
        "gratitude_hits": gratitude,
        "negative_hits": negative,
        "curiosity_hits": curiosity,
        "aggression_hits": aggression,
        "lexical_diversity": round(lexdiv, 2),
    }


def signals_to_text(s: dict) -> str:
    if s.get("n_msgs", 0) == 0:
        return "（発言なし）"
    return (
        f"- 発言数: {s['n_msgs']} / 平均文長: {s['avg_len']}文字\n"
        f"- 外向性の手がかり: 感嘆符率={s['exclam_rate']} 絵文字/発言={s['emoji_per_msg']} メンション率={s['mention_rate']}\n"
        f"- 協調性の手がかり: 感謝・称賛語={s['gratitude_hits']}回 攻撃的語={s['aggression_hits']}回\n"
        f"- 開放性の手がかり: 語彙多様性={s['lexical_diversity']} 好奇心語={s['curiosity_hits']}回 質問率={s['question_rate']}\n"
        f"- 情緒の手がかり(参考・低信頼): ネガ感情語={s['negative_hits']}回"
    )


# =============================================================================
# 行動シグナル → コホート相対パーセンタイル（自己-行動 乖離レイヤーの「観測側」土台）
#   ※ここで使うのは Stage1 の決定論シグナルのみ。LLM推定(bigfive)は混ぜない。
#     生の率は単体では高低を判断できないため、サーバー内（コホート）相対順位に変換する。
#   ※SOKA: 行動が自己申告と同等以上に妥当なのは外向性(E)。開放性(O)は脇役・低確信。
#     N/A/C は行動シグナルが弱いのでパーセンタイル化しない（乖離レイヤーでも沈黙）。
# =============================================================================

def _signal_features(raw: dict) -> dict:
    """raw signals から E/O の連続特徴量を取り出す（パーセンタイル化前）。
    O系はボリューム(=外向性)との交絡を避けるため発言数で正規化する。"""
    n = max(raw.get("n_msgs", 0), 1)
    return {
        "E_n_msgs":    raw.get("n_msgs", 0),
        "E_avg_len":   raw.get("avg_len", 0.0),
        "E_exclam":    raw.get("exclam_rate", 0.0),
        "E_emoji":     raw.get("emoji_per_msg", 0.0),
        "E_mention":   raw.get("mention_rate", 0.0),
        "O_lexdiv":    raw.get("lexical_diversity", 0.0),
        "O_curiosity": raw.get("curiosity_hits", 0) / n,
        "O_question":  raw.get("question_rate", 0.0),
    }

_E_FEATS = ["E_n_msgs", "E_avg_len", "E_exclam", "E_emoji", "E_mention"]
_O_FEATS = ["O_lexdiv", "O_curiosity", "O_question"]


def _pct_rank(sorted_vals: list, x: float) -> int:
    """ソート済み母集団に対する x のパーセンタイル順位(0-100)。"""
    i = bisect.bisect_right(sorted_vals, x)
    return round(i / len(sorted_vals) * 100)


def compute_cohort_percentiles(users_col) -> int:
    """全ユーザの behavior_signals.raw から E/O のコホート相対パーセンタイルを算出し、
    behavior_signals.pct を更新する。母集団が MIN_COHORT 未満なら付けない（沈黙）。
    返り値は pct を更新した人数。"""
    docs = list(users_col.find(
        {"profile.behavior_signals.raw": {"$exists": True},
         "personality_optout": {"$ne": True}},  # オプトアウト者は母集団からも除外
        {"profile.behavior_signals.raw": 1},
    ))
    if len(docs) < MIN_COHORT:
        print(f"[personality] cohort too small ({len(docs)}<{MIN_COHORT}) - pct skip")
        return 0

    feats = {d["_id"]: _signal_features(d["profile"]["behavior_signals"]["raw"]) for d in docs}
    dists = {k: sorted(f[k] for f in feats.values()) for k in (_E_FEATS + _O_FEATS)}

    updated = 0
    for uid, f in feats.items():
        e_ranks = [_pct_rank(dists[k], f[k]) for k in _E_FEATS]
        o_ranks = [_pct_rank(dists[k], f[k]) for k in _O_FEATS]
        pct = {
            "extraversion": round(sum(e_ranks) / len(e_ranks)),
            "openness":     round(sum(o_ranks) / len(o_ranks)),
        }
        users_col.update_one({"_id": uid}, {"$set": {"profile.behavior_signals.pct": pct}})
        updated += 1
    print(f"[personality] cohort percentiles updated for {updated} users (n={len(docs)})")
    return updated


# =============================================================================
# Stage 2-3: TIPI採点 → 因子スコア → バンド・確信度
# =============================================================================

def items_block() -> str:
    return "\n".join(f"{k}. {v['text']}" for k, v in TIPI_ITEMS.items())


def _band(val: float) -> str:
    if val <= 3.5:
        return "低"
    if val >= 5.0:
        return "高"
    return "中"


def _base_confidence(n_msgs: int) -> str:
    if n_msgs >= 80:
        return "high"
    if n_msgs >= 30:
        return "mid"
    return "low"


def _drop(conf: str) -> str:
    order = ["low", "mid", "high"]
    return order[max(0, order.index(conf) - 1)]


def score_bigfive(items: dict, signals: dict) -> dict | None:
    """LLMの項目採点(1-7)を5因子のband/confidence/evidence/scoreに変換する。"""
    n_msgs   = signals.get("n_msgs", 0)
    base     = _base_confidence(n_msgs)
    result   = {}

    for factor in FACTOR_JA:
        # この因子に属する項目を集める（逆転は 8-x）
        vals, evs = [], []
        for key, meta in TIPI_ITEMS.items():
            if meta["factor"] != factor:
                continue
            entry = items.get(key) or {}
            score = entry.get("score")
            if isinstance(score, (int, float)) and 1 <= score <= 7:
                vals.append(8 - score if meta["reverse"] else score)
                ev = entry.get("evidence")
                if ev and ev not in ("null", None):
                    evs.append(str(ev))
        if not vals:
            continue  # 証拠なし → この因子は出さない（null）

        val   = sum(vals) / len(vals)
        conf  = base
        if factor in WEAK_FACTORS and conf == "high":
            conf = "mid"               # N/C はチャットから当てにくい
        if len(vals) < 2:
            conf = _drop(conf)         # 片方の項目しか採点できなかった
        if not evs:
            conf = _drop(conf)         # 根拠引用が取れなかった

        result[factor] = {
            "band":       _band(val),
            "confidence": conf,
            "evidence":   evs[0] if evs else None,
            "score":      round((val - 1) / 6 * 100),   # 内部・較正用（UIには出さない）
        }

    if not result:
        return None
    result["method"]      = "inferred"
    result["data_points"] = n_msgs
    result["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    return result


def render_personality_summary(bigfive: dict) -> str | None:
    """bigfiveバンドから後方互換用の一言サマリを生成（根拠ある記述）。"""
    highs, lows = [], []
    for factor in FACTOR_JA:
        f = bigfive.get(factor)
        if not isinstance(f, dict):
            continue
        if f.get("band") == "高":
            highs.append(FACTOR_JA[factor])
        elif f.get("band") == "低":
            lows.append(FACTOR_JA[factor])
    parts = []
    if highs:
        parts.append("・".join(highs) + "が高め")
    if lows:
        parts.append("・".join(lows) + "は控えめ")
    return "（推定）" + "、".join(parts) if parts else None


def infer_bigfive(client: genai.Client, name: str, utterances: list[str]) -> dict | None:
    if not utterances:
        return None
    signals = compute_signals(utterances)
    prompt  = BIGFIVE_PROMPT.format(
        name=name,
        items=items_block(),
        signals=signals_to_text(signals),
        utterances="\n".join(f"- {u}" for u in utterances),
    )
    raw = call_gemma(client, prompt, max_tokens=1200)
    parsed = parse_json(raw, name)
    if not parsed or "items" not in parsed:
        return None
    return score_bigfive(parsed["items"], signals)


# =============================================================================
# 既存サブ分析（口調・社会的文脈）— 従来どおり
# =============================================================================

def analyze_tone(client: genai.Client, name: str, utterances: list[str]) -> dict | None:
    if not utterances:
        return None
    text = "\n".join(f"- {u}" for u in utterances)
    raw = call_gemma(client, TONE_PROMPT.format(name=name, utterances=text), max_tokens=300)
    return parse_json(raw, name)


def analyze_context(client: genai.Client, name: str, summaries_text: str) -> dict | None:
    if not summaries_text:
        return None
    raw = call_gemma(
        client,
        CONTEXT_PROMPT.format(name=name, days=SUMMARY_DAYS, summaries=summaries_text),
        max_tokens=800,
    )
    return parse_json(raw, name)


# =============================================================================
# Stage 4: 安全マージ（既知バグの恒久修正）
#   - bigfive は因子ごとに「新しい確信度 >= 既存の確信度」のときだけ更新。
#     null や low で既存の非null/highを潰さない。
#   - 口調・文脈フィールドは新しい非null値があれば更新（既存は消さない）。
# =============================================================================

def merge_bigfive(existing: dict, new: dict | None) -> dict | None:
    if not new:
        return existing or None
    merged = dict(existing or {})
    for factor in FACTOR_JA:
        nf = new.get(factor)
        if not nf:
            continue  # 新データに無い因子は既存を温存
        ef = merged.get(factor)
        if not ef or CONF_RANK[nf["confidence"]] >= CONF_RANK.get((ef or {}).get("confidence")):
            merged[factor] = nf
    # メタは常に最新化（少なくとも1因子が入っている場合）
    if any(isinstance(merged.get(f), dict) for f in FACTOR_JA):
        merged["method"]      = "inferred"
        merged["data_points"] = new.get("data_points", merged.get("data_points", 0))
        merged["analyzed_at"] = new.get("analyzed_at")
        return merged
    return existing or None


def safe_merge(existing: dict, tone: dict | None, context: dict | None,
               bigfive: dict | None) -> tuple[dict, bool]:
    """既存プロフィールへ新規分析を非破壊マージ。confident な bigfive が入ったかを返す。"""
    merged = dict(existing)

    # 口調・文脈: 新しい非null値だけ上書き（既存は決して消さない）
    for src in (tone, context):
        if src:
            for k, v in src.items():
                if v:
                    merged[k] = v

    # Big Five: 確信度ベースの安全マージ
    merged_bf = merge_bigfive(existing.get("bigfive", {}), bigfive)
    has_confident = False
    if merged_bf:
        merged["bigfive"] = merged_bf
        has_confident = any(
            isinstance(merged_bf.get(f), dict) and merged_bf[f]["confidence"] in ("mid", "high")
            for f in FACTOR_JA
        )
        summary = render_personality_summary(merged_bf)
        if summary:
            merged["personality"] = summary  # 後方互換フィールドを根拠ある内容で更新

    return merged, has_confident


# =============================================================================
# メイン
# =============================================================================

def select_targets(users_col, now: datetime) -> list[dict]:
    """分析対象を選ぶ。メイドと活発に話した人(conv_count)を最優先し、
    残り枠を「まだ/久しく分析していない一般チャット民」でローテーション補充する。
    メイドと話さない人もmessagesログから診断できるようにするのが狙い。"""
    all_users = list(users_col.find({
        "xp":                 {"$gt": 0},
        "name":               {"$exists": True},
        "personality_optout": {"$ne": True},
    }))
    maid_fresh = [d for d in all_users if d.get("conv_count", 0) >= MIN_CONV_COUNT]
    maid_ids   = {d["_id"] for d in maid_fresh}
    rotation   = [d for d in all_users
                  if d["_id"] not in maid_ids and needs_rotation(d, now, ROTATION_DAYS)]
    # 未分析(analyzed_at無し)→古い順に並べ、公平に巡回させる
    rotation.sort(key=lambda d: ((d.get("profile") or {}).get("bigfive") or {}).get("analyzed_at") or "")
    return maid_fresh + rotation


def main():
    print("[personality] Connecting to MongoDB...")
    mongo         = MongoClient(MONGODB_URI)
    users_col     = mongo[DB_NAME]["users"]
    summaries_col = mongo[DB_NAME]["summaries"]
    messages_col  = mongo[DB_NAME]["messages"]

    now        = datetime.now(timezone.utc)
    candidates = select_targets(users_col, now)
    print(f"[personality] Candidates: {len(candidates)} (cap {MAX_USERS_PER_RUN}/run)")
    if not candidates:
        print("[personality] No users to analyze. Done.")
        return

    print("[personality] Fetching summaries...")
    summaries_text = fetch_summaries(summaries_col, SUMMARY_DAYS)
    print(f"[personality] Summaries: {len(summaries_text)}文字")

    client    = genai.Client(api_key=GEMINI_API_KEY)
    success   = 0
    processed = 0

    for doc in candidates:
        if processed >= MAX_USERS_PER_RUN:
            print(f"[personality] Reached cap ({MAX_USERS_PER_RUN}). 残りは次回に回します。")
            break

        uid        = str(doc["_id"])
        name       = doc.get("name", uid)
        utterances = get_all_utterances(messages_col, doc)
        if len(utterances) < MIN_UTTERANCES:
            continue  # 発言が少なすぎる人は枠を消費せずスキップ

        processed += 1
        print(f"[personality] Analyzing {name} (発言{len(utterances)}件, conv_count={doc.get('conv_count', 0)})...")

        # ① 口調（stylometry） ② Big Five 推定（主力） ③ 社会的文脈
        tone = analyze_tone(client, name, utterances)
        time.sleep(3)
        bigfive = infer_bigfive(client, name, utterances)
        print(f"  bigfive: {bigfive}")
        time.sleep(3)
        context = analyze_context(client, name, summaries_text)
        time.sleep(3)

        # ④ 安全マージ（既存を破壊しない）＋ Stage1行動シグナルを常時永続化
        existing = doc.get("profile", {})
        merged, has_confident = safe_merge(existing, tone, context, bigfive)
        # 決定論シグナルはLLM失敗時も有効。乖離レイヤーの「観測側」土台として常に保存する。
        merged["behavior_signals"] = {
            "raw":         compute_signals(utterances),
            "computed_at": now.isoformat(),
        }

        update = {"$set": {"profile": merged}}
        # conv_count は confident な性格推定が保存できたときだけリセット。
        # 失敗時にリセットすると再分析トリガーを失うため（旧バグの再発防止）。
        if has_confident and doc.get("conv_count", 0) >= MIN_CONV_COUNT:
            update["$unset"] = {"conv_count": ""}

        users_col.update_one({"_id": doc["_id"]}, update)
        if not tone and not context and not bigfive:
            print(f"[personality] {name}: LLM分析は空（行動シグナルのみ保存）")
        else:
            print(f"[personality] Saved {name} (confident={has_confident})")
        success += 1
        time.sleep(5)

    # 全ユーザの行動シグナルからコホート相対パーセンタイルを再計算（乖離レイヤー用）。
    # per-userの本体保存はループ内で完了済みなので、ここで失敗してもジョブを落とさない（非致命）。
    print("[personality] Recomputing cohort behavior percentiles...")
    try:
        compute_cohort_percentiles(users_col)
    except Exception as e:
        print(f"[personality] cohort percentile step failed (non-fatal): {e}")

    print(f"[personality] Completed. {success}/{processed} analyzed (candidates={len(candidates)}).")


if __name__ == "__main__":
    main()
