"""
market_report.py

毎平日: 市場レポート + AIコメント（空気くん人格）をDiscordに投稿
月曜のみ: 先週の予想結果集計 + 今週の予想投票を追加投稿

環境変数:
  DISCORD_WEBHOOK_URL  - 投稿先Webhook
  DISCORD_BOT_TOKEN    - Bot Token（予想メッセージID保存用）
  GEMINI_API_KEY       - AI コメント生成用
  MONGODB_URI          - 予想データ保存用
  SUMMARY_CHANNEL_ID   - 投稿先チャンネルID
"""

import os
import sys
import json
import datetime
import requests
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pymongo import MongoClient
from google import genai
from google.genai import types

# =============================================================================
# 設定
# =============================================================================
SYMBOLS = {
    "nikkei": "^N225",
    "sp500":  "^GSPC",
    "vix":    "^VIX",
    "usdjpy": "JPY=X",
}

WEBHOOK_URL        = os.environ["DISCORD_WEBHOOK_URL"]
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
MONGODB_URI        = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL", "")
SUMMARY_CHANNEL_ID = os.environ.get("SUMMARY_CHANNEL_ID", "")
BOT_TOKEN          = os.environ.get("DISCORD_BOT_TOKEN", "")
MODEL              = "models/gemma-3-27b-it"
MODEL_FB           = "models/gemini-2.5-flash-lite"
MODEL_SEARCH       = "models/gemini-2.5-flash-lite"   # 検索グラウンディング用（2.5系のみ対応）
MODEL_SEARCH_ALT   = "models/gemini-flash-lite-latest" # エイリアス（フォールバック）

JST = datetime.timezone(datetime.timedelta(hours=9))

PREDICTION_XP      = 50   # 予想正解時のXPボーナス
PREDICTION_EMOJI_UP   = "📈"
PREDICTION_EMOJI_DOWN = "📉"


# =============================================================================
# 市場データ取得
# =============================================================================
def get_market_data() -> pd.DataFrame:
    import yfinance as yf
    print("[market] データ取得中...")
    data_dict = {}
    for key, symbol in SYMBOLS.items():
        d = yf.download(symbol, period="1mo", interval="1d", progress=False)
        if not d.empty:
            s = d["Close"].iloc[:, 0] if isinstance(d["Close"], pd.DataFrame) else d["Close"]
            s.index = s.index.date
            data_dict[key] = s
    df = pd.DataFrame(data_dict).sort_index().ffill().dropna()
    if df.empty:
        raise ValueError("データ取得失敗")
    df.index = pd.to_datetime(df.index)
    return df


# =============================================================================
# ダッシュボード画像生成
# =============================================================================
def create_dashboard(df: pd.DataFrame) -> str:
    print("[market] ダッシュボード生成中...")
    plt.style.use("dark_background")
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 12), sharex=True)

    n225_norm  = (df["nikkei"] / df["nikkei"].iloc[0]) * 100
    sp500_norm = (df["sp500"]  / df["sp500"].iloc[0])  * 100
    ax1.plot(df.index, n225_norm,  label="Nikkei 225 (JP)", color="#00ff00", linewidth=2)
    ax1.plot(df.index, sp500_norm, label="S&P 500 (US)",    color="#00bfff", linewidth=2)
    ax1.set_title("Stock Indices Performance (Base=100)", fontsize=14)
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.2)

    ax2.plot(df.index, df["usdjpy"], label="USD/JPY", color="#ff00ff", linewidth=2)
    ax2.set_title(f"FX: USD/JPY ({df['usdjpy'].iloc[-1]:.2f})", fontsize=14)
    ax2.grid(True, alpha=0.2)

    vix = df["vix"]
    ax3.fill_between(df.index, vix, color="red", alpha=0.3)
    ax3.plot(df.index, vix, color="red", label="VIX Index", linewidth=1.5)
    ax3.axhline(y=20, color="yellow", linestyle="--", alpha=0.5, label="Alert(20)")
    ax3.set_title("VIX Index (Market Volatility)", fontsize=14)
    ax3.legend(loc="upper left")
    ax3.grid(True, alpha=0.2)

    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    plt.tight_layout()
    path = "/tmp/dashboard.png"
    plt.savefig(path, dpi=120)
    plt.close()
    return path


