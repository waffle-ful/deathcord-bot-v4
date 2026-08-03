import os
import json
import time
import re
from datetime import datetime, timezone, timedelta, date as date_cls

import requests
from pymongo import MongoClient, DESCENDING
from google import genai
from google.genai import types

from model_chain import HEAVY_MODEL_CHAIN, ATTEMPTS_PER_MODEL, output_tokens

# --- 環境変数 ---
TARGET_DATE_STR    = os.environ.get("TARGET_DATE", "")   # YYYY-MM-DD（backfillから import する際は未設定でも可）
DISCORD_BOT_TOKEN  = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_GUILD_ID   = int(os.environ["DISCORD_GUILD_ID"])
CHANNEL_IDS_RAW    = os.environ.get("DISCORD_CHANNEL_IDS", "")
CHANNEL_IDS        = [int(c.strip()) for c in CHANNEL_IDS_RAW.split(",") if c.strip()]
EXCLUDE_IDS_RAW    = os.environ.get("EXCLUDE_CHANNEL_IDS", "")
EXCLUDE_IDS        = set(int(c.strip()) for c in EXCLUDE_IDS_RAW.split(",") if c.strip())
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
MONGODB_URI        = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
SUMMARY_CHANNEL_ID = os.environ["SUMMARY_CHANNEL_ID"]
# モデルは batch/model_chain.py に集約（gemma-4 は TPM 無制限枠の撤廃により 2026-08-03 に撤去）
DB_NAME            = "discord_bot_db"
JST                = timezone(timedelta(hours=9))
CONTEXT_DAYS       = 2

RETRO_SUMMARY_PROMPT = """\
あなたはこのDiscordサーバーの古参メンバーです。
指定された日付のチャットログと、その前後の要約（文脈）を読んで
「過去の観察メモ」を書いてください。
前置き・導入文は一切不要です。各セクションの見出しから即座に本文を書き始めてください。
各セクションは具体的に書いてください（箇条書き指定のセクションは最低3項目）。

【重要: 人物の識別ルール】
- ログの各行には「表示名(uid:数字)」の形式でユーザーIDが付いています。
- 同じuidの発言は必ず同一人物です。名前が似ていてもuidが異なれば別人として扱ってください。
- 人物に言及する際は表示名のみを使用し、uidは書かないでください。

## 全体の雰囲気・トーン
（その日のサーバー全体の空気感を簡潔に）

## 主なトピック
（話されていた話題を箇条書きで。各トピックに一言）

## メンバー別の動き
（発言が目立ったメンバーを名前付きで列挙し、各人の具体的な発言・行動・テンションを箇条書きで。
　形式は「- 表示名: 何をどう言っていたか・どんな様子だったか」。最低3人、いれば多いほどよい）

## メンバーの人間関係・関係性
（誰と誰が絡んでいたか、仲良さそうな組み合わせ、いじり合いの構図などを名前付きで）

## 記憶すべき事実・発言・決定
（後から思い出す価値のある情報を名前付きで箇条書き。誰かの主張・意見・予定・決定・報告・印象的な出来事。
　形式は「- 表示名: 〜と主張/予定/報告した」。憶測は書かず、ログにある事実だけ）

## 感情の波・注目の瞬間
（何で盛り上がり何で冷めたか、テンションの上下と、特に反応が多かった発言・面白い流れを具体的に）

## 今日の内輪ネタ・キーワード
（独自ワード・ノリ・内輪ジョークを箇条書きで）
"""

SECTION_ICONS = {
    "全体の雰囲気":       "🌡️",
    "主なトピック":       "📌",
    "メンバー別の動き":   "👥",
    "メンバーの人間関係": "🤝",
    "記憶すべき":         "📝",
    "感情の波":           "🎢",
    "今日の内輪ネタ":     "🔑",
    # 旧称（後方互換・残置）
    "注目の発言":         "💬",
    "ユーザーの感情状態": "😊",
    "直近の話題":         "🔥",
    "会話の特徴":         "✨",
}

