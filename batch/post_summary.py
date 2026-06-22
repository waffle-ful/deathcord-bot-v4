"""
post_summary.py

MongoDBの最新要約をDiscordの公開チャンネルにEmbed形式で投稿する。
各セクションを別フィールドに分割して全文表示。
GitHub Actionsから1日1回実行（JST深夜0時）。
"""

import os
import re
import time
from datetime import datetime, timezone, timedelta

import requests
from pymongo import MongoClient

MONGODB_URI        = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
DISCORD_BOT_TOKEN  = os.environ["DISCORD_BOT_TOKEN"]
SUMMARY_CHANNEL_ID = os.environ["SUMMARY_CHANNEL_ID"]
DB_NAME            = "discord_bot_db"
JST                = timezone(timedelta(hours=9))

# セクションのアイコンマッピング
SECTION_ICONS = {
    "全体の雰囲気":       "🌡️",
    "主なトピック":       "📌",
    "感情の波":           "🎢",
    "注目の発言":         "💬",
    "今日の内輪ネタ":     "🔑",
    "メンバーの人間関係": "🤝",
    "ユーザーの感情状態": "😊",
    "直近の話題":         "🔥",
    "会話の特徴":         "✨",
}

# 表示優先順位（上から順に表示）
SECTION_ORDER = [
    "直近の話題",
    "感情の波",
    "今日の内輪ネタ",
    "メンバーの人間関係",
    "注目の発言",
    "ユーザーの感情状態",
    "全体の雰囲気",
    "主なトピック",
    "会話の特徴",
]


def fetch_latest_summary(col) -> dict | None:
    """最新の通常日報を1件取得。

    is_latest フラグは廃止。created_at 降順で最新を決める。retro 要約は
    is_retro / retro_date の両方で除外する（get_latest_summary と同じ規約）。
    """
    return col.find_one(
        {"summary": {"$exists": True},
         "is_retro": {"$ne": True},
         "retro_date": {"$exists": False}},
        sort=[("created_at", -1)]
    )


def parse_sections(summary: str) -> list[tuple[str, str]]:
    """## セクション名\n本文 を (title, body) のリストに変換"""
    parts    = re.split(r"\n##\s+", "\n" + summary)
    sections = []
    for part in parts:
        if not part.strip():
            continue
        split = part.strip().split("\n", 1)
        title = split[0].strip()
        body  = split[1].strip() if len(split) > 1 else ""
        if title and body:
            sections.append((title, body))
    return sections


def sort_sections(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """優先度順に並び替え"""
    ordered = []
    used    = set()

    for keyword in SECTION_ORDER:
        for title, body in sections:
            if keyword in title and title not in used:
                ordered.append((title, body))
                used.add(title)
                break

    # 残りを末尾に
    for title, body in sections:
        if title not in used:
            ordered.append((title, body))

    return ordered


def build_embeds(doc: dict) -> list[dict]:
    """セクションごとにフィールドを作り、6000文字制限で複数Embedに分割"""
    now_jst   = datetime.now(JST)
    date_str  = now_jst.strftime("%Y年%m月%d日")
    summary   = doc.get("summary", "")
    msg_count = doc.get("message_count", 0)
    created   = (doc.get("created_at") or "")[:10]

    sections  = parse_sections(summary)
    sections  = sort_sections(sections)

    # フィールドを構築（1フィールド最大1020文字）
    fields = []
    for title, body in sections:
        icon  = next((v for k, v in SECTION_ICONS.items() if k in title), "📋")
        label = f"{icon} {title}"
        # 1024文字制限（少し余裕を持たせて1020）
        if len(body) > 1020:
            body = body[:1017] + "…"
        fields.append({"name": label, "value": body, "inline": False})

    # 集計フィールド
    fields.append({
        "name":   "📊 集計",
        "value":  f"対象日: {created} / 総メッセージ数: {msg_count:,}件",
        "inline": False,
    })

    # 6000文字制限でEmbedを分割（フィールド25個制限も考慮）
    embeds       = []
    current_fields = []
    current_chars  = 0
    title_str      = f"📰 {date_str} のサーバー日報"

    for i, field in enumerate(fields):
        field_chars = len(field["name"]) + len(field["value"])
        # 最初のEmbedはtitleとfooterの分を引く
        limit = 5800 if embeds else 5800 - len(title_str)

        if (current_chars + field_chars > limit or len(current_fields) >= 25) and current_fields:
            # 新しいEmbedを作成
            embed = {
                "color":  0x5865F2,
                "fields": current_fields,
            }
            if not embeds:
                embed["title"] = title_str
            else:
                embed["title"] = f"📰 {date_str} のサーバー日報（続き）"
            embeds.append(embed)
            current_fields = []
            current_chars  = 0

        current_fields.append(field)
        current_chars += field_chars

    # 残りをまとめて最後のEmbedに
    if current_fields:
        embed = {
            "color":  0x5865F2,
            "fields": current_fields,
            "footer": {
                "text": f"空気くん日報 • {now_jst.strftime('%H:%M')} JST",
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if not embeds:
            embed["title"] = title_str
        else:
            embed["title"] = f"📰 {date_str} のサーバー日報（続き）"
        embeds.append(embed)

    return embeds


def post_to_discord(embeds: list[dict]):
    """Discord APIにPOSTする（1Embed = 1メッセージ）。

    Discordの6000文字制限は「1メッセージ内の全Embedの合計」に対する制限であり、
    Embed単位ではない。build_embeds が各Embedを ≤5800字 で分割しているので、
    1メッセージ1Embedで送れば必ず 6000 未満に収まる。複数Embedを同梱すると
    合計が6000を超えて 400 Bad Request になり日報が落ちていた。
    """
    url     = f"https://discord.com/api/v10/channels/{SUMMARY_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type":  "application/json",
    }

    for idx, embed in enumerate(embeds):
        payload = {
            "embeds": [embed],
            "allowed_mentions": {"parse": []}  # すべてのメンションを無効化
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code in (200, 201):
            print(f"[post] 投稿成功 ({idx+1}/{len(embeds)}): message_id={resp.json().get('id')}")
        else:
            print(f"[ERROR] 投稿失敗: {resp.status_code} {resp.text}")
            resp.raise_for_status()
        time.sleep(0.5)  # メッセージ間レート制限の緩和


def main():
    print("[post] Connecting to MongoDB...")
    mongo = MongoClient(MONGODB_URI)
    col   = mongo[DB_NAME]["summaries"]
    doc   = fetch_latest_summary(col)

    if not doc:
        print("[post] No summary found. Skipping.")
        return

    print("[post] Building embeds...")
    embeds = build_embeds(doc)
    print(f"[post] {len(embeds)} embed(s) to post.")
    post_to_discord(embeds)
    print("[post] Done.")


if __name__ == "__main__":
    main()