# =============================================================================
# AIコメント生成（空気くん人格 + 検索グラウンディング）
# =============================================================================

def _call_with_search(client: genai.Client, prompt: str, max_tokens: int = 300) -> str:
    """検索グラウンディングを使ってGemini 2.5 Flash Liteを呼び出す"""
    for model, label in [(MODEL_SEARCH, "2.5-flash-lite"), (MODEL_SEARCH_ALT, "flash-lite-latest")]:
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.9,
                    max_output_tokens=max_tokens,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            text = getattr(resp, "text", "").strip()
            if text:
                print(f"[market] 検索グラウンディング成功: {label}")
                return text
        except Exception as e:
            print(f"[WARN] search grounding ({label}): {e}")
    return ""


def generate_ai_comment(df: pd.DataFrame, personality: str) -> str:
    last = df.iloc[-1]
    prev = df.iloc[-2]
    diff_n   = last["nikkei"] - prev["nikkei"]
    pct_n    = diff_n / prev["nikkei"] * 100
    diff_sp  = last["sp500"] - prev["sp500"]
    pct_sp   = diff_sp / prev["sp500"] * 100
    vix_val  = last["vix"]
    usdjpy   = last["usdjpy"]
    today    = datetime.datetime.now(JST).strftime("%Y年%m月%d日")

    market_summary = (
        f"日経平均: {last['nikkei']:,.0f}円 ({diff_n:+,.0f} / {pct_n:+.2f}%)\n"
        f"S&P500: {last['sp500']:,.2f} ({diff_sp:+,.2f} / {pct_sp:+.2f}%)\n"
        f"ドル円: {usdjpy:.2f}円\n"
        f"VIX: {vix_val:.2f} ({'⚠️警戒水準' if vix_val > 20 else '✅安定'})"
    )

    personality_examples = {
        "yandere": (
            "ヤンデレなメイド口調で書け。",
            "例:「日経が上がった…でも主人、S&P500が落ちてるんです。"
            "米国が不安定な時に一人でどこかへ行かないでください…VIXも23まで上がってて、"
            "私、怖くて仕方ないんです」"
        ),
        "angry": (
            "常にキレているメイド口調で書け。",
            "例:「はあ？日経だけ55,000円まで上がって何が嬉しいんだ。"
            "S&P500はちゃっかり下落してるじゃないか。"
            "VIXが23超えてる時点で浮かれてんじゃねーよ」"
        ),
        "tsundere": (
            "ツンデレなメイド口調で書け。",
            "例:「べ、別に心配してるわけじゃないけど…日経が上がったのは"
            "GDPが良かったからでしょ。でもVIXが23って…少しくらい気になるじゃない。勘違いしないでよね」"
        ),
        "baka": (
            "天然でズレたメイド口調で書け。だいたい間違った方向で解釈してよい。",
            "例:「日経が55,239円！これってピザ55,239枚買えるってことですよね！"
            "すごいです！VIXが23って…23時のことですか？夜中に市場開くんですね！」"
        ),
        "serious": (
            "有能で真面目なメイド口調で書け。",
            "例:「日経は55,239円と大幅続伸。GDP上振れと円安が追い風となりました。"
            "ただしVIX23超・S&P500続落の外部環境は注視が必要です」"
        ),
        "counselor": (
            "穏やかなカウンセラーメイド口調で書け。",
            "例:「日経が上がりましたね。GDP好調のニュース、少し安心できたでしょうか。"
            "でもVIXが警戒水準にあります。焦らず、長い目で見ていきましょう💙」"
        ),
    }
    p_instruction, p_example = personality_examples.get(
        personality, personality_examples["serious"]
    )

    # 検索グラウンディングで本日の市場ニュースを参照しながらコメント生成
    prompt = f"""{today}の金融市場データ:
{market_summary}

【タスク】
上記データと本日の市場ニュースを検索して調べ、「なぜ今日こう動いたか」を含む市場コメントを書け。

【口調・キャラクター】
{p_instruction}
出力例（口調の参考）: {p_example}

【絶対に守るルール】
- 1文字目から本文を書け。前置き・挨拶・「かしこまりました」等は完全禁止
- プレースホルダー（〇〇・←・【要点】等）禁止。実際の情報のみ
- 検索で確認できた具体的なニュース・数字を必ず1つ以上引用
- 上記の口調例を参考に、キャラクターらしさを最優先
- 300文字以内・コメント本文のみ出力せよ"""

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)

        # まず検索グラウンディングで試みる
        text = _call_with_search(client, prompt, max_tokens=400)
        if text:
            print(f"[market] AIコメント生成（検索グラウンディング）: {len(text)}文字")
            return text

        # フォールバック: 通常モデル
        for model in [MODEL, MODEL_FB]:
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=0.9, max_output_tokens=400),
                )
                text = getattr(resp, "text", "").strip()
                if text:
                    print(f"[market] AIコメント生成（フォールバック {model}）: {len(text)}文字")
                    return text
            except Exception as e:
                print(f"[WARN] AI comment {model}: {e}")
    except Exception as e:
        print(f"[ERROR] AI comment: {e}")
    return "本日の市場データをご確認ください。"


