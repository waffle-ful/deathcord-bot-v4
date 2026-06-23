"""
focus_summary.py

特定メンバー or キーワードに絞った要約を作成し、
人物の場合はMongoDBのプロフィールに自動反映する。

環境変数:
  FOCUS_TYPE:   "member" or "keyword"
  FOCUS_TARGET: メンバーID or キーワード文字列
  FOCUS_NAME:   表示名（メンバー表示名 or キーワード）
"""

import os
import json
import re
import time
from datetime import datetime, timezone, timedelta

import requests
from pymongo import MongoClient
from google import genai
from google.genai import types

FOCUS_TYPE    = os.environ["FOCUS_TYPE"]    # "member" or "keyword"
FOCUS_TARGET  = os.environ["FOCUS_TARGET"]  # user_id or keyword
FOCUS_NAME    = os.environ["FOCUS_NAME"]    # 表示名

DISCORD_BOT_TOKEN  = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_GUILD_ID   = int(os.environ["DISCORD_GUILD_ID"])
CHANNEL_IDS_RAW    = os.environ.get("DISCORD_CHANNEL_IDS", "")
CHANNEL_IDS        = [int(c.strip()) for c in CHANNEL_IDS_RAW.split(",") if c.strip()]
EXCLUDE_IDS_RAW    = os.environ.get("EXCLUDE_CHANNEL_IDS", "")
EXCLUDE_IDS        = set(int(c.strip()) for c in EXCLUDE_IDS_RAW.split(",") if c.strip())
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
MONGODB_URI        = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
SUMMARY_CHANNEL_ID = os.environ["SUMMARY_CHANNEL_ID"]

MODEL          = "models/gemma-4-31b-it"
MODEL_FALLBACK = "models/gemma-4-26b-a4b-it"
DB_NAME        = "discord_bot_db"
JST            = timezone(timedelta(hours=9))
FETCH_DAYS     = 7    # 直近何日分の生ログを取得するか
SUMMARY_LOOKBACK_DAYS = 90     # 要約アーカイブを何日分さかのぼるか
SUMMARY_MAX_DOCS      = 1200   # 走査する要約ドキュメント上限（安全弁。90日×12件/日≈1080をカバー）
MAX_LOG_CHARS         = 25000  # 生ログをプロンプトに入れる最大文字数（旧10000の切り詰めを撤廃）
MAX_SUMMARY_CHARS     = 12000  # 要約抜粋をプロンプトに入れる最大文字数
MAX_MEMBER_CONTEXT_CHARS = 4000  # 既知情報(profile/claims/memories)をプロンプトに入れる最大文字数(Tier2)

# =============================================================================
# プロンプト
# =============================================================================

MEMBER_FOCUS_PROMPT = """\
以下はDiscordサーバーの「長期の要約アーカイブ（過去数ヶ月）」と「直近{days}日分の生チャットログ」です。
両方を突き合わせ、長期の傾向と直近の動きの両面から、
「{name}」というメンバーの発言・言及に絞って分析し、
この人物についての詳細な観察レポートを書いてください。
冒頭に「これまでに蓄積した既知情報」が提示される場合があります。それは過去の分析の蓄積であり、
今回のログ・要約で裏付け・更新・新情報の追加を行い、矛盾があれば新しい証拠を優先してください。
古い情報の単なる引き写しは避け、変化や新事実を重視すること。
前置き・導入文は不要です。各セクションの見出しから即座に書き始めてください。

## 発言の口調・語彙
## 性格・行動パターン
## サーバー内での立場・役割
## よく絡むメンバーと関係性
## 関心・よく話すトピック
## 印象的な発言・エピソード
## 総合評価
"""

KEYWORD_FOCUS_PROMPT = """\
以下はDiscordサーバーの「長期の要約アーカイブ（過去数ヶ月）」と「直近{days}日分の生チャットログ」です。
両方を突き合わせ、長期の経緯と直近の状況の両面から、
「{keyword}」に関する発言・話題に絞って分析し、
このトピックについての詳細なレポートを書いてください。
前置き・導入文は不要です。各セクションの見出しから即座に書き始めてください。

## このトピックの概要
## 主な議論の流れ
## 関わったメンバーと立場
## 盛り上がった瞬間・転換点
## 結論・現在の状況
## 関連する他のトピック
"""

