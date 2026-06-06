"""
analyze_personality.py

ブースターユーザーの性格分析をハイブリッド方式で実行。

【分析方式】
  ① 口調・語彙・テンション → butler_history（生の発言）から抽出
  ② 性格・背景・人間関係  → summaries（要約）から抽出
  ③ 両者を統合して保存

【保存フィールド】
  profile.tone              : 口調・語彙の特徴
  profile.personality       : 性格の概要
  profile.background        : 役職・サーバー内の立場
  profile.relations         : よく絡むメンバー・関係性
  profile.communication_style: コミュニケーションの特徴
  profile.interests_vibe    : 関心・雰囲気
"""

import os
import json
import re
import time
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient
from google import genai
from google.genai import types

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MONGODB_URI    = os.environ["MONGODB_URI"]
MODEL          = "models/gemma-4-31b-it"
MODEL_FALLBACK = "models/gemma-4-26b-a4b-it"
DB_NAME        = "discord_bot_db"
MIN_CONV_COUNT = 10
SUMMARY_DAYS   = 7  # 直近何日分の要約を参照するか

# =============================================================================
# プロンプト
# =============================================================================

# ① 生の発言から「口調・語彙・テンション」のみ抽出
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

# ② 要約から「性格・背景・人間関係」のみ抽出
CONTEXT_PROMPT = """\
以下はDiscordサーバーの要約ログです（直近{days}日分）。
この中に「{name}」というメンバーの情報が含まれている場合、
「性格・サーバー内の立場・他メンバーとの関係性」を分析してください。
言及が少ない場合は全項目nullにしてください。
必ずJSON形式のみで返してください。前置き・説明文・```は不要です。

出力形式:
{{
  "personality": "性格の概要（例: 面倒見がよくムードメーカー・物静かだが鋭い観察眼がある）",
  "background": "サーバー内の立場・役職（例: シニアマネージャー・サーバー創設メンバーの一人）",
  "relations": "よく絡むメンバーや関係性（例: 花子と特に親密・新規メンバーの案内役を担う）",
  "interests_vibe": "関心・雰囲気（例: ゲーム全般・深夜帯によく出没・盛り上げ役）"
}}

【要約ログ】
{summaries}
"""

