"""
reembed_memories.py

【ワンショット移行スクリプト】
既存 users.memories[].embedding のうち「空配列」または「3072次元でない」ものを
gemini-embedding-001（main.py / search_memories と同一・3072次元）で埋め直す。

背景: focus_summary.py が廃止モデル text-embedding-004 を呼んでおり、
embed_content が例外を投げて embedding が空([])のまま保存され続けていた。
コード側は修正済みだが、既存記憶は空のままで in-Python cosine の比較対象に
ならない（best=0.000）。このスクリプトで過去分をバックフィルする。

使い方:
    MONGO_URL=<uri> GEMINI_API_KEY=<key> python reembed_memories.py
    # ドライラン（書き込まず対象数だけ表示）:
    DRY_RUN=1 MONGO_URL=<uri> GEMINI_API_KEY=<key> python reembed_memories.py

冪等: 既に3072次元の記憶はスキップするので、複数回実行しても安全。
"""

import os
import sys
import time

from pymongo import MongoClient
from google import genai

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MONGODB_URI    = os.environ.get("MONGO_URL") or os.environ.get("MONGODB_URI")
DB_NAME        = "discord_bot_db"
EMBED_MODEL    = "models/gemini-embedding-001"  # main.py と同一（既定3072次元）
TARGET_DIM     = 3072
DRY_RUN        = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
SLEEP_SEC      = 0.5   # Embedding API レート対策


def get_embedding(client: genai.Client, text: str) -> list[float]:
    resp = client.models.embed_content(
        model=EMBED_MODEL,
        contents=text[:500],
    )
    if hasattr(resp, "embeddings") and resp.embeddings:
        return list(resp.embeddings[0].values)
    if hasattr(resp, "embedding"):
        return list(resp.embedding.values)
    return []


def needs_reembed(mem: dict) -> bool:
    emb = mem.get("embedding") or []
    return len(emb) != TARGET_DIM


def main():
    if not MONGODB_URI:
        print("[ERROR] MONGO_URL（または MONGODB_URI）が未設定です")
        sys.exit(1)

    db     = MongoClient(MONGODB_URI)[DB_NAME]
    users  = db["users"]
    client = genai.Client(api_key=GEMINI_API_KEY)

    cursor = users.find(
        {"memories.embedding": {"$exists": True}},
        {"memories": 1},
    )

    total_users = total_fixed = total_failed = total_skipped = 0

    for doc in cursor:
        uid      = doc["_id"]
        memories = doc.get("memories", [])
        targets  = [m for m in memories if m.get("content") and needs_reembed(m)]
        if not targets:
            continue
        total_users += 1
        print(f"[user {uid}] 対象 {len(targets)}/{len(memories)} 件")

        if DRY_RUN:
            total_skipped += len(targets)
            continue

        changed = False
        for m in memories:
            if not (m.get("content") and needs_reembed(m)):
                continue
            try:
                vec = get_embedding(client, m["content"])
            except Exception as e:
                print(f"  [WARN] embed失敗: {m['content'][:30]}… ({e})")
                total_failed += 1
                continue
            if len(vec) == TARGET_DIM:
                m["embedding"] = vec
                changed = True
                total_fixed += 1
            else:
                print(f"  [WARN] 想定外の次元 {len(vec)}: {m['content'][:30]}…")
                total_failed += 1
            time.sleep(SLEEP_SEC)

        if changed:
            users.update_one({"_id": uid}, {"$set": {"memories": memories}})
            print(f"  → 書き戻し完了")

    print("\n=== 完了 ===")
    print(f"対象ユーザー: {total_users}")
    if DRY_RUN:
        print(f"再embed対象（ドライラン）: {total_skipped} 件 — 実行するには DRY_RUN を外してください")
    else:
        print(f"再embed成功: {total_fixed} 件 / 失敗: {total_failed} 件")


if __name__ == "__main__":
    main()
