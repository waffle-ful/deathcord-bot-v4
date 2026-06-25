"""embed_summaries.py — 既存 summaries への embedding バックフィル（focus Tier3）。

summarize.py は【今後の】新規 doc に embedding を付けるが、既存 doc には無い。
意味検索を過去アーカイブにも効かせるため、embedding 未付与の doc を遡って埋める。

規約は embed_util.py に固定（書込・検索と同一 model/次元/task_type）。
再実行可能（付与済みはスキップ）。空/プレースホルダ要約は対象外。

レート: 無料枠の embedding は TPM≈30,000（トークン/分）が律速。1doc 最大~1500トークンなので
持続可能は ~20doc/分＝1doc あたり ~3秒。既定 SLEEP=3.5s で ~15doc/分（TPM に余裕）に抑える。
さらに 429 が来たら API 指定秒だけ待って【同じ doc を再試行】するので、1回の実行で完走できる
（即中断して何度も再実行する必要はない）。連続 429 が続く＝日次クォータ(RPD)枯渇とみなし中断。

環境変数:
  GEMINI_API_KEY            : 必須
  MONGODB_URI / MONGO_URL   : 必須
  EMBED_MAX_PER_RUN         : 1回の最大処理件数（既定 1000・5h タイムアウト回避）
  EMBED_SLEEP               : 1件ごとの待機秒（既定 3.5・TPM 30k に収めるペース）
"""

import os
import re
import time

from pymongo import MongoClient, DESCENDING
from google import genai

from embed_util import embed_document

GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]
MONGODB_URI     = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
DB_NAME         = "discord_bot_db"
COLLECTION      = "summaries"
MAX_PER_RUN     = int(os.environ.get("EMBED_MAX_PER_RUN", "1000"))
SLEEP           = float(os.environ.get("EMBED_SLEEP", "3.5"))   # TPM≈30k 内に収めるペース(~15doc/分)
MAX_CONSEC_429  = 8     # 429 が連続でこれを超えたら日次クォータ枯渇とみなし中断


def _retry_wait(err: str) -> float:
    """429 詳細から retryDelay 秒を拾う（google-genai は 'retryDelay': '34s' を返すことが多い）。
    取れなければ 60s。"""
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+)", err)
    return float(m.group(1)) + 2.0 if m else 60.0

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
    consec_429 = 0
    aborted = False
    for d in cursor:
        summ = d.get("summary", "")
        date = d.get("retro_date") or str(d.get("created_at", ""))[:10]
        if not _is_embeddable(summ):
            col.update_one({"_id": d["_id"]}, {"$set": {"embedding": []}})  # 空印で再走査から外す
            skipped += 1
            continue

        # 429 の間は同じ doc を待って再試行（即中断しない＝1回で完走させる）。
        # 連続429が MAX_CONSEC_429 を超えたら日次クォータ枯渇とみなし中断（明日以降に再実行で続行）。
        vec = None
        while True:
            try:
                vec = embed_document(client, summ)
                consec_429 = 0
                break
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    consec_429 += 1
                    if consec_429 > MAX_CONSEC_429:
                        print(f"[embed] 連続429が{MAX_CONSEC_429}回超過＝日次クォータ枯渇の可能性。"
                              f"中断（{done}件処理済・後日 再実行で続行）。")
                        aborted = True
                        break
                    wait = _retry_wait(err)
                    print(f"[embed] 429: {wait:.0f}s 待機して再試行（doc={date} 連続{consec_429}）")
                    time.sleep(wait)
                    continue
                print(f"[embed] 失敗（{date}・スキップ）: {err[:120]}")
                failed += 1
                break

        if aborted:
            break
        if vec:
            col.update_one({"_id": d["_id"]}, {"$set": {"embedding": vec}})
            done += 1
            if done % 25 == 0:
                print(f"[embed] {done}件付与...（最新処理日={date} 残≈{max(0, total_missing - done - skipped)}）")
        time.sleep(SLEEP)

    remaining = col.count_documents(query)
    print(f"[embed] 完了: 付与={done} 空印スキップ={skipped} 失敗={failed} / 残り未付与={remaining}")
    if remaining:
        print("[embed] 残りがあるので再実行で続行してください（新しい順なので直近から埋まる）。")


if __name__ == "__main__":
    main()