def generate_prediction_hint() -> str:
    """月曜日用: 検索グラウンディングで今週の相場材料を収集"""
    today = datetime.datetime.now(JST).strftime("%Y年%m月%d日")
    prompt = f"""今日は{today}（月曜日）です。
今週の日本・米国株式市場の注目材料を検索して調べ、
実際の情報のみを以下の形式で出力せよ。

【出力ルール・厳守】
- 前置き・導入文は一切禁止。1行目から内容を書け
- プレースホルダー（〇〇など）は絶対に使わない
- 検索で確認できた情報のみ記載。不明な箇所は省略
- 200文字以内

📅 今週の注目材料:
（曜日と具体的な予定を箇条書きで3〜5件）"""

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        text = _call_with_search(client, prompt, max_tokens=300)
        if text:
            print(f"[market] 予想ヒント生成（検索グラウンディング）: {len(text)}文字")
            return text
    except Exception as e:
        print(f"[ERROR] prediction hint: {e}")
    return ""


# =============================================================================
# 日次レポート送信
# =============================================================================
def send_daily_report(df: pd.DataFrame, img_path: str, ai_comment: str):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    diff_n  = last["nikkei"] - prev["nikkei"]
    pct_n   = diff_n / prev["nikkei"] * 100
    vix_val = last["vix"]

    embed_color = 0x2ecc71
    if vix_val > 30:
        embed_color = 0xff0000
    elif vix_val > 20:
        embed_color = 0xf1c40f

    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    payload = {
        "embeds": [{
            "title":  "📊 グローバル市場サマリー",
            "color":  embed_color,
            "fields": [
                {"name": "日経平均",   "value": f"**{last['nikkei']:,.0f}円**\n({diff_n:+,.0f} / {pct_n:+.2f}%)", "inline": True},
                {"name": "S&P 500",   "value": f"**{last['sp500']:,.2f}**", "inline": True},
                {"name": "ドル円",     "value": f"**{last['usdjpy']:.2f}円**", "inline": True},
                {"name": "VIX指数",   "value": f"**{vix_val:.2f}** {'⚠️警戒' if vix_val > 20 else '✅安定'}", "inline": True},
                {"name": "🤖 空気くんのコメント", "value": ai_comment, "inline": False},
            ],
            "footer": {"text": f"Report Date: {today}"},
        }]
    }

    with open(img_path, "rb") as f:
        resp = requests.post(
            WEBHOOK_URL,
            data={"payload_json": json.dumps(payload)},
            files={"file": ("dashboard.png", f)},
            timeout=15,
        )
    print(f"[market] 日次レポート送信: {resp.status_code}")
    return resp


# =============================================================================
# 予想機能（MongoDB）
# =============================================================================
def get_mongo():
    if not MONGODB_URI:
        return None, None
    client = MongoClient(MONGODB_URI)
    db     = client["discord_bot_db"]
    return client, db


