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
MONGODB_URI    = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
MODEL          = "models/gemma-4-31b-it"
MODEL_FALLBACK = "models/gemma-4-26b-a4b-it"

# 内部バッチなので safety ブロックで空レスポンスにならないよう緩和（analyze_personality/focus と同方針）。
try:
    SAFETY_OFF = [
        types.SafetySetting(category=c, threshold="BLOCK_NONE")
        for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                  "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
    ]
except Exception as _e:
    print(f"[WARN] safety_settings 構築失敗（デフォルト使用）: {_e}")
    SAFETY_OFF = None
DB_NAME        = "discord_bot_db"
SUMMARY_DAYS   = 10  # 直近5日分（精度向上のため3→5に拡大）
# 1回の最大処理人数。安全策修正でLLM呼び出しが成功＝低速化したため、無制限だと
# daily_tasks の60分ジョブtimeoutを食い潰す。未分析優先のローテーションで複数日に分散。
MAX_NB_PER_RUN = int(os.environ.get("NB_MAX_PER_RUN", "20"))


ANALYZE_PROMPT = """\
あなたは熟練のコミュニティアナリストです。
提供されたDiscordサーバーの要約ログ（直近{days}日分）を元に、メンバー「{name}」の人物像を深く分析してください。

言及が少ない・見つからない場合は全項目nullにしてください。
推測・捏造は絶対にしないでください。ログに根拠がある情報のみ記載してください。
必ずJSON形式のみで返してください。前置き・説明文・```は不要です。

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
                        max_output_tokens=2000,
                        safety_settings=SAFETY_OFF,
                    ),
                )
                text = getattr(resp, "text", None)
                if text and text.strip():
                    return text.strip()
                try:
                    cand = (getattr(resp, "candidates", None) or [None])[0]
                    fr   = getattr(cand, "finish_reason", None)
                    print(f"[WARN] {label}: 空レスポンス attempt{attempt+1} finish={fr}")
                except Exception:
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

    # D: 要約に登場する人だけ対象（無駄打ち削減）。さらに未分析(simple_profile.analyzed_at
    # 無し)→古い順に並べ、MAX_NB_PER_RUN で打ち切る＝1回の実行時間を有界にしつつ複数日で全員巡回。
    mentioned = [d for d in targets
                 if is_mentioned(d.get("name", str(d["_id"])), nick_map, summaries_text)]
    skipped   = len(targets) - len(mentioned)
    mentioned.sort(key=lambda d: (d.get("simple_profile") or {}).get("analyzed_at") or "")
    batch     = mentioned[:MAX_NB_PER_RUN]
    print(f"[nonbooster] 言及あり={len(mentioned)}人 / 今回処理={len(batch)}人 (cap {MAX_NB_PER_RUN}/run)")

    client  = genai.Client(api_key=GEMINI_API_KEY)
    now     = datetime.now(timezone.utc)
    success = 0

    for doc in batch:
        name = doc.get("name", str(doc["_id"]))
        uid  = doc["_id"]

        print(f"[nonbooster] Analyzing {name}...")

        prompt = ANALYZE_PROMPT.format(
            name=name,
            days=SUMMARY_DAYS,
            summaries=summaries_text,
        )
        raw    = call_ai(client, prompt)
        result = parse_json(raw, name) if raw else None

        if not result:
            # 言及ありなのにデータ無し → analyzed_at だけ刻んでローテーションを前進させる
            # （毎回 cap 枠を食い潰さないため。次の巡回で再挑戦される）
            print(f"[nonbooster] No data for {name}, skipping.")
            users_col.update_one({"_id": uid},
                                 {"$set": {"simple_profile.analyzed_at": now.isoformat()}})
            time.sleep(1)
            continue

        existing = doc.get("simple_profile", {})
        merged   = merge_simple_profile(existing, result)
        merged["analyzed_at"] = now.isoformat()

        users_col.update_one(
            {"_id": uid},
            {"$set": {"simple_profile": merged}},
            upsert=False,
        )
        print(f"[nonbooster] Saved {name}: personality={str(result.get('personality', '-'))[:40]}")
        success += 1
        time.sleep(3)

    print(f"[nonbooster] Completed. {success}/{len(batch)} 処理 "
          f"(言及あり{len(mentioned)}人中・cap {MAX_NB_PER_RUN})。{skipped} skipped (要約に未登場).")


if __name__ == "__main__":
    main()
