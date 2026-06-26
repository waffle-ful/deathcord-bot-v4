"""
enrich_memories.py

retroreportとfocus結果からclaims・memoriesを補完するバッチ。
daily_tasksの最後に実行する。

処理:
  ① summariesコレクションから直近7日分の要約を取得
  ② 各ブースターユーザーについて
     要約から主張・記憶に値する情報を抽出
  ③ claims・memoriesに追記（重複は除く）
  ④ Embeddingベクトルを付与して保存
"""

import os
import json
import re
import time
import asyncio
from datetime import datetime, timezone, timedelta

from pymongo import MongoClient
from google import genai
from google.genai import types

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MONGODB_URI    = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
MODEL          = "models/gemma-4-31b-it"
MODEL_FB       = "models/gemma-4-26b-a4b-it"

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
EMBED_MODEL    = "models/gemini-embedding-001"
DB_NAME        = "discord_bot_db"
SUMMARY_DAYS   = 7
MAX_MEMORIES   = 40
MAX_CLAIMS     = 20


EXTRACT_PROMPT = """\
以下はDiscordサーバーの要約ログです。
「{name}」というメンバーについての情報を抽出してください。

【抽出ルール】
- 根拠がある情報のみ記載。推測・捏造禁止
- 情報が見当たらない項目は空リストにせよ
- 単に事実を書き出すだけでなく、その人の価値観や、将来の行動を予測する手がかりになるような『深い情報』を優先して抽出せよ。

必ずJSON形式のみで返せ。前置き・説明文・コードブロック不要。

出力形式:
{{
  "claims": [
    "この人が主張・意見として述べたこと（「〜だと思う」「〜がいい」等）を一文ずつ"
  ],
  "memories": [
    {{
      "content": "後で思い出す価値のある出来事・状況・感情を一文で",
      "category": "趣味/出来事/感情/計画のいずれか"
    }}
  ]
}}

【要約ログ（直近{days}日分）】
{summaries}
"""


def _extract_retry_wait(err: str) -> float:
    m = re.search(r"retry in ([\d.]+)s", err)
    return float(m.group(1)) + 2.0 if m else 60.0


def call_ai(client: genai.Client, prompt: str) -> str | None:
    for model, label in [(MODEL, "main"), (MODEL_FB, "fallback")]:
        for attempt in range(4):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        max_output_tokens=1500,
                        safety_settings=SAFETY_OFF,
                    ),
                )
                text = getattr(resp, "text", "").strip()
                if text:
                    return text
                try:
                    cand = (getattr(resp, "candidates", None) or [None])[0]
                    print(f"[WARN] {label}: 空レスポンス attempt{attempt+1} finish={getattr(cand, 'finish_reason', None)}")
                except Exception:
                    print(f"[WARN] {label}: 空レスポンス attempt{attempt+1}")
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    wait = _extract_retry_wait(err)
                    print(f"[WARN] 429 ({label}) attempt{attempt+1}, {wait:.1f}s待機...")
                    time.sleep(wait)
                elif "503" in err or "UNAVAILABLE" in err:
                    wait = (attempt + 1) * 15
                    time.sleep(wait)
                else:
                    print(f"[ERROR] {label}: {e}")
                    break
    return None


def get_embedding(client: genai.Client, text: str) -> list[float]:
    try:
        resp = client.models.embed_content(
            model=EMBED_MODEL,
            contents=text[:500],
        )
        if hasattr(resp, "embeddings") and resp.embeddings:
            return list(resp.embeddings[0].values)
        if hasattr(resp, "embedding"):
            return list(resp.embedding.values)
    except Exception as e:
        print(f"[WARN] embedding: {e}")
    return []


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


def dedup(existing: list[dict], new_items: list, key: str = "content") -> list:
    """既存リストと重複しないものだけ返す"""
    existing_texts = {e.get(key, "") for e in existing}
    result = []
    for item in new_items:
        text = item if isinstance(item, str) else item.get(key, "")
        if text and text not in existing_texts:
            result.append(item)
            existing_texts.add(text)
    return result