def post_prediction_poll(df: pd.DataFrame) -> str | None:
    """月曜日: 今週の予想投票メッセージを送信してメッセージIDを返す"""
    last      = df.iloc[-1]
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")

    # 検索グラウンディングで今週の相場材料を取得
    hint_text = generate_prediction_hint()
    hint_field = f"\n\n{hint_text}" if hint_text else ""

    payload = {
        "embeds": [{
            "title":       "📈📉 今週の日経平均予想！",
            "description": (
                f"今週（{today_str}〜）の日経平均はどう動く？\n\n"
                f"現在値: **{last['nikkei']:,.0f}円**\n\n"
                f"📈 上がると思う人は {PREDICTION_EMOJI_UP} を\n"
                f"📉 下がると思う人は {PREDICTION_EMOJI_DOWN} を\n\n"
                "金曜日に結果を発表するにゃ！正解者には **+50 XP** のボーナスがあるよ！"
                f"{hint_field}"
            ),
            "color": 0x3498DB,
            "footer": {"text": f"予想締切: 今週金曜 / 集計: 自動"},
        }]
    }

    if not SUMMARY_CHANNEL_ID or not BOT_TOKEN:
        # Webhookで送信（メッセージIDは取得できない）
        resp = requests.post(WEBHOOK_URL + "?wait=true", json=payload, timeout=15)
        if resp.status_code in (200, 201):
            msg_id = resp.json().get("id")
            print(f"[market] 予想投票送信: {msg_id}")
            return msg_id
        return None

    # Bot APIで送信（メッセージIDを確実取得）
    url  = f"https://discord.com/api/v10/channels/{SUMMARY_CHANNEL_ID}/messages"
    hdrs = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=hdrs, json=payload, timeout=15)
    if resp.status_code in (200, 201):
        msg_id = resp.json().get("id")
        print(f"[market] 予想投票送信(Bot API): {msg_id}")
        # リアクション追加
        import urllib.parse
        for emoji in [PREDICTION_EMOJI_UP, PREDICTION_EMOJI_DOWN]:
            try:
                enc = urllib.parse.quote(emoji)
                requests.put(
                    f"https://discord.com/api/v10/channels/{SUMMARY_CHANNEL_ID}/messages/{msg_id}/reactions/{enc}/@me",
                    headers={"Authorization": f"Bot {BOT_TOKEN}"},
                    timeout=10,
                )
            except Exception as e:
                print(f"[WARN] リアクション追加失敗 {emoji}: {e}")
        return msg_id
    print(f"[ERROR] 予想投票送信失敗: {resp.status_code} {resp.text[:100]}")
    return None


def save_prediction_message(msg_id: str, nikkei_open: float):
    """MongoDBに予想メッセージIDと基準値を保存"""
    _, db = get_mongo()
    if db is None:
        return
    week_start = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    db["market_predictions"].update_one(
        {"week_start": week_start},
        {"$set": {
            "message_id":   msg_id,
            "nikkei_open":  nikkei_open,
            "channel_id":   SUMMARY_CHANNEL_ID,
            "created_at":   datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }},
        upsert=True,
    )
    print(f"[market] 予想データ保存: week={week_start} msg={msg_id}")