BASE_URL     = "https://discord.com/api/v10"
REST_HEADERS = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
    "Content-Type":  "application/json",
}

def _api_get(path: str, params: dict = None):
    url = BASE_URL + path
    for attempt in range(5):
        resp = requests.get(url, headers=REST_HEADERS, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            print(f"[fetch] Rate limited. Waiting {retry_after:.1f}s...")
            time.sleep(retry_after + 0.5)
            continue
        if resp.status_code in (403, 404):
            return None
        time.sleep(1)
    return None

def _datetime_to_snowflake(dt: datetime) -> int:
    discord_epoch = 1420070400000
    return (int(dt.timestamp() * 1000) - discord_epoch) << 22

def is_excluded(channel: dict) -> bool:
    if channel.get("nsfw", False):
        return True
    return int(channel["id"]) in EXCLUDE_IDS

def fetch_day_logs(target_date: date_cls) -> list[dict]:
    """指定日のDiscordログをREST APIで取得"""
    after_dt  = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=timezone.utc)
    before_dt = after_dt + timedelta(days=1)
    after_sf  = _datetime_to_snowflake(after_dt)
    before_sf = _datetime_to_snowflake(before_dt)

    # 1. チャンネル一覧取得
    all_channels = _api_get(f"/guilds/{DISCORD_GUILD_ID}/channels") or []
    TEXT_TYPES   = {0, 5}  # GUILD_TEXT, GUILD_ANNOUNCEMENT
    THREAD_TYPES = {11, 12} # PUBLIC_THREAD, PRIVATE_THREAD

    if CHANNEL_IDS:
        id_set = set(CHANNEL_IDS)
        base = [ch for ch in all_channels if int(ch["id"]) in id_set and ch["type"] in TEXT_TYPES]
    else:
        base = [ch for ch in all_channels if ch["type"] in TEXT_TYPES]

    targets = []
    parent_ids = set()
    for ch in base:
        if is_excluded(ch):
            continue
        targets.append(ch)
        parent_ids.add(int(ch["id"]))

    # 2. アクティブなスレッドも取得対象に含める
    thread_resp = _api_get(f"/guilds/{DISCORD_GUILD_ID}/threads/active") or {}
    for t in thread_resp.get("threads", []):
        if int(t.get("parent_id", 0)) in parent_ids and not is_excluded(t) and t["type"] in THREAD_TYPES:
            targets.append(t)

    print(f"[debug] Starting log fetch for {len(targets)} targets (including threads)...")

    all_messages = []
    seen_ids = set()  # 重複排除用：これで16000件ループを防ぐ

    for ch in targets:
    
        ch_id   = int(ch["id"])
        ch_name = ch.get("name", str(ch_id))
        print(f"[fetch] Reading #{ch_name}...")

        last_id  = after_sf
        ch_count = 0

        for _ in range(50):
            # after のみでページング。before は Discord 仕様で after と排他（同時指定は
            # 未定義動作で before が無視され翌日以降まで溢れる＝15,858件バグの原因）。
            # before_sf は手動境界チェックで対象日の終端を越えたら打ち切る。
            params = {"limit": 100, "after": str(last_id)}
            batch  = _api_get(f"/channels/{ch_id}/messages", params=params)

            if not batch or not isinstance(batch, list):
                break

            batch_sorted = sorted(batch, key=lambda x: int(x["id"]))  # ID昇順（古い順）

            crossed = False
            for msg in batch_sorted:
                if int(msg["id"]) >= before_sf:    # 対象日の終端を越えた＝翌日以降
                    crossed = True
                    break
                m_id = msg["id"]
                if m_id in seen_ids:
                    continue
                seen_ids.add(m_id)
                author = msg.get("author", {})
                if author.get("bot") or msg.get("webhook_id"):
                    continue
                all_messages.append({
                    "channel":   ch_name,
                    "author":    author.get("global_name") or author.get("username", ""),
                    "author_id": author.get("id", ""),
                    "timestamp": msg.get("timestamp", ""),
                    "content":   msg.get("content", ""),
                })
                ch_count += 1

            new_last_id = int(batch_sorted[-1]["id"])
            if crossed or new_last_id <= last_id or len(batch) < 100:
                break

            last_id = new_last_id
            time.sleep(0.4)

        if ch_count:
            print(f"[fetch]   #{ch_name}: {ch_count}件")
        time.sleep(0.2)  # チャンネル間のレート制限対策

    # 全チャンネルのメッセージを時系列順に並び替え
    all_messages.sort(key=lambda m: m["timestamp"])
    if all_messages:
        print(f"[fetch] 取得期間 {all_messages[0]['timestamp'][:10]}〜{all_messages[-1]['timestamp'][:10]} "
              f"(対象日={target_date.isoformat()}) ← 対象日と一致しなければ日境界異常")
    print(f"[fetch] Final unique messages count: {len(all_messages)}")
    return all_messages

