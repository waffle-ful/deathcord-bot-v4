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
MONGODB_URI        = os.environ["MONGODB_URI"]
SUMMARY_CHANNEL_ID = os.environ["SUMMARY_CHANNEL_ID"]

MODEL          = "models/gemma-4-31b-it"
MODEL_FALLBACK = "models/gemma-4-26b-a4b-it"
DB_NAME        = "discord_bot_db"
JST            = timezone(timedelta(hours=9))
FETCH_DAYS     = 7   # 直近何日分のログを取得するか

# =============================================================================
# プロンプト
# =============================================================================

MEMBER_FOCUS_PROMPT = """\
以下はDiscordサーバーの直近{days}日分のチャットログです。
「{name}」というメンバーの発言・言及に絞って分析し、
この人物についての詳細な観察レポートを書いてください。
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
以下はDiscordサーバーの直近{days}日分のチャットログです。
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


def filter_logs(msgs: list[dict]) -> str:
    """フォーカス対象に絞ってログをテキスト化"""
    filtered = []

    if FOCUS_TYPE == "member":
        target_name = FOCUS_NAME.lower()
        target_id   = FOCUS_TARGET
        for m in msgs:
            # 発言者が対象 or メッセージ内に名前/IDが含まれる
            is_author  = m["author_id"] == target_id or target_name in m["author"].lower()
            is_mention = target_name in m["content"].lower() or f"<@{target_id}>" in m["content"]
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
# AI処理
# =============================================================================

def _extract_retry_wait(err: str) -> float:
    m = re.search(r"retry in ([\d.]+)s", err)
    return float(m.group(1)) + 2.0 if m else 60.0


def call_ai(client_ai: genai.Client, prompt: str, max_tokens: int = 2000) -> str | None:
    for model, label in [(MODEL, "main"), (MODEL_FALLBACK, "fallback")]:
        for attempt in range(5):
            try:
                resp = client_ai.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.3,
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
                elif "503" in err or "UNAVAILABLE" in err:
                    wait = (attempt + 1) * 20
                    print(f"[WARN] 503 ({label}) attempt{attempt+1}, {wait}s待機...")
                    time.sleep(wait)
                else:
                    print(f"[ERROR] {label}: {e}")
                    break
    return None


def generate_report(client_ai: genai.Client, log_text: str) -> str | None:
    if FOCUS_TYPE == "member":
        prompt = MEMBER_FOCUS_PROMPT.format(name=FOCUS_NAME, days=FETCH_DAYS)
    else:
        prompt = KEYWORD_FOCUS_PROMPT.format(keyword=FOCUS_NAME, days=FETCH_DAYS)

    full_prompt = prompt + f"\n\n【チャットログ（{FOCUS_NAME}関連のみ抽出）】\n{log_text[:10000]}"
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

    try:
        resp = client_ai.models.generate_content(
            model="models/gemma-3-27b-it",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=500),
        )
        raw  = getattr(resp, "text", "").strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
    except Exception as e:
        print(f"[WARN] focus memories extract: {e}")
        return

    doc = users_col.find_one({"_id": FOCUS_TARGET}) or {}
    now = datetime.now(timezone.utc)

    # claims更新
    new_claims = []
    existing_claims = {c.get("content","") for c in doc.get("claims", [])}
    for c in data.get("claims", []):
        if c and c not in existing_claims:
            new_claims.append({
                "content": c[:200],
                "date":    now.isoformat(),
                "source":  "focus",
            })
    all_claims = (doc.get("claims", []) + new_claims)[-20:]

    # memories更新（Embedding付与）
    new_memories = []
    existing_mems = {m.get("content","") for m in doc.get("memories", [])}
    for m in data.get("memories", []):
        text = m.get("content","")
        if not text or text in existing_mems:
            continue
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

    # ログ取得
    print(f"[focus] 直近{FETCH_DAYS}日分のログを取得中...")
    msgs = fetch_logs(FETCH_DAYS)
    if not msgs:
        print("[focus] メッセージが見つかりません。")
        return

    # フォーカス対象に絞る
    log_text = filter_logs(msgs)
    if not log_text:
        print(f"[focus] 「{FOCUS_NAME}」に関する発言が見つかりませんでした。")
        return
    print(f"[focus] 対象ログ: {len(log_text)}文字")

    # レポート生成
    print("[focus] レポート生成中...")
    client_ai = genai.Client(api_key=GEMINI_API_KEY)
    report    = generate_report(client_ai, log_text)
    if not report:
        print("[focus] レポート生成失敗")
        return
    print(f"[focus] レポート生成完了 ({len(report)}文字)")

    # 人物フォーカスの場合はプロフィール・memories補完
    if FOCUS_TYPE == "member":
        print("[focus] プロフィール・記憶抽出中...")
        time.sleep(3)
        mongo     = MongoClient(MONGODB_URI)
        users_col = mongo[DB_NAME]["users"]
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