# ③ 既存プロフィールとの統合
MERGE_PROMPT = """\
以下はDiscordユーザー「{name}」の既存プロフィールと新しい分析結果です。
両者を統合して最終的なプロフィールを作成してください。
新しい情報を優先しつつ、既存情報の良い部分も残してください。
必ずJSON形式のみで返してください。前置き・説明文・```は不要です。

【既存プロフィール】
{existing}

【新しい分析（口調・語彙）】
{new_tone}

【新しい分析（性格・背景）】
{new_context}

出力形式:
{{
  "tone": "口調の特徴",
  "communication_style": "話し方の特徴",
  "vocabulary": "よく使う語彙・口癖",
  "personality": "性格の概要",
  "background": "サーバー内の立場・役職",
  "relations": "よく絡むメンバーや関係性",
  "interests_vibe": "関心・雰囲気"
}}
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
        # 全部nullなら None扱い
        if not any(v for v in result.values() if v):
            return None
        return result
    except json.JSONDecodeError:
        print(f"[WARN] JSON parse failed for {name}: {raw[:80]}")
        return None


# =============================================================================
# 分析ロジック
# =============================================================================

def build_utterances(history: list) -> str:
    """butler_historyからユーザー発言のみ抽出"""
    lines = [h["content"] for h in history if h.get("role") == "user"]
    # 直近30発言・各発言は100文字に制限
    lines = lines[-150:]
    # 文字数制限(l[:100])を撤廃し、生のニュアンスを維持
    return "\n".join(f"- {l}" for l in lines)


def fetch_summaries(summaries_col, days: int, limit: int = 30) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    docs = list(
        summaries_col.find({"created_at": {"$gte": since.isoformat()}})
                     .sort("created_at", -1)
                     .limit(limit)
    )
    if not docs:
        return ""
    docs.reverse()  # 時系列順に戻す
    parts = [f"[{d.get('created_at', '')[:10]}]\n{d.get('summary', '')}" for d in docs if d.get("summary")]
    return "\n\n---\n\n".join(parts)


def analyze_tone(client: genai.Client, name: str, history: list) -> dict | None:
    """① 生の発言から口調・語彙を分析"""
    utterances = build_utterances(history)
    if not utterances:
        return None
    prompt = TONE_PROMPT.format(name=name, utterances=utterances)
    raw    = call_gemma(client, prompt, max_tokens=300)
    return parse_json(raw, name)


def analyze_context(client: genai.Client, name: str, summaries_text: str) -> dict | None:
    if not summaries_text: return None
    # 制限( [:5000] )を削除
    prompt = CONTEXT_PROMPT.format(
        name=name,
        days=SUMMARY_DAYS,
        summaries=summaries_text, 
    )
    raw = call_gemma(client, prompt, max_tokens=1000) # 出力を少しリッチに
    return parse_json(raw, name)


def merge_profiles(client: genai.Client, name: str,
                   existing: dict, tone: dict | None, context: dict | None) -> dict | None:
    """③ 既存＋新規を統合"""
    # 既存がなく片方しかない場合はそのまま返す
    has_existing = any(existing.get(k) for k in ("tone", "personality", "communication_style"))
    if not has_existing:
        merged = {}
        if tone:
            merged.update(tone)
        if context:
            merged.update(context)
        return merged if merged else None

    prompt = MERGE_PROMPT.format(
        name=name,
        existing=json.dumps(existing, ensure_ascii=False),
        new_tone=json.dumps(tone or {}, ensure_ascii=False),
        new_context=json.dumps(context or {}, ensure_ascii=False),
    )
    raw = call_gemma(client, prompt, max_tokens=500)
    return parse_json(raw, name) or (tone or context)


# =============================================================================
# メイン
# =============================================================================

def main():
    print("[personality] Connecting to MongoDB...")
    mongo        = MongoClient(MONGODB_URI)
    users_col    = mongo[DB_NAME]["users"]
    summaries_col = mongo[DB_NAME]["summaries"]

    targets = list(users_col.find({"conv_count": {"$gte": MIN_CONV_COUNT}}))
    print(f"[personality] Target users: {len(targets)}")
    if not targets:
        print("[personality] No users to analyze. Done.")
        return

    # 要約は全ユーザー共通で使い回す（API節約）
    print("[personality] Fetching summaries...")
    summaries_text = fetch_summaries(summaries_col, SUMMARY_DAYS)
    print(f"[personality] Summaries: {len(summaries_text)}文字")

    client  = genai.Client(api_key=GEMINI_API_KEY)
    success = 0

    for doc in targets:
        uid     = str(doc["_id"])
        name    = doc.get("name", uid)
        history = doc.get("butler_history", [])
        print(f"[personality] Analyzing {name} (conv_count={doc.get('conv_count', 0)})...")

        # ① 口調分析（生の発言から）
        tone = analyze_tone(client, name, history)
        print(f"  tone: {tone}")
        time.sleep(3)

        # ② 性格・背景分析（要約から）
        context = analyze_context(client, name, summaries_text)
        print(f"  context: {context}")
        time.sleep(3)

        if not tone and not context:
            print(f"[personality] Skipped {name}: データ不足")
            continue

        # ③ 既存プロフィールと統合
        existing = doc.get("profile", {})
        merged   = merge_profiles(client, name, existing, tone, context)
        if not merged:
            print(f"[personality] Skipped {name}: マージ失敗")
            continue

        users_col.update_one(
            {"_id": doc["_id"]},
            {
                "$set":   {"profile": merged},
                "$unset": {"conv_count": ""},
            }
        )
        print(f"[personality] Saved {name}: {merged}")
        success += 1
        time.sleep(5)

    print(f"[personality] Completed. {success}/{len(targets)} users analyzed.")


if __name__ == "__main__":
    main()
