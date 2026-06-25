"""embed_summaries.py — 既存 summaries への embedding バックフィル（focus Tier3）。

summarize.py は【今後の】新規 doc に embedding を付けるが、既存 doc には無い。
意味検索を過去アーカイブにも効かせるため、embedding 未付与の doc を遡って埋める。

規約は embed_util.py に固定（書込・検索と同一 model/次元/task_type）。
再実行可能（付与済みはスキップ）。空/プレースホルダ要約は対象外。

環境変数:
  GEMINI_API_KEY            : 必須
  MONGODB_URI / MONGO_URL   : 必須
  EMBED_MAX_PER_RUN         : 1回の最大処理件数（既定 1000・5h タイムアウト回避）
  EMBED_SLEEP               : 1件ごとの待機秒（既定 0.5・無料枠レート保護）
"""

import os
import time

from pymongo import MongoClient, DESCENDING
from google import genai

from embed_util import embed_document

GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]
MONGODB_URI     = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
DB_NAME         = "discord_bot_db"
COLLECTION      = "summaries"
MAX_PER_RUN     = int(os.environ.get("EMBED_MAX_PER_RUN", "1000"))
SLEEP           = float(os.environ.get("EMBED_SLEEP", "0.5"))

# 中身の無い要約（topic 信号ゼロ）は embedding しても無駄＝API を焼くだけなので除外。
PLACEHOLDER_MARK = "メッセージがありませんでした"
MIN_SUMMARY_LEN  = 30


def _is_embeddable(summ: str) -> bool:
    if not summ or len(summ.strip()) < MIN_SUMMARY_LEN:
        return False
    return PLACEHOLDER_MARK not in summ


def main():
    print(f"[embed] backfill 開始 max={MAX_PER_RUN} sleep={SLEEP}s")
    client = genai.Client(api_key=GEMINI_API_KEY)
    col    = MongoClient(MONGODB_URI)[DB_NAME][COLLECTION]

    # embedding フィールドが【無い】doc のみ対象。新しい順＝よく引かれる直近から先に埋める。
    # 重要: プレースホルダは下で embedding:[] を $set して「処理済み」印にする。$size:0 を条件に
    # 入れると [] 印の doc が永遠に再ヒットし remaining が 0 に到達しない（再実行が終わらない）。
    # → 「$exists:False のみ」にすれば、[]印=フィールド有り=恒久除外、一過性失敗(未マーク)=再試行、
    #   の両立になる。書込側は vec があるときしか embedding を作らないので [] は skip 印専用。
    query = {"summary": {"$exists": True}, "embedding": {"$exists": False}}
    total_missing = col.count_documents(query)
    print(f"[embed] embedding 未付与: {total_missing}件（うち最大 {MAX_PER_RUN}件を今回処理）")

    cursor = col.find(
        query, {"summary": 1, "created_at": 1, "retro_date": 1},
        sort=[("created_at", DESCENDING)],
    ).limit(MAX_PER_RUN)

    done = skipped = failed = 0
    for d in cursor:
        summ = d.get("summary", "")
        date = d.get("retro_date") or str(d.get("created_at", ""))[:10]
        if not _is_embeddable(summ):
            col.update_one({"_id": d["_id"]}, {"$set": {"embedding": []}})  # 空印で再走査から外す
            skipped += 1
            continue
        try:
            vec = embed_document(client, summ)
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                print(f"[embed] 429/枯渇で中断（{done}件処理済・再実行で続行可）: {err[:120]}")
                break
            print(f"[embed] 失敗（{date}・スキップ）: {err[:120]}")
            failed += 1
            time.sleep(SLEEP)
            continue
        if vec:
            col.update_one({"_id": d["_id"]}, {"$set": {"embedding": vec}})
            done += 1
            if done % 50 == 0:
                print(f"[embed] {done}件付与...（最新処理日={date}）")
        else:
            failed += 1
        time.sleep(SLEEP)

    remaining = col.count_documents(query)
    print(f"[embed] 完了: 付与={done} 空印スキップ={skipped} 失敗={failed} / 残り未付与={remaining}")
    if remaining:
        print("[embed] 残りがあるので再実行で続行してください（新しい順なので直近から埋まる）。")


if __name__ == "__main__":
    main()
