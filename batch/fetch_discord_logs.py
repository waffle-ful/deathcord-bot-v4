"""
fetch_discord_logs.py

Discordの過去 HOURS_BACK 時間分（既定5時間）のログをREST APIで取得し、logs.json として保存する。

【重要】
旧実装は discord.py の Gateway接続（client.start）を使っていたため、
バッチ実行のたびに本番Botと同一トークンで競合し、本番Botが強制切断されていた。
本実装は requests による HTTP REST APIのみを使用するため Gateway接続は一切行わない。

対応:
- 全テキストチャンネル + アクティブスレッド
- NSFWチャンネルを除外
- EXCLUDE_CHANNEL_IDS で個別除外も可能
- 規約同意ゲートが有効なら、未同意ユーザーの発言を除外（consent_util）

【重要2】
このスクリプトは Discord REST から生ログを直接読むため、main.py の on_message 側の
同意ガード（messages コレクションへの記録を止めるだけ）を素通りする。
未同意者の発言を Gemini（無料枠 = 学習利用あり）へ送らないため、ここでも必ずフィルタする。
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta

import requests

from consent_util import get_consent_filter

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_GUILD_ID  = os.environ["DISCORD_GUILD_ID"]
CHANNEL_IDS_RAW   = os.environ.get("DISCORD_CHANNEL_IDS", "")
CHANNEL_IDS       = [int(c.strip()) for c in CHANNEL_IDS_RAW.split(",") if c.strip()]
EXCLUDE_IDS_RAW   = os.environ.get("EXCLUDE_CHANNEL_IDS", "")
EXCLUDE_IDS       = set(int(c.strip()) for c in EXCLUDE_IDS_RAW.split(",") if c.strip())
# cron は 4h 間隔（summarize.yml）。窓は 5h にして実行スキップ/遅延時に最大1hの取りこぼしを吸収する。
# スナップショット要約なので隣接要約間の1h重複は無害。
HOURS_BACK            = int(os.environ.get("HOURS_BACK", "5"))
OUTPUT_PATH           = "/tmp/logs.json"
MAX_MSGS_PER_CHANNEL  = int(os.environ.get("MAX_MSGS_PER_CHANNEL", "500"))
MAX_TOTAL_API_CALLS   = int(os.environ.get("MAX_TOTAL_API_CALLS", "300"))
ALLOW_ALL_CHANNELS    = os.environ.get("ALLOW_ALL_CHANNELS", "false").lower() == "true"

BASE_URL = "https://discord.com/api/v10"
HEADERS  = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
    "Content-Type":  "application/json",
}

_api_call_count = 0


def api_get(path: str, params: dict = None) -> dict | list | None:
    """Discord REST APIへのGETリクエスト（レート制限対応・上限監視付き）"""
    global _api_call_count

    # C: グローバル上限 — 超えたら打ち切り（fetch_messages_from_channel の break に繋がる）
    if _api_call_count >= MAX_TOTAL_API_CALLS:
        print(f"[WARN] Total API call limit reached ({MAX_TOTAL_API_CALLS}), aborting fetch early")
        return None
    _api_call_count += 1

    url = BASE_URL + path
    for attempt in range(5):
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)

        # A: 先手スロットリング — 429 が来る前に自分でブレーキをかける
        remaining   = int(resp.headers.get("X-RateLimit-Remaining", 5))
        reset_after = float(resp.headers.get("X-RateLimit-Reset-After", "1"))
        if remaining <= 1:
            print(f"[fetch] Rate limit low (remaining={remaining}), sleeping {reset_after:.2f}s proactively")
            time.sleep(reset_after + 0.1)

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            print(f"[fetch] Rate limited (attempt {attempt + 1}/5). Waiting {retry_after:.1f}s...")
            time.sleep(retry_after + 1.0)  # +1.0 で保守的に待つ
            continue
        if resp.status_code == 403:
            return None
        if resp.status_code == 404:
            return None
        print(f"[fetch] API error {resp.status_code}: {path}")
        time.sleep(1)
    return None


def get_guild_channels() -> list[dict]:
    """サーバーの全チャンネル一覧を取得"""
    result = api_get(f"/guilds/{DISCORD_GUILD_ID}/channels")
    return result if isinstance(result, list) else []


def get_active_threads() -> list[dict]:
    """サーバーのアクティブスレッド一覧を取得"""
    result = api_get(f"/guilds/{DISCORD_GUILD_ID}/threads/active")
    if not result:
        return []
    return result.get("threads", [])


def is_excluded(channel: dict) -> bool:
    """除外すべきチャンネルか判定"""
    if channel.get("nsfw", False):
        return True
    if int(channel["id"]) in EXCLUDE_IDS:
        return True
    return False


def collect_target_channels(all_channels: list[dict], active_threads: list[dict]) -> list[dict]:
    """取得対象チャンネル一覧を構築"""
    # チャンネルタイプ: 0=テキスト, 5=アナウンス, 11=publicスレッド, 12=privateスレッド
    TEXT_TYPES   = {0, 5}
    THREAD_TYPES = {11, 12}

    targets = []

    if CHANNEL_IDS:
        # 指定チャンネルのみ
        id_set = set(CHANNEL_IDS)
        base = [ch for ch in all_channels if int(ch["id"]) in id_set and ch["type"] in TEXT_TYPES]
    else:
        # D: 全ch取得は明示的なオプトインを要求
        if not ALLOW_ALL_CHANNELS:
            print("[ERROR] DISCORD_CHANNEL_IDS is not set and ALLOW_ALL_CHANNELS is not 'true'.")
            print("[ERROR] Fetching all channels risks Discord BAN on large servers.")
            print("[ERROR] Either set DISCORD_CHANNEL_IDS or set ALLOW_ALL_CHANNELS=true explicitly.")
            raise SystemExit(1)
        print(f"[WARN] ALLOW_ALL_CHANNELS=true: fetching ALL {len(all_channels)} channels. Watch MAX_TOTAL_API_CALLS.")
        base = [ch for ch in all_channels if ch["type"] in TEXT_TYPES]

    parent_ids = set()
    for ch in base:
        if is_excluded(ch):
            print(f"[fetch] Skip (excluded/NSFW): #{ch.get('name')}")
            continue
        targets.append(ch)
        parent_ids.add(int(ch["id"]))

    # アクティブスレッドのうち対象チャンネル配下のものを追加
    for thread in active_threads:
        parent_id = int(thread.get("parent_id", 0))
        if parent_id in parent_ids and not is_excluded(thread) and thread["type"] in THREAD_TYPES:
            targets.append(thread)

    return targets


def fetch_messages_from_channel(channel_id: int, after_snowflake: int) -> list[dict]:
    """チャンネルから対象期間のメッセージを取得（ページネーション対応）"""
    messages = []
    last_id  = after_snowflake

    while True:
        # B: チャンネル毎上限 — ページネーション爆発を防ぐ
        if len(messages) >= MAX_MSGS_PER_CHANNEL:
            print(f"[WARN] Channel {channel_id} hit per-channel cap ({MAX_MSGS_PER_CHANNEL} msgs), stopping pagination")
            break

        params = {"limit": 100, "after": str(last_id)}
        batch  = api_get(f"/channels/{channel_id}/messages", params=params)
        if not batch:
            break

        # Discord APIは新しい順で返すので逆順に
        batch = list(reversed(batch))
        if not batch:
            break

        messages.extend(batch)
        last_id = int(batch[-1]["id"])

        # 100件未満なら最終ページ
        if len(batch) < 100:
            break

        # レート制限対策
        time.sleep(0.3)

    return messages


def datetime_to_snowflake(dt: datetime) -> int:
    """datetimeをDiscord Snowflake IDに変換"""
    discord_epoch = 1420070400000  # 2015-01-01 UTC (ms)
    ms = int(dt.timestamp() * 1000)
    return (ms - discord_epoch) << 22


def main():
    print(f"[fetch] Starting REST API fetch (no Gateway connection)")

    # 規約同意ゲート: 未同意者の発言は要約（＝Geminiへの送信）の対象外にする。
    # 判定できない場合はここで SystemExit(1) して要約自体を中止する（fail-closed）。
    consent = get_consent_filter()

    after_dt        = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    after_snowflake = datetime_to_snowflake(after_dt)

    print(f"[fetch] Fetching channels...")
    all_channels    = get_guild_channels()
    active_threads  = get_active_threads()
    print(f"[fetch] Found {len(all_channels)} channels, {len(active_threads)} active threads")

    targets = collect_target_channels(all_channels, active_threads)
    print(f"[fetch] Target channels: {len(targets)}")

    all_messages = []
    skipped_unagreed = 0
    for ch in targets:
        ch_id   = int(ch["id"])
        ch_name = ch.get("name", str(ch_id))
        label   = f"#{ch_name}" + (" [thread]" if ch["type"] in {11, 12} else "")
        print(f"[fetch] Reading {label} ...")

        raw_msgs = fetch_messages_from_channel(ch_id, after_snowflake)

        for msg in raw_msgs:
            # Botメッセージを除外
            author = msg.get("author", {})
            if author.get("bot", False):
                continue
            # Webhookメッセージを除外
            if msg.get("webhook_id"):
                continue
            # 規約に同意していないユーザーの発言は除外（LLMへ送らない）
            if not consent.allows(author.get("id")):
                skipped_unagreed += 1
                continue

            all_messages.append({
                "channel":   ch_name,
                "author":    author.get("global_name") or author.get("username", ""),
                "author_id": author.get("id", ""),
                "timestamp": msg.get("timestamp", ""),
                # 未同意者へのメンション(<@ID>)は送信前に伏せる
                "content":   consent.mask_content(msg.get("content", "")),
            })

        time.sleep(0.2)  # チャンネル間のレート制限対策

    # timestamp順にソート
    all_messages.sort(key=lambda m: m["timestamp"])

    print(f"[fetch] Total messages: {len(all_messages)}")
    if skipped_unagreed:
        print(f"[fetch] 規約未同意により除外: {skipped_unagreed}件")
    if consent.masked_mentions:
        print(f"[fetch] 未同意者へのメンションを伏せ字化: {consent.masked_mentions}件")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "fetched_at":    datetime.now(timezone.utc).isoformat(),
                "hours_back":    HOURS_BACK,
                "message_count": len(all_messages),
                "messages":      all_messages,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"[fetch] Saved {len(all_messages)} messages to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
