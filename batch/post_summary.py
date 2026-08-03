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

from discord_post import split_for_field, pack_fields_into_embeds, post_embeds

MONGODB_URI        = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
DISCORD_BOT_TOKEN  = os.environ["DISCORD_BOT_TOKEN"]
SUMMARY_CHANNEL_ID = os.environ["SUMMARY_CHANNEL_ID"]
DB_NAME            = "discord_bot_db"
JST                = timezone(timedelta(hours=9))

# セクションのアイコンマッピング（新項目に追従。旧称は後方互換で残置）
SECTION_ICONS = {
    "全体の雰囲気":       "🌡️",
    "主なトピック":       "📌",
    "メンバー別の動き":   "👥",
    "メンバーの人間関係": "🤝",
    "記憶すべき":         "📝",
    "感情の波":           "🎢",
    "今日の内輪ネタ":     "🔑",
    "直近の話題":         "🔥",
    # 旧称（後方互換・残置）
    "注目の発言":         "💬",
    "ユーザーの感情状態": "😊",
    "会話の特徴":         "✨",
}

# 表示優先順位（上から順に表示）
SECTION_ORDER = [
    "直近の話題",
    "メンバー別の動き",
    "記憶すべき",
    "感情の波",
    "メンバーの人間関係",
    "今日の内輪ネタ",
    "主なトピック",
    "全体の雰囲気",
    # 旧称（後方互換）
    "注目の発言",
    "ユーザーの感情状態",
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
        # 1024字超は切り捨てず「(続きN)」フィールドに分割して全文表示
        for i, chunk in enumerate(split_for_field(body)):
            label = f"{icon} {title}" if i == 0 else f"{icon} {title}（続き{i+1}）"
            fields.append({"name": label[:256], "value": chunk, "inline": False})

    # 集計フィールド
    fields.append({
        "name":   "📊 集計",
        "value":  f"対象日: {created} / 総メッセージ数: {msg_count:,}件",
        "inline": False,
    })

    # 詰め方は batch/discord_post.py に集約。旧実装は 5800字/25フィールド で詰めていたが、
    # Discordは仕様上限の内側でも規模が大きいと 400 ではなく 500 code:0 を返す
    # （2026-08-04に focus で実測。経緯は discord_post.py 冒頭）。実績のある規模まで絞る。
    title_str = f"📰 {date_str} のサーバー日報"
    return pack_fields_into_embeds(
        fields,
        color=0x5865F2,
        title_for=lambda i, n: title_str if i == 0 else f"📰 {date_str} のサーバー日報（続き{i+1}）",
        footer_for=lambda i, n: f"空気くん日報 • {i+1}/{n} • {now_jst.strftime('%H:%M')} JST",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def post_to_discord(embeds: list[dict]):
    """Discord APIにPOSTする（1Embed = 1メッセージ）。

    Discordの6000文字制限は「1メッセージ内の全Embedの合計」に対する制限であり、
    Embed単位ではない。複数Embedを同梱すると合計が6000を超えて 400 になり日報が落ちる。

    投稿の再試行・二分割救出・平文フォールバックは discord_post.post_embeds が担う。
    旧実装は1回の失敗で raise_for_status していたため、Discord側の一時的な5xxで
    その日の日報が丸ごと落ち、残りのEmbedも投稿されないまま終わっていた。
    """
    url     = f"https://discord.com/api/v10/channels/{SUMMARY_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type":  "application/json",
    }
    # 救出手段を尽くしてなお届かなかった場合だけ、ワークフローを赤くして気づけるようにする
    if not post_embeds(url, headers, embeds, label="日報", pace=0.5):
        raise RuntimeError("日報の一部がDiscordに投稿できませんでした（上のログを参照）")


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