def fetch_context_summaries(col, target_date: date_cls, days: int) -> str:
    start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc) - timedelta(days=days)
    end   = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc) + timedelta(days=days+1)
    docs  = list(col.find({"created_at": {"$gte": start.isoformat(), "$lt": end.isoformat()}}).sort("created_at", 1).limit(20))
    if not docs: return ""
    return "\n\n".join([f"【{d.get('created_at', '')[:10]}の要約抜粋】\n{d.get('summary', '')[:500]}" for d in docs])

def _extract_retry_wait(err: str) -> float:
    m = re.search(r"retry in ([\d.]+)s", err)
    return float(m.group(1)) + 2.0 if m else 60.0

def generate_summary(client_ai: genai.Client, log_text: str, context: str) -> str:
    user_prompt = RETRO_SUMMARY_PROMPT.strip() + "\n\n以下のチャットログの観察メモを書いてください。\n前置きは一切不要です。\n\n"
    if context:
        user_prompt += f"【前後の文脈】\n{context}\n\n---\n\n"
    user_prompt += f"【対象日のログ】\n{log_text}"

    for model, label in HEAVY_MODEL_CHAIN:
        for attempt in range(ATTEMPTS_PER_MODEL):
            try:
                resp = client_ai.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=output_tokens(5500),   # thinking 系の空応答対策
                    ),
                )
                text = getattr(resp, "text", None)
                if text and text.strip():
                    print(f"[retro] {label} 成功 ({len(text)}文字)")
                    return text.strip()
                time.sleep(5)
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    wait = _extract_retry_wait(err)
                    print(f"[WARN] 429 {label} attempt{attempt+1}, {wait:.1f}s待機...")
                    time.sleep(wait)
                else:
                    print(f"[ERROR] {label}: {e}")
                    break
    raise RuntimeError("要約生成失敗")

