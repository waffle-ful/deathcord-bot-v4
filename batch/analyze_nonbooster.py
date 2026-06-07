"""
analyze_nonbooster.py

非ブースターユーザーの性格分析を要約ベースのハイブリッド方式で実行。

【分析内容】
  要約から以下を抽出してsimple_profileに保存:
    tone_tags:        口調タグ
    vibe:             雰囲気の一言
    frequent_members: よく絡むメンバー
    personality:      性格の概要（NEW）
    background:       サーバー内の立場（NEW）
    relations:        関係性の詳細（NEW）
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
SUMMARY_DAYS   = 10  # 直近5日分（精度向上のため3→5に拡大）


ANALYZE_PROMPT = """\
あなたは熟練のコミュニティアナリストです。
提供されたDiscordサーバーの要約ログ（直近{days}日分）を元に、メンバー「{name}」の人物像を深く分析してください。

言及が少ない・見つからない場合は全項目nullにしてください。
推測・捏造は絶対にしないでください。ログに根拠がある情報のみ記載してください。
必ずJSON形式のみで返してください。前置き・説明文・\`\`\`は不要です。

出力形式:
{{
  "tone_tags": ["口調タグ（例: テンション高め / 短文多め / ツッコミ役 / ボケ役 / 煽り気味 / 丁寧語）"],
  "vibe": "その人の雰囲気を一言で（例: ムードメーカー的存在・物静かだが鋭い・いじられキャラ）",
  "frequent_members": ["よく絡むメンバー名（最大3人）"],
  "personality": "性格の概要（例: 面倒見が良くサーバーをよく盛り上げる・クールだが仲間思い）",
  "background": "サーバー内の立場・役割（例: 古参メンバー・よく質問に答える人・盛り上げ役）",
  "relations": "人間関係の特徴（例: ○○と特によく絡む・新規メンバーへの案内役）"
}}

【要約ログ】
{summaries}
"""


def _extract_retry_wait(err: str) -> float:
    m = re.search(r"retry in ([\d.]+)s", err)
    return float(m.group(1)) + 2.0 if m else 60.0


def call_ai(client: genai.Client, prompt: str) -> str | None:
    for model, label in [(MODEL, "main"), (MODEL_FALLBACK, "fallback")]:
        for attempt in range(5):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.25,
                        max_output_tokens=1000,
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
                    wait = (attempt + 1) * 15
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
        if not any(result.get(k) for k in ("tone_tags", "vibe", "personality")):
            return None
        return result
    except json.JSONDecodeError:
        print(f"[WARN] JSON parse failed for {name}: {raw[:80]}")
        return None


def fetch_recent_summaries(col, days: int) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    docs  = list(
        col.find({"created_at": {"$gte": since.isoformat()}})
           .sort("created_at", -1)
           .limit(days * 12)
    )
    parts = [d.get("summary", "") for d in docs if d.get("summary")]
    return "\n\n---\n\n".join(parts)


# =============================================================================
# 出現チェック（D: 要約に一度も登場しない人はAPIを呼ばずスキップ）
#   情報量は一切減らさない。分析する人には従来どおり全要約を渡す。
#   減らすのは「そもそも話題に出ていない人への無駄なAPIコール」だけ。
# =============================================================================

def core_name(name: str) -> str:
    """表示名から「対応可能」等のブラケット装飾タグを内側から順に除去して芯の名前を得る。"""
    c = name or ""
    for _ in range(3):  # ネストした 「…「…」…」 を内側から段階的に除去
        new = re.sub(r"[「『【][^「『【」』】]*[」』】]", "", c)
        if new == c:
            break
        c = new
    return c.strip(" \t\"'`・,")


def build_search_terms(name: str, nick_map: dict) -> set[str]:
    """その人物を要約内で指している可能性のある文字列集合（本名・芯・ニックネーム別名）。

    判断に迷うときは「残す（=スキップしない）」方向に倒す設計。誤スキップで
    本来データのある人を取りこぼすより、無駄打ちが1回残るほうが安全。
    """
    core  = core_name(name)
    terms = {name, core}
    for alias, canon in (nick_map or {}).items():
        if not canon or not core:
            continue
        if core == canon or core in canon or canon in core:
            terms.add(alias)   # 要約が別名で言及しているケースを拾う
            terms.add(canon)
    return {t for t in terms if t and len(t) >= 2}


def is_mentioned(name: str, nick_map: dict, summaries_text: str) -> bool:
    return any(t in summaries_text for t in build_search_terms(name, nick_map))


def merge_simple_profile(existing: dict, new: dict) -> dict:
    merged = existing.copy()
    if new.get("tone_tags"):
        merged["tone_tags"] = list(dict.fromkeys(
            (existing.get("tone_tags") or []) + new["tone_tags"]
        ))[:8]
    for key in ("vibe", "frequent_members", "personality", "background", "relations"):
        if new.get(key):
            merged[key] = new[key]
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    return merged


def main():
    print("[nonbooster] Connecting to MongoDB...")
    mongo         = MongoClient(MONGODB_URI)
    users_col     = mongo[DB_NAME]["users"]
    summaries_col = mongo[DB_NAME]["summaries"]

    summaries_text = fetch_recent_summaries(summaries_col, SUMMARY_DAYS)
    if not summaries_text:
        print("[nonbooster] No summaries found. Skipping.")
        return
    print(f"[nonbooster] Summaries: {len(summaries_text)}文字")

    targets = list(users_col.find({
        "is_booster":         {"$ne": True},
        "name":               {"$exists": True},
        "xp":                 {"$gt": 0},
        "personality_optout": {"$ne": True},   # /privacy でオプトアウトした人は分析しない
    }))
    print(f"[nonbooster] Target users: {len(targets)}")
    if not targets:
        print("[nonbooster] No users. Done.")
        return

    nick_map = (mongo[DB_NAME]["system"].find_one({"_id": "nickname_map"}) or {}).get("map", {})
    print(f"[nonbooster] nickname_map: {len(nick_map)} aliases")

    client  = genai.Client(api_key=GEMINI_API_KEY)
    success = 0
    skipped = 0

    for doc in targets:
        name = doc.get("name", str(doc["_id"]))
        uid  = doc["_id"]

        # D: 要約に一度も登場しない人はAPIを呼ばずスキップ（無駄打ち削減・タイムアウト回避）
        if not is_mentioned(name, nick_map, summaries_text):
            skipped += 1
            continue

        print(f"[nonbooster] Analyzing {name}...")

        prompt = ANALYZE_PROMPT.format(
            name=name,
            days=SUMMARY_DAYS,
            summaries=summaries_text,
        )
        raw    = call_ai(client, prompt)
        result = parse_json(raw, name) if raw else None

        if not result:
            print(f"[nonbooster] No data for {name}, skipping.")
            time.sleep(1)
            continue

        existing = doc.get("simple_profile", {})
        merged   = merge_simple_profile(existing, result)

        users_col.update_one(
            {"_id": uid},
            {"$set": {"simple_profile": merged}},
            upsert=False,
        )
        print(f"[nonbooster] Saved {name}: personality={str(result.get('personality', '-'))[:40]}")
        success += 1
        time.sleep(3)

    print(f"[nonbooster] Completed. {success}/{len(targets)} users analyzed. "
          f"{skipped} skipped (要約に未登場・API未呼出).")


if __name__ == "__main__":
    main()
