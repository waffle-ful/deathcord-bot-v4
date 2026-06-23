"""
retro_backfill.py

過去N日分の遡及日報（retro）をまとめて生成・保存する。
focus/記憶の素材になる summaries アーカイブの「空白期間」を埋め戻すための一括処理。

設計方針:
- 逐次処理＋日間スリープで Discord のレート制限/BAN を回避（Gemini=gemma-4 は 1500/日で余裕）。
- 既存 retro_date はスキップ＝中断しても再開可能。
- 各日を Discord にも投稿する（BACKFILL_POST、既定ON）。OFF にすれば Mongo 保存のみ。
- 429/レート制限を検知したら安全のため即停止。

環境変数:
  BACKFILL_START_DATE : 開始日 YYYY-MM-DD（この日から過去へ遡る）
  BACKFILL_DAYS       : 遡る日数（既定 31）
  BACKFILL_SLEEP      : 日間スリープ秒（既定 45）
  BACKFILL_POST       : 各日を Discord 投稿するか（既定 true。false/0/no/off で投稿しない）
  ほか DISCORD_BOT_TOKEN / DISCORD_GUILD_ID / DISCORD_CHANNEL_IDS / EXCLUDE_CHANNEL_IDS /
       GEMINI_API_KEY / MONGODB_URI / SUMMARY_CHANNEL_ID（retro_summarize と共通）
"""

import os
import time
from datetime import date as date_cls, timedelta

from pymongo import MongoClient
from google import genai

import retro_summarize as R  # 同一ディレクトリのモジュールを再利用

START_DATE_STR = os.environ["BACKFILL_START_DATE"]
DAYS           = int(os.environ.get("BACKFILL_DAYS", "31"))
SLEEP_SEC      = int(os.environ.get("BACKFILL_SLEEP", "45"))
# 各日をDiscordにも投稿するか（既定ON。要約に時間がかかるので連投スパムにはなりにくい想定）
POST           = os.environ.get("BACKFILL_POST", "true").strip().lower() not in ("0", "false", "no", "off")


def _is_rate_limited(err: str) -> bool:
    e = err.lower()
    return "429" in e or "rate limit" in e or "too many requests" in e


def main():
    try:
        start = date_cls.fromisoformat(START_DATE_STR)
    except ValueError:
        print(f"[backfill] 開始日フォーマット不正: {START_DATE_STR!r}")
        return

    print(f"[backfill] {start.isoformat()} から過去 {DAYS} 日分を遡及生成"
          f"（日間 {SLEEP_SEC}s スリープ / Discord投稿={'ON' if POST else 'OFF'}）")

    client_ai = genai.Client(api_key=R.GEMINI_API_KEY)
    mongo     = MongoClient(R.MONGODB_URI)
    col       = mongo[R.DB_NAME]["summaries"]

    done = skipped = empty = failed = 0
    for i in range(DAYS):
        d = start - timedelta(days=i)
        print(f"\n=== [{i + 1}/{DAYS}] {d.isoformat()} ===")
        try:
            # 既存日はスキップ。投稿は POST トグル（既定ON）に従う。
            status = R.run_for_date(d, client_ai, col, skip_existing=True, post=POST)
            if   status == "done":    done += 1
            elif status == "skipped": skipped += 1
            else:                     empty += 1
        except Exception as e:
            failed += 1
            print(f"[backfill] {d.isoformat()} 失敗: {e}")
            if _is_rate_limited(str(e)):
                print("[backfill] レート制限を検知。BAN回避のため処理を停止します。")
                break

        if i < DAYS - 1:
            time.sleep(SLEEP_SEC)

    print(f"\n[backfill] 完了: 生成={done} / スキップ={skipped} / 空={empty} / 失敗={failed}")


if __name__ == "__main__":
    main()