def collect_and_announce_results(df: pd.DataFrame):
    """金曜日: 先週月曜の予想を集計して結果発表 + XP付与"""
    _, db = get_mongo()
    if db is None or not BOT_TOKEN or not SUMMARY_CHANNEL_ID:
        print("[market] MongoDB/Bot設定なし・予想集計スキップ")
        return

    # 直近の予想データを取得（今週月曜分）
    doc = db["market_predictions"].find_one(
        {"channel_id": SUMMARY_CHANNEL_ID},
        sort=[("created_at", -1)],
    )
    if not doc:
        print("[market] 予想データなし・スキップ")
        return

    msg_id      = doc.get("message_id")
    nikkei_open = doc.get("nikkei_open", 0)
    nikkei_now  = df.iloc[-1]["nikkei"]
    diff        = nikkei_now - nikkei_open
    pct         = diff / nikkei_open * 100
    actual_up   = diff >= 0
    correct_emoji = PREDICTION_EMOJI_UP if actual_up else PREDICTION_EMOJI_DOWN

    print(f"[market] 結果: open={nikkei_open:.0f} now={nikkei_now:.0f} diff={diff:+.0f}({pct:+.2f}%)")

    # リアクション集計
    winners = []
    if msg_id and BOT_TOKEN:
        import urllib.parse
        for emoji, is_up in [(PREDICTION_EMOJI_UP, True), (PREDICTION_EMOJI_DOWN, False)]:
            enc  = urllib.parse.quote(emoji)
            url  = f"https://discord.com/api/v10/channels/{SUMMARY_CHANNEL_ID}/messages/{msg_id}/reactions/{enc}?limit=100"
            hdrs = {"Authorization": f"Bot {BOT_TOKEN}"}
            resp = requests.get(url, headers=hdrs, timeout=10)
            if resp.status_code == 200:
                users = resp.json()
                for u in users:
                    if u.get("bot"):
                        continue
                    if is_up == actual_up:
                        winners.append({"id": u["id"], "name": u.get("username", "不明")})

    # XP付与
    if winners and db is not None:
        users_col = db["users"]
        for w in winners:
            users_col.update_one(
                {"_id": w["id"]},
                {"$inc": {"xp": PREDICTION_XP}},
                upsert=False,
            )
        print(f"[market] XP付与: {len(winners)}人 +{PREDICTION_XP}XP")

    # 結果発表メッセージ
    direction   = "上昇📈" if actual_up else "下落📉"
    winner_text = "\n".join(f"・{w['name']}" for w in winners[:10]) or "（なし）"
    if len(winners) > 10:
        winner_text += f"\n…他{len(winners)-10}名"

    payload = {
        "embeds": [{
            "title":       "🏆 今週の日経予想 結果発表！",
            "description": (
                f"今週の日経平均は **{direction}** でした！\n\n"
                f"月曜始値: **{nikkei_open:,.0f}円**\n"
                f"金曜終値: **{nikkei_now:,.0f}円**\n"
                f"変動: **{diff:+,.0f}円 ({pct:+.2f}%)**\n\n"
                f"✅ 正解者（+{PREDICTION_XP}XP付与済み）:\n{winner_text}"
            ),
            "color": 0x2ecc71 if actual_up else 0xe74c3c,
            "footer": {"text": "来週も予想してにゃ！"},
        }]
    }
    url  = f"https://discord.com/api/v10/channels/{SUMMARY_CHANNEL_ID}/messages"
    hdrs = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=hdrs, json=payload, timeout=15)
    print(f"[market] 結果発表送信: {resp.status_code}")


# =============================================================================
# メイン
# =============================================================================
def main():
    now_jst  = datetime.datetime.now(JST)
    weekday  = now_jst.weekday()  # 0=月曜 4=金曜
    is_monday = weekday == 0
    is_friday = weekday == 4

    # 現在の人格をMongoDBから取得
    personality = "serious"
    if MONGODB_URI:
        try:
            _, db = get_mongo()
            if db is not None:
                doc = db["system"].find_one({"_id": "personality"})
                if doc:
                    personality = doc.get("value", "serious")
        except Exception:
            pass
    print(f"[market] 人格: {personality} 曜日: {weekday}")

    df = get_market_data()

    # 金曜: 予想結果集計（先に実行）
    if is_friday:
        print("[market] 金曜: 予想結果集計")
        collect_and_announce_results(df)

    # 毎平日: 日次レポート
    ai_comment = generate_ai_comment(df, personality)
    img_path   = create_dashboard(df)
    send_daily_report(df, img_path, ai_comment)

    # 月曜: 予想投票
    if is_monday:
        print("[market] 月曜: 予想投票投稿")
        msg_id = post_prediction_poll(df)
        if msg_id:
            save_prediction_message(msg_id, float(df.iloc[-1]["nikkei"]))

    print("✅ market_report.py 完了")


if __name__ == "__main__":
    main()
