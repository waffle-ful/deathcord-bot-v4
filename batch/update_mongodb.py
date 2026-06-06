"""
update_mongodb.py

/tmp/summary_result.json を読み込み MongoDBへ保存。
全件永久保存（削除なし）。created_at インデックスで高速検索。
ニックネームマップも自動マージ保存。
"""

import os
import json
from datetime import datetime, timezone

from pymongo import MongoClient, DESCENDING

MONGODB_URI = os.environ["MONGODB_URI"]
DB_NAME     = "discord_bot_db"
COLLECTION  = "summaries"
INPUT_PATH  = "/tmp/summary_result.json"


def main():
    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        result = json.load(f)

    print("[mongodb] Connecting...")
    client    = MongoClient(MONGODB_URI)
    db        = client[DB_NAME]
    col       = db[COLLECTION]
    sys_col   = db["system"]

    # インデックス作成（初回のみ実際に作成される）
    # is_latest フラグは廃止（複数 True 残留の競合を防ぐため）。最新判定は
    # created_at 降順ソートで行う（読み手: main.get_latest_summary /
    # summarystatus / post_summary.fetch_latest_summary）。
    col.create_index([("created_at", DESCENDING)], background=True)

    new_record = {
        "summary":       result.get("summary", ""),
        "message_count": result.get("message_count", 0),
        "created_at":    result.get("created_at", datetime.now(timezone.utc).isoformat()),
        "fetched_at":    result.get("fetched_at", ""),
    }
    inserted = col.insert_one(new_record)
    total    = col.count_documents({})
    print(f"[mongodb] Inserted: {inserted.inserted_id} (total: {total} records)")
    print(f"[mongodb] Done. messages={new_record['message_count']}")

    # ニックネームマップをsystem_colに自動マージ保存
    nickname_map = result.get("nickname_map", {})
    if nickname_map:
        existing_doc = sys_col.find_one({"_id": "nickname_map"}) or {"map": {}}
        existing_map = existing_doc.get("map", {})
        # 手動登録（existing）を優先・AIが検出した新規のみ追加
        merged = {**nickname_map, **existing_map}
        sys_col.update_one(
            {"_id": "nickname_map"},
            {"$set": {"map": merged}},
            upsert=True,
        )
        print(f"[mongodb] nickname_map updated: {merged}")
    else:
        print("[mongodb] nickname_map: 変更なし")


if __name__ == "__main__":
    main()