def save_to_mongodb(col, summary: str, target_date: date_cls, msg_count: int):
    col.create_index([("created_at", DESCENDING)], background=True)
    col.delete_many({"retro_date": target_date.isoformat()})
    col.insert_one({
        "summary": summary,
        "message_count": msg_count,
        "created_at": datetime(target_date.year, target_date.month, target_date.day, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        "retro_date": target_date.isoformat(),
        "is_retro": True,
    })
    print(f"[mongodb] 保存完了")

def parse_sections(summary: str) -> list[tuple[str, str]]:
    parts = re.split(r"\n##\s+", "\n" + summary)
    sections = []
    for part in parts:
        if not part.strip(): continue
        split = part.strip().split("\n", 1)
        if len(split) > 1: sections.append((split[0].strip(), split[1].strip()))
    return sections

def _split_for_field(text: str, limit: int = 1020) -> list[str]:
    """Discord 1フィールド=1024字制限に収まるよう本文を行境界で分割（切り捨てず全文表示）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(line[:limit]); line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def post_to_discord(summary: str, target_date: date_cls, msg_count: int):
    date_str = target_date.strftime("%Y年%m月%d日")
    sections = parse_sections(summary)

    # 1024字超は「(続きN)」に分割して全文表示
    fields = []
    for title, body in sections:
        icon = next((v for k, v in SECTION_ICONS.items() if k in title), "📋")
        for i, chunk in enumerate(_split_for_field(body, 1020)):
            name = f"{icon} {title}" if i == 0 else f"{icon} {title}（続き{i+1}）"
            fields.append({"name": name[:256], "value": chunk, "inline": False})
    fields.append({"name": "📊 集計", "value": f"対象日: {target_date.isoformat()} / {msg_count:,}件", "inline": False})

    # 6000字/25フィールド制限で複数Embedに分割し、1Embed=1メッセージで投稿（合計6000超の400回避）
    title_str = f"📜 {date_str} の過去日報（遡及作成）"
    groups, cur, chars = [], [], 0
    for f in fields:
        fc = len(f["name"]) + len(f["value"])
        if (chars + fc > 5800 or len(cur) >= 25) and cur:
            groups.append(cur); cur, chars = [], 0
        cur.append(f); chars += fc
    if cur:
        groups.append(cur)

    url = f"https://discord.com/api/v10/channels/{SUMMARY_CHANNEL_ID}/messages"
    for idx, fset in enumerate(groups):
        embed = {
            "title": title_str if idx == 0 else f"{title_str}（続き）",
            "color": 0x8B4513,
            "fields": fset,
        }
        if idx == len(groups) - 1:
            embed["footer"]    = {"text": "空気くん遡及日報"}
            embed["timestamp"] = datetime.now(timezone.utc).isoformat()
        resp = requests.post(url, headers=REST_HEADERS, json={"embeds": [embed]}, timeout=10)
        if resp.status_code not in (200, 201):
            print(f"[ERROR] 投稿失敗: {resp.status_code} {resp.text}")
        time.sleep(0.5)
    print("[post] Discord投稿完了")

def _build_log_text(messages: list[dict]) -> str:
    def _fmt(m: dict) -> str:
        uid = m.get("author_id", "")
        who = f"{m['author']}(uid:{uid})" if uid else m["author"]
        return f"[{m['timestamp'][11:16]}] #{m['channel']} {who}: {m['content']}"
    return "\n".join(_fmt(m) for m in messages)[:40000]


def run_for_date(target_date: date_cls, client_ai: genai.Client, col,
                 skip_existing: bool = False, post: bool = True) -> str:
    """1日分の遡及日報を生成・保存（必要なら投稿）。
    戻り値: "skipped"（既存）/ "empty"（メッセージ無し）/ "done"（生成）。
    backfill から再利用するため client_ai と col は呼び出し側で1回だけ作って渡す。"""
    iso = target_date.isoformat()
    if skip_existing and col.find_one({"retro_date": iso}):
        print(f"[retro] {iso} は既存のためスキップ")
        return "skipped"

    print(f"[retro] 対象日: {iso}")
    messages = fetch_day_logs(target_date)
    if not messages:
        print(f"[retro] {iso} メッセージなし。")
        return "empty"

    log_text = _build_log_text(messages)
    context  = fetch_context_summaries(col, target_date, CONTEXT_DAYS)

    print("[retro] 要約生成中...")
    summary = generate_summary(client_ai, log_text, context)

    save_to_mongodb(col, summary, target_date, len(messages))
    if post:
        post_to_discord(summary, target_date, len(messages))
    return "done"


def main():
    try:
        target_date = date_cls.fromisoformat(TARGET_DATE_STR)
    except ValueError:
        print(f"[ERROR] 日付フォーマット不正: {TARGET_DATE_STR!r}")
        return

    print(f"[debug] Fetching channels for guild {DISCORD_GUILD_ID}...")
    client_ai = genai.Client(api_key=GEMINI_API_KEY)
    mongo     = MongoClient(MONGODB_URI)
    col       = mongo[DB_NAME]["summaries"]

    run_for_date(target_date, client_ai, col, skip_existing=False, post=True)
    print("[retro] 完了！")

if __name__ == "__main__":
    main()