def main():
    print("[enrich] MongoDB接続中...")
    mongo         = MongoClient(MONGODB_URI)
    db            = mongo[DB_NAME]
    users_col     = db["users"]
    summaries_col = db["summaries"]

    summaries_text = fetch_summaries(summaries_col, SUMMARY_DAYS)
    if not summaries_text:
        print("[enrich] 要約なし・スキップ")
        return
    print(f"[enrich] 要約: {len(summaries_text)}文字")

    # ブースターユーザーのみ対象
    targets = list(users_col.find({"is_booster": True, "name": {"$exists": True}}))
    print(f"[enrich] 対象ユーザー: {len(targets)}人")

    client = genai.Client(api_key=GEMINI_API_KEY)
    success = 0

    for doc in targets:
        uid  = str(doc["_id"])
        name = doc.get("name", uid)
        print(f"[enrich] 処理中: {name}")

        prompt = EXTRACT_PROMPT.format(
            name=name,
            days=SUMMARY_DAYS,
            summaries=summaries_text,
        )
        raw = call_ai(client, prompt)
        if not raw:
            print(f"[enrich] {name}: AI失敗・スキップ")
            time.sleep(2)
            continue

        try:
            cleaned   = raw.replace("```json", "").replace("```", "").strip()
            extracted = json.loads(cleaned)
        except Exception:
            print(f"[enrich] {name}: JSON解析失敗")
            time.sleep(2)
            continue

        new_claims  = extracted.get("claims", [])
        new_memories = extracted.get("memories", [])

        if not new_claims and not new_memories:
            print(f"[enrich] {name}: 情報なし・スキップ")
            time.sleep(1)
            continue

        # 既存と重複除去
        existing_claims   = doc.get("claims", [])
        existing_memories = doc.get("memories", [])
        claims_to_add  = dedup(existing_claims, new_claims, key="content"
                               if isinstance(new_claims[0] if new_claims else {}, dict)
                               else "content")
        # claimsは文字列リスト形式に統一
        claims_to_add_dicts = []
        for c in claims_to_add:
            text = c if isinstance(c, str) else c.get("content", "")
            if text:
                claims_to_add_dicts.append({
                    "content": text[:200],
                    "date":    datetime.now(timezone.utc).isoformat(),
                    "source":  "batch",
                })

        memories_to_add = dedup(existing_memories, new_memories)

        # Embeddingを付与してmemories保存
        memories_with_vec = []
        for mem in memories_to_add:
            text = mem.get("content", "")
            if not text:
                continue
            vec = get_embedding(client, text)
            memories_with_vec.append({
                "content":   text[:300],
                "date":      datetime.now(timezone.utc).strftime("%Y-%m"),
                "category":  mem.get("category", ""),
                "embedding": vec,
                "source":    "batch",
            })
            time.sleep(0.5)  # Embedding APIのレート対策

        # MongoDB更新
        update = {}
        if claims_to_add_dicts:
            all_claims = (existing_claims + claims_to_add_dicts)[-MAX_CLAIMS:]
            update["claims"] = all_claims
        if memories_with_vec:
            # 新しい記憶を先頭に置き、新しい順に MAX_MEMORIES 件保持。
            # main.py($position:0)と向きを揃え、main側が直近書いた記憶を切り捨てない。
            all_memories = (memories_with_vec + existing_memories)[:MAX_MEMORIES]
            update["memories"] = all_memories

        if update:
            users_col.update_one({"_id": uid}, {"$set": update})
            print(f"[enrich] {name}: claims+{len(claims_to_add_dicts)}件, memories+{len(memories_with_vec)}件")
            success += 1

        time.sleep(3)

    print(f"[enrich] 完了: {success}/{len(targets)}人処理")


if __name__ == "__main__":
    main()