PROFILE_UPDATE_PROMPT = """\
以下は「{name}」についての観察レポートです。
このレポートから、Discordメイドボットが会話に使う
プロフィール情報をJSON形式で抽出してください。
前置き・説明文・```は不要です。JSONのみ出力してください。
情報が不明な項目はnullにしてください。

出力形式:
{{
  "tone": "口調の特徴（例: テンション高め・敬語なし・短文多め）",
  "vocabulary": "よく使う語彙・口癖",
  "personality": "性格の概要",
  "background": "サーバー内の立場・役割",
  "relations": "よく絡むメンバーや関係性",
  "interests_vibe": "関心・よく話すトピック・雰囲気"
}}

【観察レポート】
{report}
"""

SECTION_ICONS_MEMBER = {
    "発言の口調": "🗣️", "性格": "🧠", "立場": "👑",
    "よく絡む": "🤝", "関心": "💡", "印象的": "⭐", "総合": "📋",
}
SECTION_ICONS_KEYWORD = {
    "概要": "📌", "議論の流れ": "🔄", "関わった": "👥",
    "盛り上がった": "🔥", "結論": "✅", "関連": "🔗",
}


# =============================================================================
# ログ取得
# =============================================================================

BASE_URL     = "https://discord.com/api/v10"
REST_HEADERS = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
    "Content-Type":  "application/json",
}


