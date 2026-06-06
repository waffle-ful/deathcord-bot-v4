"""
setup_mongo_index.py

MongoDB Atlas の Vector Search インデックスを作成するセットアップスクリプト。
新環境構築時・Atlas 再作成時に1回だけ実行する。

使い方:
    MONGODB_URI=<your_uri> python setup_mongo_index.py

インデックスが既に存在する場合は何もしない。
"""

import os
import sys
from pymongo import MongoClient
from pymongo.operations import SearchIndexModel

MONGODB_URI = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
DB_NAME     = "discord_bot_db"

INDEX_NAME  = "memories_vector_index"
INDEX_DEF   = {
    "fields": [
        {
            "type":          "vector",
            "path":          "memories.embedding",
            "numDimensions": 768,
            "similarity":    "cosine",
        }
    ]
}


def main():
    if not MONGODB_URI:
        print("[ERROR] MONGODB_URI（または MONGO_URL）が未設定です")
        sys.exit(1)

    client = MongoClient(MONGODB_URI)
    col    = client[DB_NAME]["users"]

    # 既存インデックス確認
    existing = {idx["name"] for idx in col.list_search_indexes()}
    if INDEX_NAME in existing:
        print(f"[OK] インデックス '{INDEX_NAME}' は既に存在します。スキップ。")
        return

    model = SearchIndexModel(
        definition=INDEX_DEF,
        name=INDEX_NAME,
        type="vectorSearch",
    )
    col.create_search_index(model)
    print(f"[OK] インデックス '{INDEX_NAME}' を作成しました。")
    print("     Atlas UI で ACTIVE になるまで数分かかります。")


if __name__ == "__main__":
    main()