def _api_get(path: str, params: dict = None):
    url = BASE_URL + path
    for attempt in range(5):
        resp = requests.get(url, headers=REST_HEADERS, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            print(f"[fetch] Rate limited. Waiting {retry_after:.1f}s...")
            time.sleep(retry_after + 0.5)
            continue
        if resp.status_code in (403, 404):
            return None
        print(f"[fetch] API error {resp.status_code}: {path}")
        time.sleep(1)
    return None


def _datetime_to_snowflake(dt: datetime) -> int:
    discord_epoch = 1420070400000
    return (int(dt.timestamp() * 1000) - discord_epoch) << 22


def is_excluded(channel: dict) -> bool:
    if channel.get("nsfw", False):
        return True
    return int(channel["id"]) in EXCLUDE_IDS


def fetch_logs(days: int) -> list[dict]:
    after_dt     = datetime.now(timezone.utc) - timedelta(days=days)
    after_sf     = _datetime_to_snowflake(after_dt)
    TEXT_TYPES   = {0, 5}
    THREAD_TYPES = {11, 12}

    all_channels = _api_get(f"/guilds/{DISCORD_GUILD_ID}/channels") or []

    if CHANNEL_IDS:
        id_set = set(CHANNEL_IDS)
        base = [ch for ch in all_channels if int(ch["id"]) in id_set and ch["type"] in TEXT_TYPES]
    else:
        base = [ch for ch in all_channels if ch["type"] in TEXT_TYPES]

    targets    = []
    parent_ids = set()
    for ch in base:
        if is_excluded(ch):
            continue
        targets.append(ch)
        parent_ids.add(int(ch["id"]))

    thread_resp = _api_get(f"/guilds/{DISCORD_GUILD_ID}/threads/active") or {}
    for t in thread_resp.get("threads", []):
        if int(t.get("parent_id", 0)) in parent_ids and not is_excluded(t) and t["type"] in THREAD_TYPES:
            targets.append(t)

    msgs = []
    for ch in targets:
        ch_id   = int(ch["id"])
        ch_name = ch.get("name", str(ch_id))
        last_id = after_sf
        while True:
            params = {"limit": 100, "after": str(last_id)}
            batch  = _api_get(f"/channels/{ch_id}/messages", params=params)
            if not batch:
                break
            batch = list(reversed(batch))
            if not batch:
                break
            for msg in batch:
                author = msg.get("author", {})
                if author.get("bot") or msg.get("webhook_id"):
                    continue
                msgs.append({
                    "channel":   ch_name,
                    "author":    author.get("global_name") or author.get("username", ""),
                    "author_id": author.get("id", ""),
                    "timestamp": msg.get("timestamp", ""),
                    "content":   msg.get("content", ""),
                })
            last_id = int(batch[-1]["id"])
            if len(batch) < 100:
                break
            time.sleep(0.3)
        time.sleep(0.2)

    msgs.sort(key=lambda m: m["timestamp"])
    print(f"[fetch] {len(msgs)} messages")
    return msgs


def filter_logs(msgs: list[dict], needles: list[str] = None) -> str:
    """フォーカス対象に絞ってログをテキスト化。

    member の場合、needles に nickname_map のエイリアスを含めると
    「たろー」のような愛称での発言・言及も拾える。
    """
    filtered = []

    if FOCUS_TYPE == "member":
        target_id = FOCUS_TARGET
        nlist     = [n.lower() for n in (needles or [FOCUS_NAME]) if n]
        for m in msgs:
            author_l  = m["author"].lower()
            content_l = m["content"].lower()
            # 発言者が対象 or メッセージ内に名前/エイリアス/IDが含まれる
            is_author  = m["author_id"] == target_id or any(n in author_l for n in nlist)
            is_mention = any(n in content_l for n in nlist) or f"<@{target_id}>" in m["content"]
            if is_author or is_mention:
                filtered.append(m)
    else:
        kw = FOCUS_TARGET.lower()
        for m in msgs:
            if kw in m["content"].lower() or kw in m["channel"].lower():
                filtered.append(m)

    lines = []
    for m in filtered:
        ts = m["timestamp"][:16].replace("T", " ")
        lines.append(f"[{ts}] #{m['channel']} {m['author']}: {m['content']}")
    return "\n".join(lines)


# =============================================================================
# 長期アーカイブ（summaries）からの絞り込み
# =============================================================================

def _strip_title_decoration(name: str) -> set[str]:
    """「二つ名」本名 / 【二つ名】本名 等の装飾を剥がして素の名前候補を返す。
    例: 「わっふる教団司祭」チャコ → {'わっふる教団司祭', 'チャコ'}。
    /as の title が Discord 表示名に「title」付きで反映されるため、summaries/生ログ上の
    素の呼称（チャコ）と表示名（装飾付き）がズレてヒットしない問題に対応。"""
    out = set()
    if not name:
        return out
    stripped = re.sub(r"[「」『』【】《》〈〉\[\]()（）\"'`･・|]", " ", name)
    tokens   = [t for t in stripped.split() if len(t) >= 2]
    out.update(tokens)
    joined = "".join(tokens)
    if len(joined) >= 2:
        out.add(joined)
    return out


def build_member_needles(system_col, users_col) -> list[str]:
    """フォーカス対象を summaries/生ログで拾うための検索語を集める。
    FOCUS_NAME（装飾付き表示名）・素名・users.name/title・nickname_map の相互展開を統合。"""
    variants = {FOCUS_NAME}
    variants |= _strip_title_decoration(FOCUS_NAME)

    # users ドキュメントの記録名・二つ名も候補に
    try:
        udoc = users_col.find_one({"_id": FOCUS_TARGET}, {"name": 1, "title": 1}) or {}
        for key in ("name", "title"):
            if udoc.get(key):
                variants.add(udoc[key])
                variants |= _strip_title_decoration(udoc[key])
    except Exception as e:
        print(f"[WARN] users doc 読込失敗: {e}")

    # nickname_map: variants のいずれかに一致する alias/canon を相互展開（2パスで推移的に）
    try:
        ndoc = system_col.find_one({"_id": "nickname_map"}) or {}
        nmap = ndoc.get("map", {})
        for _ in range(2):
            for alias, canon in nmap.items():
                if alias in variants or canon in variants:
                    variants.add(alias)
                    variants.add(canon)
    except Exception as e:
        print(f"[WARN] nickname_map 読込失敗: {e}")

    # 1文字は誤爆するので2文字以上のみ採用
    return [v for v in variants if v and len(v) >= 2]


def fetch_relevant_summaries(summaries_col, needles: list[str]) -> str:
    """summaries アーカイブ（永久保持）から、needle を含む行だけを
    新しい順に MAX_SUMMARY_CHARS まで抜き出してテキスト化する。

    要約は全員分の内容を含むため、フォーカス対象を含む行のみに絞って
    ノイズとトークン消費を抑える。retro 要約も歴史的文脈として含める。
    """
    needles_l = [n.lower() for n in needles if n]
    if not needles_l:
        return ""

    # created_at は全書き手が .isoformat()（UTC）で文字列保存しているため、
    # ISO 文字列の辞書順比較で日付フィルタできる（analyze_* / enrich と同パターン）。
    since = datetime.now(timezone.utc) - timedelta(days=SUMMARY_LOOKBACK_DAYS)
    try:
        docs = list(summaries_col.find(
            {"summary": {"$exists": True}, "created_at": {"$gte": since.isoformat()}},
            {"summary": 1, "created_at": 1, "retro_date": 1},
            sort=[("created_at", -1)],
        ).limit(SUMMARY_MAX_DOCS))
    except Exception as e:
        print(f"[WARN] summaries 取得失敗: {e}")
        return ""

    blocks, total = [], 0
    for d in docs:
        summ = d.get("summary", "")
        if not summ:
            continue
        date = d.get("retro_date") or str(d.get("created_at", ""))[:10]
        hits = [ln.strip() for ln in summ.split("\n")
                if ln.strip() and any(n in ln.lower() for n in needles_l)]
        if not hits:
            continue
        block = f"[{date}]\n" + "\n".join(hits)
        if total + len(block) > MAX_SUMMARY_CHARS:
            break  # 新しい順なので、ここで打ち切れば直近を優先して保持
        blocks.append(block)
        total += len(block)

    print(f"[focus] 関連サマリ: {len(blocks)}ブロック ({total}文字)")
    return "\n\n".join(blocks)


# =============================================================================
# 既知情報の読み返し（Tier2・member専用）
# =============================================================================

# profile / simple_profile のフィールド表示名（両形に対応）
_PROFILE_LABELS = {
    "tone":                "口調",
    "communication_style": "コミュ特徴",
    "vocabulary":          "語彙・口癖",
    "personality":         "性格",
    "background":          "立場・背景",
    "relations":           "関係性",
    "interests_vibe":      "関心・雰囲気",
    "birthday":            "誕生日",
    "vibe":                "雰囲気",          # simple_profile
    "tone_tags":           "口調タグ",        # simple_profile（list）
}


def load_member_context(users_col) -> str:
    """対象メンバーについて過去に蓄積した profile/claims/memories を読み返す（積み上げ式）。

    embedding は巨大なのでプロンプトには載せない（content/category/date のみ）。
    profile は $set 上書きなので戻しても劣化しないが、claims/memories は append-store の
    ため、戻した内容が言い換えで再抽出されると near-dup が溜まる。dedup 側を正規化して防ぐ
    （save_memories_from_focus の _dedup_key を参照）。
    """
    doc = users_col.find_one({"_id": FOCUS_TARGET}) or {}
    if not doc:
        return ""

    prof  = doc.get("profile") or {}
    sprof = doc.get("simple_profile") or {}
    sections = []

    # プロフィール（booster profile 優先、無い項目は simple_profile で補う）
    prof_lines = []
    for k, label in _PROFILE_LABELS.items():
        v = prof.get(k) or sprof.get(k)
        if not v:
            continue
        if isinstance(v, list):
            v = "・".join(str(x) for x in v if x)
        if v:
            prof_lines.append(f"- {label}: {v}")
    for k, label in [("hobbies", "趣味"), ("memo", "メモ")]:
        v = prof.get(k)
        if v:
            joined = "・".join(str(x) for x in v if x)
            if joined:
                prof_lines.append(f"- {label}: {joined}")
    if prof_lines:
        sections.append("◆既存プロフィール\n" + "\n".join(prof_lines))

    # claims（append+cap20、末尾が新しい）→ 新しい順に最大15件
    claims = [c.get("content", "") for c in (doc.get("claims") or []) if c.get("content")]
    if claims:
        cl = "\n".join(f"- {c}" for c in reversed(claims[-15:]))
        sections.append("◆過去の主張(claims)\n" + cl)

    # memories（先頭が新しい MRU、embedding は載せない）→ 最大20件
    ml = []
    for m in (doc.get("memories") or [])[:20]:
        c = m.get("content", "")
        if not c:
            continue
        tag = "/".join(x for x in (m.get("category", ""), m.get("date", "")) if x)
        ml.append(f"- [{tag}] {c}" if tag else f"- {c}")
    if ml:
        sections.append("◆過去の記憶(memories)\n" + "\n".join(ml))

    return "\n\n".join(sections)


# =============================================================================
# AI処理
# =============================================================================

def _extract_retry_wait(err: str) -> float:
    m = re.search(r"retry in ([\d.]+)s", err)
    return float(m.group(1)) + 2.0 if m else 60.0


def call_ai(client_ai: genai.Client, prompt: str, max_tokens: int = 2000,
            temperature: float = 0.3) -> str | None:
    for model, label in [(MODEL, "main"), (MODEL_FALLBACK, "fallback")]:
        for attempt in range(5):
            try:
                resp = client_ai.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
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
                elif any(s in err for s in ("503", "UNAVAILABLE", "500", "INTERNAL")):
                    # 500 INTERNAL / 503 UNAVAILABLE は一時障害が多い → backoff してリトライ
                    wait = (attempt + 1) * 20
                    print(f"[WARN] 500/503 ({label}) attempt{attempt+1}, {wait}s待機...")
                    time.sleep(wait)
                else:
                    print(f"[ERROR] {label}: {e}")
                    break
    return None


def generate_report(client_ai: genai.Client, log_text: str, summary_text: str = "",
                    member_context: str = "") -> str | None:
    if FOCUS_TYPE == "member":
        prompt = MEMBER_FOCUS_PROMPT.format(name=FOCUS_NAME, days=FETCH_DAYS)
    else:
        prompt = KEYWORD_FOCUS_PROMPT.format(keyword=FOCUS_NAME, days=FETCH_DAYS)

    parts = []
    if member_context:
        parts.append(
            f"【これまでに蓄積した既知情報（過去の分析結果。今回の証拠で検証・更新せよ）】\n"
            f"{member_context[:MAX_MEMBER_CONTEXT_CHARS]}"
        )
    if summary_text:
        parts.append(
            f"【長期の要約アーカイブ（過去{SUMMARY_LOOKBACK_DAYS}日・{FOCUS_NAME}関連の行を抽出）】\n"
            f"{summary_text[:MAX_SUMMARY_CHARS]}"
        )
    if log_text:
        parts.append(
            f"【直近{FETCH_DAYS}日の生チャットログ（{FOCUS_NAME}関連のみ抽出）】\n"
            f"{log_text[:MAX_LOG_CHARS]}"
        )

    full_prompt = prompt + "\n\n" + "\n\n".join(parts)
    return call_ai(client_ai, full_prompt, max_tokens=3000)


def extract_profile(client_ai: genai.Client, report: str) -> dict | None:
    """人物フォーカスの場合のみプロフィールをJSON抽出"""
    prompt = PROFILE_UPDATE_PROMPT.format(name=FOCUS_NAME, report=report[:3000])
    raw    = call_ai(client_ai, prompt, max_tokens=400)
    if not raw:
        return None
    try:
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        result  = json.loads(cleaned)
        return result if any(v for v in result.values() if v) else None
    except Exception as e:
        print(f"[WARN] profile extract failed: {e}")
        return None


# =============================================================================
# MongoDB保存
# =============================================================================

def save_profile(users_col, profile: dict):
    """人物フォーカスの場合、対象メンバーのプロフィールに反映"""
    updates = {f"profile.{k}": v for k, v in profile.items() if v}
    if not updates:
        return
    users_col.update_one(
        {"_id": FOCUS_TARGET},
        {"$set": updates},
        upsert=False,
    )
    print(f"[profile] {FOCUS_NAME}のプロフィールを更新: {list(updates.keys())}")


def _dedup_key(s: str) -> str:
    """near-dup 照合用の正規化キー。Tier2 で既知情報を戻すと言い換え再抽出が起きるため、
    大小文字・空白・末尾句読点を畳んで軽い言い換え重複を弾く（完全な意味dedupはTier3領域）。"""
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", "", s)
    return s.strip("。.!?！？、,，・")


def save_memories_from_focus(client_ai: genai.Client, users_col, report: str):
    """focusレポートからmemories・claimsを抽出してユーザーに保存"""
    prompt = f"""以下は「{FOCUS_NAME}」についての観察レポートです。
このレポートから、後で思い出す価値のある情報を抽出してください。
必ずJSON形式のみで返せ。前置き・コードブロック不要。

出力形式:
{{
  "claims": ["この人が主張・意見として述べたことを一文ずつ（最大5件）"],
  "memories": [
    {{"content": "記憶すべき出来事・状況・感情を一文で", "category": "趣味/出来事/感情/計画"}}
  ]
}}

【観察レポート】
{report[:3000]}"""

    # call_ai 経由で MODEL→MODEL_FALLBACK のリトライ/フォールバックに乗せる
    # （旧実装は models/gemma-3-27b-it 直書きで 404 NOT_FOUND になり常に失敗していた）
    raw = call_ai(client_ai, prompt, max_tokens=500, temperature=0.1)
    if not raw:
        print("[WARN] focus memories extract: 空/失敗（スキップ）")
        return
    try:
        raw  = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
    except Exception as e:
        print(f"[WARN] focus memories extract parse: {e}")
        return

    doc = users_col.find_one({"_id": FOCUS_TARGET}) or {}
    now = datetime.now(timezone.utc)

    # claims更新（正規化キーで near-dup も弾く・intra-batch も seen で防ぐ）
    new_claims = []
    seen_claims = {_dedup_key(c.get("content","")) for c in doc.get("claims", [])}
    for c in data.get("claims", []):
        key = _dedup_key(c)
        if c and key and key not in seen_claims:
            seen_claims.add(key)
            new_claims.append({
                "content": c[:200],
                "date":    now.isoformat(),
                "source":  "focus",
            })
    all_claims = (doc.get("claims", []) + new_claims)[-20:]

    # memories更新（Embedding付与・正規化キーで near-dup を弾く）
    new_memories = []
    seen_mems = {_dedup_key(m.get("content","")) for m in doc.get("memories", [])}
    for m in data.get("memories", []):
        text = m.get("content","")
        key  = _dedup_key(text)
        if not text or not key or key in seen_mems:
            continue
        seen_mems.add(key)
        vec = []
        try:
            er = client_ai.models.embed_content(
                model="models/gemini-embedding-001",
                contents=text[:500],
            )
            if hasattr(er, "embeddings") and er.embeddings:
                vec = list(er.embeddings[0].values)
        except Exception:
            pass
        new_memories.append({
            "content":   text[:300],
            "date":      now.strftime("%Y-%m"),
            "category":  m.get("category",""),
            "embedding": vec,
            "source":    "focus",
        })
        time.sleep(0.5)

    # 新しい記憶を先頭に置き、新しい順に40件保持（main.py $position:0 と向きを統一）
    all_memories = (new_memories + doc.get("memories", []))[:40]

    if new_claims or new_memories:
        users_col.update_one(
            {"_id": FOCUS_TARGET},
            {"$set": {"claims": all_claims, "memories": all_memories}},
            upsert=False,
        )
        print(f"[focus] {FOCUS_NAME}: claims+{len(new_claims)}件, memories+{len(new_memories)}件")


# =============================================================================
# Discord投稿
# =============================================================================

def post_report(report: str):
    now_jst    = datetime.now(JST)
    icon       = "👤" if FOCUS_TYPE == "member" else "🔍"
    title_str  = f"{icon} {FOCUS_NAME} のフォーカス要約"
    ICONS      = SECTION_ICONS_MEMBER if FOCUS_TYPE == "member" else SECTION_ICONS_KEYWORD

    parts    = re.split(r"\n##\s+", "\n" + report)
    sections = []
    for part in parts:
        if not part.strip():
            continue
        split = part.strip().split("\n", 1)
        title = split[0].strip()
        body  = split[1].strip() if len(split) > 1 else ""
        if title and body:
            sections.append((title, body))

    fields = []
    for title, body in sections:
        icon_c = next((v for k, v in ICONS.items() if k in title), "📋")
        if len(body) > 1020:
            body = body[:1017] + "…"
        fields.append({"name": f"{icon_c} {title}", "value": body, "inline": False})

    embeds, current_fields, current_chars = [], [], 0
    for field in fields:
        fc = len(field["name"]) + len(field["value"])
        if (current_chars + fc > 5800 or len(current_fields) >= 25) and current_fields:
            embed = {"color": 0x9B59B6, "fields": current_fields,
                     "title": title_str if not embeds else f"{icon} {FOCUS_NAME} のフォーカス要約（続き）"}
            embeds.append(embed)
            current_fields, current_chars = [], 0
        current_fields.append(field)
        current_chars += fc

    if current_fields:
        embeds.append({
            "color":  0x9B59B6,
            "title":  title_str if not embeds else f"{icon} {FOCUS_NAME} のフォーカス要約（続き）",
            "fields": current_fields,
            "footer": {"text": f"空気くんフォーカス要約 • {now_jst.strftime('%H:%M')} JST"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    url     = f"https://discord.com/api/v10/channels/{SUMMARY_CHANNEL_ID}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
    for i in range(0, len(embeds), 10):
        resp = requests.post(url, headers=headers,
                             json={"embeds": embeds[i:i+10]}, timeout=10)
        if resp.status_code in (200, 201):
            print(f"[post] 投稿成功")
        else:
            print(f"[ERROR] 投稿失敗: {resp.status_code} {resp.text}")


# =============================================================================
# メイン
# =============================================================================

def main():
    print(f"[focus] type={FOCUS_TYPE}, target={FOCUS_TARGET}, name={FOCUS_NAME}")

    # MongoDB は両モードで使う（要約アーカイブ・nickname_map）ので先に接続
    mongo         = MongoClient(MONGODB_URI)
    db            = mongo[DB_NAME]
    users_col     = db["users"]
    summaries_col = db["summaries"]
    system_col    = db["system"]

    # フォーカス対象の検索語（member はエイリアスまで展開）
    if FOCUS_TYPE == "member":
        needles = build_member_needles(system_col, users_col)
        print(f"[focus] 名前バリアント: {needles}")
    else:
        needles = list({FOCUS_TARGET, FOCUS_NAME})

    # 生ログ取得（直近 FETCH_DAYS 日）
    print(f"[focus] 直近{FETCH_DAYS}日分の生ログを取得中...")
    msgs     = fetch_logs(FETCH_DAYS)
    log_text = filter_logs(msgs, needles) if msgs else ""
    print(f"[focus] 対象生ログ: {len(log_text)}文字")

    # 長期アーカイブ（summaries）から関連行を抽出
    print(f"[focus] 過去{SUMMARY_LOOKBACK_DAYS}日の要約アーカイブを検索中...")
    summary_text = fetch_relevant_summaries(summaries_col, needles)

    if not log_text and not summary_text:
        print(f"[focus] 「{FOCUS_NAME}」に関する情報が見つかりませんでした。")
        return

    # 既知情報の読み返し（Tier2・member専用：過去のprofile/claims/memoriesを積み上げ）
    member_context = load_member_context(users_col) if FOCUS_TYPE == "member" else ""
    if member_context:
        print(f"[focus] 既知情報を読み込み: {len(member_context)}文字")

    # レポート生成（既知情報 + 長期要約 + 直近生ログ）
    print("[focus] レポート生成中...")
    client_ai = genai.Client(api_key=GEMINI_API_KEY)
    report    = generate_report(client_ai, log_text, summary_text, member_context)
    if not report:
        print("[focus] レポート生成失敗")
        return
    print(f"[focus] レポート生成完了 ({len(report)}文字)")

    # 人物フォーカスの場合はプロフィール・memories補完
    if FOCUS_TYPE == "member":
        print("[focus] プロフィール・記憶抽出中...")
        time.sleep(3)
        profile = extract_profile(client_ai, report)
        if profile:
            save_profile(users_col, profile)
        else:
            print("[focus] プロフィール抽出失敗（スキップ）")
        time.sleep(3)
        save_memories_from_focus(client_ai, users_col, report)

    # Discord投稿
    post_report(report)
    print("[focus] 完了！")


if __name__ == "__main__":
    main()
