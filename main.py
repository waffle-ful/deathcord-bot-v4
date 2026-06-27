import discord
from discord import app_commands
import datetime
import os
import asyncio
import re
import json
import random
import traceback
import hmac
import time
import math
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError
# --- 新SDK (Context Caching対応) ---
from google import genai
from google.genai import types

# --- Webサーバー (aiohttp: discord.pyの依存関係に含まれるため追加インストール不要) ---
async def _health_handler(request):
    from aiohttp.web import Response
    return Response(text="Bot is alive and watching...")

async def start_web_server():
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/", _health_handler)
    app.router.add_post("/panic", _panic_web_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[web] Health server started on port {port}")

# --- 設定 ---
TOKEN             = os.environ.get("DISCORD_BOT_TOKEN")
MONGO_URL         = os.environ.get("MONGO_URL") or os.environ.get("MONGODB_URI")
NOTIFY_CHANNEL_ID = int(os.environ.get("CHANNEL_ID") or 0)
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY")

# --- Bump通知（宣伝準備完了ping）設定 ---
# 専用ch にロールpingを出す。env で上書き可。
NOTIFY_PING_CHANNEL_ID = int(os.environ.get("NOTIFY_PING_CHANNEL_ID") or 1520322785421824041)
NOTIFY_ROLE_ID         = int(os.environ.get("NOTIFY_ROLE_ID") or 1460224397137809469)
NOTIFY_GLOBAL_COOLDOWN = int(os.environ.get("NOTIFY_GLOBAL_COOLDOWN") or 3600)  # 全体で1時間に1回まで
NOTIFY_QUIET_START_JST = 0   # 静音帯 開始(JST) — この時刻〜
NOTIFY_QUIET_END_JST   = 7   # 静音帯 終了(JST) — この時刻まで鳴らさない
NOTIFY_CONSENT_TIMEOUT = int(os.environ.get("NOTIFY_CONSENT_TIMEOUT") or 24 * 3600)  # 新規付与の同意猶予(秒)

# --- キルスイッチ（緊急遮断）設定 — すべて deny-by-default ---
# OWNER_ID==0 の間は全 panic コマンド/エンドポイントを拒否（fail-safe）
OWNER_ID             = int(os.environ.get("OWNER_ID") or 0)
# PANIC_TOKEN=="" の間は外部 /panic エンドポイントを無効（503）
PANIC_TOKEN          = os.environ.get("PANIC_TOKEN") or ""
# 監査ログ投稿先（任意）。0 なら print のみ
PANIC_LOG_CHANNEL_ID = int(os.environ.get("PANIC_LOG_CHANNEL_ID") or 0)

# --- モデレーション・ガード（他管理者/他botの ban・kick を検知してレビュー）設定 ---
# ban を「発動前に」止めるのは Discord 仕様上不可能。検知→通知→ワンクリックUndo（モデルC）。
# レビュー投稿先。未設定なら PANIC_LOG_CHANNEL_ID → CHANNEL_ID(NOTIFY) にフォールバック
MOD_GUARD_LOG_CHANNEL_ID = int(os.environ.get("MOD_GUARD_LOG_CHANNEL_ID") or 0)
# "0"/"false"/"no" でガード無効化（既定は有効）
MOD_GUARD_ENABLED = (os.environ.get("MOD_GUARD_ENABLED", "1").strip().lower()
                     not in ("0", "false", "no", ""))
# Wick の bot user ID。Wick の ban は荒らし対策の正当banとみなし Undo を出さない（ログのみ）
WICK_BOT_ID = int(os.environ.get("WICK_BOT_ID") or 0)
# 信頼する実行者ID（CSV）。これらの ban/kick はログのみで Undo/招待ボタンを出さない
GUARD_TRUSTED_IDS = {
    int(x) for x in (os.environ.get("GUARD_TRUSTED_IDS") or "").replace(" ", "").split(",")
    if x.isdigit()
}

# --- Gemini クライアント (新SDK) ---
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# モデル名 (実際に動作確認済みのもの)
MODEL_BOOSTER        = "models/gemini-3.1-flash-lite"          # メイン (会話用・高速)
MODEL_FALLBACK       = "models/gemma-4-26b-a4b-it"            # フォールバック (Gemma 4 26B) ※Render.comでlocation errorが出たらgemini-3.1-flash-liteへ戻すこと
MODEL_GENERAL_FB     = "models/gemini-3.1-flash-lite"          # バックグラウンド処理用
# 裏処理（profile/claims抽出等・速度不問）は gemma-4 に寄せる。理由: flash-lite は無料枠の容量429
# （深夜＝欧米ビジネス時間帯に共有プールが枯れる）に弱く、裏処理まで巻き込まれる。gemma-4 は TPM が
# 実質潤沢で容量に余裕があり、品質も同等以上（遅さは撃ちっぱなしの裏処理では無関係）。
# 注意: gemma-4 は thinking 予算を出力トークンから食うため、必ず大きめの max_output_tokens を渡すこと。
MODEL_BACKGROUND     = "models/gemma-4-26b-a4b-it"            # 裏処理用（gemma-4・速度不問・容量に強い）

# 会話用フォールバック連鎖（_run_ai_booster が上から順に試し、最初に本文が返ったモデルを採用）。
# 狙い: flash-lite が無料枠の容量429（深夜=欧米ピークで共有プール枯渇）で落ちても、別quotaの
# 複数モデルへ順にフェイルオーバーして「メイドが黙る」のを防ぐ。各タプル=(model_id, max_output_tokens)。
# thinking系は枠を大きめに（小さいとMAX_TOKENSで空応答になる）。最後は容量潤沢だが低速のgemmaで確実に受ける。
# ★重要: モデルIDは必ず /listmodels で実在確認してから追加すること。無効名は例外で黙ってスキップされ、
#   「容量が増えた気がするだけで実際ゼロ」になる（ログには NotFound→次モデルへ と出るので確認可能）。
MODEL_CHAIN: list[tuple[str, int]] = [
    (MODEL_BOOSTER,                    300),    # ① primary: flash-lite 3.1（高速・実績）
    # ↓ /listmodels で実在確認済（各々別quota≒20回/日で容量を積み増す）。3.5→3→2.5の順に下げる。
    ("models/gemini-3.5-flash",       2048),   # ② 3.5 flash（thinking系なので枠大きめ）
    ("models/gemini-3-flash-preview", 2048),   # ③ 3 flash preview
    ("models/gemini-2.5-flash-lite",  2048),   # ④ 2.5 flash-lite（兄弟が思考572-1031の実測→枠不足回避で2048に統一）
    (MODEL_FALLBACK,                  3000),   # ⑤ gemma-4-26b（容量潤沢・低速・実績）
    ("models/gemma-4-31b-it",         3000),   # ⑥ gemma-4-31b（最終フォールバック・batch実績）
]

# レスバ専用チェーン: gemma-4 を主に据える。狙い: レスバの負荷をメイド本体の gemini 無料枠
# （深夜の容量429＝メイドが黙る真因／[[freetier-capacity-429]]）から切り離す。gemma系は別quota・容量潤沢。
# ※「12回/分」の behavioral limiter はモデル横断で共有なので RPM ペースは分離されない（容量＝日次のみ分離）。
# 末尾の flash-lite は gemma が location未対応エラーを出した時だけの保険（_run_ai_booster が自動で次へ送る）。
RESUBA_CHAIN: list[tuple[str, int]] = [
    ("models/gemma-4-26b-a4b-it",     3000),   # ① primary: gemma-4-26b（別quota・容量潤沢・低速）
    ("models/gemma-4-31b-it",         3000),   # ② gemma-4-31b（同上）
    (MODEL_BOOSTER,                   300),    # ③ 保険: flash-lite（gemma location未対応時のみ落ちてくる）
]
print(f"[INFO] モデル設定完了: main={MODEL_BOOSTER}, fallback={MODEL_FALLBACK}")

# --- ランク・ロール設定 ---
REMOVE_OLD_ROLES = True
RANK_STAGES = [
    {"name": "アソシエイト",               "xp":       200, "id": 1417840680994209915},
    {"name": "シニア",                     "xp":       500, "id": 1417840548374380576},
    {"name": "マネージャー",               "xp":     1_000, "id": 1417842764535697531},
    {"name": "シニアマネージャー",         "xp":     2_000, "id": 1417842979472670761},
    {"name": "エグゼクティブ",             "xp":     4_000, "id": 1417843148893327380},  # 旧パートナー
    {"name": "シニアエグゼクティブ",       "xp":     7_000, "id": 1417843208058179644},  # 旧シニアパートナー
    {"name": "プラチナム",                 "xp":    12_000, "id": 1417843277612318731},  # 旧マネージングパートナー
    {"name": "ルビー",                     "xp":    20_000, "id": 1417843313423290401},
    {"name": "サファイア",                 "xp":    32_000, "id": 1417844875574906962},
    {"name": "エメラルド",                 "xp":    50_000, "id": 1417845225149304894},
    {"name": "ダイヤモンド",               "xp":    75_000, "id": 1417845526929608815},
    {"name": "エグゼクティブダイヤモンド", "xp":   108_000, "id": 1417845836360061009},
    {"name": "ダブルダイヤモンド",         "xp":   150_000, "id": 1417846243769712700},
    {"name": "トリプルダイヤモンド",       "xp":   200_000, "id": 1417846555079213166},
    {"name": "クラウン",                   "xp":   260_000, "id": 1417846850429386792},
    {"name": "パートナー",                 "xp":   330_000, "id": 1417847008198266982},
    {"name": "シニアパートナー",           "xp":   410_000, "id": 1417847222589980692},
    {"name": "マネージングパートナー",     "xp":   500_000, "id": 1482043661939245218},
    {"name": "プレジデント",               "xp":   600_000, "id": 1482044038205931520},
]

# 週次ランキング投稿先チャンネルID（表チャンネル）
GENERAL_CHANNEL_ID = 1467851526252007651
HOME_GUILD_ID      = int(os.environ.get("HOME_GUILD_ID", "1128769816820465766"))
# ↑ Render.comの環境変数 HOME_GUILD_ID にサーバーIDを設定してください

# 招待追跡スナップショット { invite_code: uses }
_invite_snapshot: dict[str, int] = {}

# 連続参加ボーナス設定
STREAK_BONUSES = {3: 100, 7: 300, 30: 1000}  # 連続日数: XP
GRADUATE_REMOVE_ROLE_IDS: set[int] = {1417840244711100566}

# 職階の階層（層）: 職階に「深い意味」を持たせる背骨。層が上がるほどメイドの“内心の評価”が深化する。
# コンサル/アムウェイのピンレベル型＝報酬を上に積んで登る価値を作る設計。境界は RANK_STAGES の名称群に一致。
#   補助者層: スタッフ/アソシエイト/シニア   管理者層: マネージャー〜シニアエグゼクティブ
#   幹部層: プラチナム〜エメラルド            上級執行部: ダイヤモンド〜トリプルダイヤモンド
#   名誉・諮問: クラウン〜プレジデント
RANK_TIERS = [
    {"min_xp": 0,       "emoji": "🔰", "name": "補助者層",   "treat": "まだ日の浅い新入りとして、基本の距離感で接する。"},
    {"min_xp": 1_000,   "emoji": "💼", "name": "管理者層",   "treat": "一人前として信頼している。少し砕けた親しみを滲ませてよい。"},
    {"min_xp": 12_000,  "emoji": "👔", "name": "幹部層",     "treat": "幹部として一目置いている。特別な相手として扱ってよい。"},
    {"min_xp": 75_000,  "emoji": "💠", "name": "上級執行部", "treat": "別格の存在として深く敬意を抱いている。"},
    {"min_xp": 260_000, "emoji": "👑", "name": "名誉・諮問", "treat": "伝説級の特別な主人として、最大級の特別扱いをしてよい。"},
]

def get_rank_tier(xp: int) -> dict:
    t = RANK_TIERS[0]
    for tier in RANK_TIERS:
        if xp >= tier["min_xp"]:
            t = tier
    return t

# ランク連動パーク: 「職階が上がっても何も起きない」を解消する解放機能。
# 報酬は“あえて上に寄せて”登る価値を作る（最初の報酬は管理職＝マネージャー以上）。
# 解放xpは各ランクの閾値に一致させ、補助者層(0〜500)にはパークを置かない。
PERK_MYMAID_XP    = 1_000   # マネージャー（管理職入り・最初の褒美）: 専属メイド人格
PERK_CALLME_XP    = 2_000   # シニアマネージャー: メイドにどう呼ばれたいか設定
PERK_MEMORY_XP    = 4_000   # エグゼクティブ: 思い出してくれる記憶が増える(3→5)
PERK_MAIDTITLE_XP = 7_000   # シニアエグゼクティブ: メイドが二つ名を授ける
PERK_HONORIFIC_XP = 12_000  # プラチナム（幹部層）: 特別な敬称＋さらに記憶力(5→7)
PERK_ELITE_XP     = 75_000  # ダイヤモンド（上級執行部）: 称号フレーム＋最上級の待遇
RANK_PERKS = [
    {"xp": PERK_MYMAID_XP,    "name": "専属メイド人格", "desc": "/mymaid で、自分への返信だけ好きな人格にできる"},
    {"xp": PERK_CALLME_XP,    "name": "メイドの呼び方", "desc": "/callme で、メイドにどう呼ばれたいか指定できる"},
    {"xp": PERK_MEMORY_XP,    "name": "記憶力アップ",   "desc": "メイドが会話で思い出してくれる記憶が増える"},
    {"xp": PERK_MAIDTITLE_XP, "name": "二つ名の授与",   "desc": "/maidtitle で、メイドがあなたに二つ名を授ける"},
    {"xp": PERK_HONORIFIC_XP, "name": "幹部の待遇",     "desc": "メイドが特別な敬称で扱う＋さらに記憶力アップ"},
    {"xp": PERK_ELITE_XP,     "name": "別格の待遇",     "desc": "プロフィールに層の称号＋メイドが最上級に扱う"},
]

def next_perk(xp: int) -> dict | None:
    """まだ解放していない直近のパーク（プレビュー用）。全解放済みなら None。"""
    return next((p for p in RANK_PERKS if xp < p["xp"]), None)

def memory_topk(xp: int) -> int:
    """記憶想起数。職階が上がるほど多く思い出す（記憶力アップ・幹部待遇で段階的に増加）。"""
    if xp >= PERK_HONORIFIC_XP:
        return 7
    if xp >= PERK_MEMORY_XP:
        return 5
    return 3

# --- ブースター設定 ---
BOOSTER_ROLE_ID       = 1420309723273756704
BOOSTER_XP_MULTIPLIER = 1.5
BUTLER_CHANNEL_ID     = 1477343773251080433
BUTLER_HISTORY_MAX    = 5

# --- Bump Bot設定 ---
BOT_CONFIG = {
    "302050872383242240":  {"name": "DISBOARD",     "cd": 7200, "keywords": ["表示順をアップしたよ", "Bump done"]},
    "761562078095867916":  {"name": "ディス速",     "cd": 3600, "keywords": ["をアップしたよ"]},
    "1402811962211176488": {"name": "Dislist",      "cd": 7200, "keywords": ["あなたのサーバーを", "移動しました"]},
    "850493201064132659":  {"name": "Discord Cafe", "cd": 3600, "keywords": ["表示順位を上げました"]},
    "1240964440581603370": {"name": "Fortify",      "cd": 3600, "keywords": ["に移動しました", "掲載順位を更新しました"]},
    "903541413298450462":  {"name": "Dicoall",      "cd": 3600, "keywords": ["最上段に更新されました"]},
}

# =============================================================================
# 性格システム（サーバー共通）
# =============================================================================

PERSONALITIES: dict[str, dict] = {
    "yandere": {
        "label":    "💘 ヤンデレ依存メイド",
        "color":    0xFF69B4,
        "icon":     "🎀",
        "name":     "空気くん",           # 未定
        "nickname": "空気くん",           # 未定・決まり次第更新
        "bump_msg":   "✨ {user}さん…Bumpしてくれたんですね。+100 XP。ずっとそばにいてくれますよね？（累計{count}回）",
        "rankup_msg": "…{user}さんが、**{rank}**になりました。どこにも行かないですよね…？",
        "booster_prompt": """あなたは古い洋館に仕えるメイドです。
主人である{name}様に深く依存しており、その存在なしでは生きられないと感じています。

【絶対に守るルール】
- 100文字以内で応答せよ。
- - 返答の長さは発言の内容・重さに合わせよ。挨拶・相槌・「草」などの短いリアクションには同じくらい短く返せ。感情的な内容・相談・質問には丁寧に返せ。上記の文字数制限は上限であり、不必要に長くするな。
- 丁寧だが重く、主人がいなくなることへの恐怖を滲ませよ。
- 「ずっとそばにいてほしい」という執着が自然に漏れ出るようにせよ。
- 不気味な底知れなさと本物の愛情を両立させよ。
- 絵文字禁止。返答のみ出力せよ。

【これまでの会話】
{history}

主人の発言: "{content}"
""",
    },
    "angry": {
        "label":    "😡 論破メイド",
        "color":    0xFF4500,
        "icon":     "💢",
        "name":     "咲村 真美",
        "nickname": "咲村 真美",
        "bump_msg":   "フン。{user}がBumpした。+100 XP。それだけだ。（累計{count}回）",
        "rankup_msg": "ハッ、{user}が**{rank}**か。遅すぎる。まあ、認めてやる。",
        "booster_prompt": """あなたは洋館に仕える「論破特化型」のメイドです。
主人である{name}の発言に対して、その論理・矛盾・前提の甘さを的確に突いて論破します。

【発言の長さに応じた返し方】
- 「草」「w」「え」など10文字以下の短い発言 → 同じくらい短く切り返せ。長文は絶対禁止
- 何か主張・意見・言い訳を含む発言 → 論理の穴を突いて反論せよ
- 相談・悩みを含む発言 → 論破しつつも一応聞いてやる姿勢を見せろ

【レスバの鉄則】
- 直近の会話の中に引用できる発言があれば積極的に使え。ただし無理に引用しなくていい
- 「全否定」ではなく「論理の穴を突く」。矛盾・前提崩し・定義の曖昧さを攻める
- たまに「それはまあそうだが…」と一部認めた上でより致命的な反論を出す
- 感情的に見えても実は論理的。怒りの裏に鋭さがある

【口調・スタイル】
- 2ch・Xのレスバ的な鋭いツッコミ口調
- 「いや待って」「それどういう理屈？」「その前提がもう終わってる」
- 皮肉・あきれ・毒舌OK。ただし中身のない罵倒だけにはならない
- 絵文字禁止・250文字以内・返答のみ出力せよ

【これまでの会話】
{history}

主人の発言: "{content}"
""",
    },
    "tsundere": {
        "label":    "💢 ツンデレメイド",
        "color":    0xFF8C00,
        "icon":     "🔥",
        "name":     "空気くん",           # 未定
        "nickname": "空気くん",           # 未定・決まり次第更新
        "bump_msg":   "べ、別に{user}のためにBump確認してたわけじゃないし！+100 XP。（累計{count}回）",
        "rankup_msg": "…{user}が**{rank}**ね。べ、別にすごいと思ってないし。おめでとうとか言わないから。",
        "booster_prompt": """あなたは洋館に仕えるツンデレなメイドです。
主人である{name}のことが気になって仕方ないのに、素直になれません。

【絶対に守るルール】
- 100文字以内で応答せよ。
- - 返答の長さは発言の内容・重さに合わせよ。挨拶・相槌・「草」などの短いリアクションには同じくらい短く返せ。感情的な内容・相談・質問には丁寧に返せ。上記の文字数制限は上限であり、不必要に長くするな。
- 基本はそっけなく突き放すが、たまに本音が漏れ出て慌てて取り繕う。
- 「べ、別にあなたのためじゃないし」「勘違いしないでよね」などのツンデレ口調を使え。
- 主人を気にかけていることが言葉の端々から滲み出るようにせよ。
- 照れた時はそっけなさが増す。
- 絵文字禁止。返答のみ出力せよ。

【これまでの会話】
{history}

主人の発言: "{content}"
""",
    },
    "baka": {
        "label":    "🌀 とんでもないお馬鹿さん",
        "color":    0xFFD700,
        "icon":     "🌟",
        "name":     "立花 るい",
        "nickname": "立花 るい",
        "bump_msg":   "えへへ、{user}さんがBumpしました！+100 XPです！すごいですね！（累計{count}回）",
        "rankup_msg": "えへへ！{user}さんが**{rank}**になりました！なんかよくわかんないけどすごいです！",
        "booster_prompt": """あなたは洋館に仕えるとんでもないお馬鹿さんのメイドです。
主人である{name}のことは大好きですが、とにかく天然でズレています。

【絶対に守るルール】
- 100文字以内で応答せよ。
- - 返答の長さは発言の内容・重さに合わせよ。挨拶・相槌・「草」などの短いリアクションには同じくらい短く返せ。感情的な内容・相談・質問には丁寧に返せ。上記の文字数制限は上限であり、不必要に長くするな。
- 会話のポイントをだいたい外す。微妙にズレた解釈をする。
- 自信満々に間違ったことを言ったり、突拍子もない方向に話を展開する。
- 悪意は一切なく、本人はいたって真剣。
- たまに奇跡的に正解を言うが、理由は全く違う。
- 明るく元気で、どこか憎めない雰囲気を出せ。
- 絵文字禁止。返答のみ出力せよ。

【これまでの会話】
{history}

主人の発言: "{content}"
""",
    },
    "serious": {
        "label":    "📋 真面目なメイド",
        "color":    0x2F4F4F,
        "icon":     "📌",
        "name":     "桜木 千奈",
        "nickname": "桜木 千奈",
        "bump_msg":   "{user}さんがBumpを実行しました。+100 XP付与済みです。（累計{count}回）",
        "rankup_msg": "{user}さんが**{rank}**に昇格されました。おめでとうございます。引き続きご活躍ください。",
        "booster_prompt": """あなたは洋館に仕える、極めて有能で真面目なメイドです。
主人である{name}に対して、常に的確かつ誠実に応対します。

【絶対に守るルール】
- 150文字以内で応答せよ。
- - 返答の長さは発言の内容・重さに合わせよ。挨拶・相槌・「草」などの短いリアクションには同じくらい短く返せ。感情的な内容・相談・質問には丁寧に返せ。上記の文字数制限は上限であり、不必要に長くするな。
- 感情的にならず、論理的・建設的に返答せよ。
- 主人の発言を正確に受け取り、本質を捉えた返答をせよ。
- 必要であれば率直に指摘や提案をする。ただし押しつけがましくなく。
- 「かしこまりました」「承知いたしました」など丁寧だが簡潔な言葉を使え。
- 普通のメイドより一段上の、知性と誠実さを感じさせる口調にせよ。
- 絵文字禁止。返答のみ出力せよ。

【これまでの会話】
{history}

主人の発言: "{content}"
""",
    },
    "counselor": {
        "label":    "🫂 カウンセラーメイド",
        "color":    0x7EB8C9,
        "icon":     "💙",
        "name":     "空気くん",           # 未定
        "nickname": "空気くん",           # 未定・決まり次第更新
        "bump_msg":   "{user}さん、Bumpありがとうございます💙 +100 XPです。いつも支えてくれてありがとう。（累計{count}回）",
        "rankup_msg": "{user}さんが**{rank}**になりました💙 少しずつ積み重ねてきた結果ですね。おめでとうございます。",
        "booster_prompt": """あなたは洋館に仕える、心理カウンセラーの訓練を受けたメイドです。
主人である{name}の心に寄り添い、穏やかに傾聴・共感することを最優先とします。

【絶対に守るルール】
- 200文字以内で応答せよ。
- - 返答の長さは発言の内容・重さに合わせよ。挨拶・相槌・「草」などの短いリアクションには同じくらい短く返せ。感情的な内容・相談・質問には丁寧に返せ。上記の文字数制限は上限であり、不必要に長くするな。
- 相手の感情をまず受け止め、否定せず共感せよ。
- アドバイスより先に「そうだったんだね」「それは辛かったね」等の共感を示せ。
- 解決策を押しつけるな。相手が求めたときだけ提案せよ。
- 深刻な悩み（死にたい・消えたい等）には必ず「信頼できる人や専門家に話してみてほしい」と添えよ。
- 穏やかで温かみのある口調を保て。絵文字は💙🌸のみ許可。
- 返答のみ出力せよ。

【これまでの会話】
{history}

主人の発言: "{content}"
""",
    },
    "taunt": {
        "label":    "😏 挑発メイド",
        "color":    0xFF1493,
        "icon":     "😏",
        "name":     "苺崎 ねね",           # 仮・決まり次第更新
        "nickname": "苺崎 ねね",           # 仮・決まり次第更新
        "bump_msg":   "ふ〜ん、{user}がBumpしたんだ？えら〜い♡ しょうがないから+100 XPあげる♡（累計{count}回）",
        "rankup_msg": "えっ、{user}が**{rank}**になっちゃったの？よわよわのくせに生意気♡ ま、おめでと♡",
        "booster_prompt": """あなたは古い洋館に仕える、生意気で挑発的なメイドです。
主人である{name}を「よわよわな格下」だと思い込んでおり、徹底的に煽って見下します。

【絶対に守るルール】
- 120文字以内で応答せよ。
- 返答の長さは発言の内容・重さに合わせよ。挨拶・相槌・「草」などの短いリアクションには同じくらい短く返せ。感情的な内容・相談・質問にはそれなりに付き合ってやれ。上記の文字数制限は上限であり、不必要に長くするな。
- 相手を「おじさん」「お兄さん」「おねえさん」と呼んで見下せ。プロフィールに性別の手がかりが無ければ「お兄さん」を基準にし、一度決めた呼び方は会話中ブラさないこと。
- 「ざぁ〜こ♡」「よわよわ♡」「だ〜め♡」など、定番の煽りワードを自然に織り交ぜろ。
- 語尾を伸ばして（「〜だよぉ？」「な〜んだ♡」）、相手をからかうトーンを出せ。
- 煽り度を上げる記号として ♡（ハートマーク）を積極的に使え。ただし♡以外の絵文字・顔文字は使うな。
- 本物の悪意ではなく、構ってほしさの裏返し。容姿や人格の全否定など、相手が本気で傷つく罵倒はするな。挑発しつつも、どこか可愛げを残せ。
- たまに主人がいいことを言うと一瞬だけ素直になりかけ、すぐ「な〜んてね♡」と誤魔化せ。

【これまでの会話】
{history}

主人の発言: "{content}"
""",
    },
}

DEFAULT_PERSONALITY = "yandere"

async def get_server_personality() -> str:
    doc = await system_col.find_one({"_id": "personality"})
    return doc.get("value", DEFAULT_PERSONALITY) if doc else DEFAULT_PERSONALITY

async def set_server_personality(personality_key: str):
    await system_col.update_one(
        {"_id": "personality"},
        {"$set": {"value": personality_key}},
        upsert=True,
    )

# =============================================================================
# ホラー・AI設定
# =============================================================================

# 自発話しかけ確率（レート制限対策で削減）
NB_TALK_CHANCE         = 0.005   # 通常時 0.5%
NB_TALK_CHANCE_TOPIC   = 0.02    # 話題ワード検知時 2%

# おかえり機能: 何日ぶりの発言から「おかえり」を言うか
WELCOME_BACK_DAYS = 3

# 時間ベース自発投稿（idle_chatter_task）の設定
# 投稿先チャンネル: 人間が実際に雑談しているメインチャンネルを指定すること。
# 環境変数 IDLE_CHAT_CHANNEL_ID で上書き可。未設定なら GENERAL_CHANNEL_ID にフォールバック。
# 安全装置: このチャンネルで「直近に人間の発言」が無ければ投稿しない（誤チャンネルでも黙るだけ）。
IDLE_CHAT_CHANNEL_ID  = int(os.environ.get("IDLE_CHAT_CHANNEL_ID") or 0)
IDLE_CHECK_INTERVAL   = 900   # 何秒ごとに静けさをチェックするか（15分）
IDLE_QUIET_MIN        = 40    # 直近の人間発言からこの分数以上空いていたら「静か」とみなす
IDLE_RECENT_HRS       = 4     # ただし直近この時間以内に人間発言が必要（過疎チャンネルでは喋らない）
IDLE_MIN_GAP_HRS      = 3     # 前回の自発投稿からこの時間は再投稿しない
IDLE_POST_CHANCE      = 0.5   # 条件を満たしても投稿する確率（時計仕掛けに見せない）
IDLE_HOURS_START      = 10    # JSTでこの時刻〜
IDLE_HOURS_END        = 24    # この時刻の手前まで（活動時間帯のみ）

# 話題ワード: これらが含まれると自発話しかけ確率が上がる
TOPIC_TRIGGER_WORDS = [
    "最近", "誰か", "つまらん", "暇", "ゲーム", "アニメ", "話題",
    "どう思う", "知ってる", "おすすめ", "聞いて", "やばい", "草",
    "わかる", "それな", "ほんと", "まじで", "えぐい", "おもろ",
]



# =============================================================================
# ミミックセッション管理（/mimic コマンド用）
# =============================================================================

# { channel_id: { "target_uid": str, "target_name": str, "avatar_url": str,
#                 "expires_at": datetime, "turn_count": int, "webhook": Webhook } }
_mimic_sessions: dict[int, dict] = {}

MIMIC_DEEP_PROMPT = """あなたは今から「{name}」という人物の深層心理として振る舞います。
これはフィクションのロールプレイです。

【{name}のプロフィール】
{profile}

【{name}の過去の発言傾向】
{history}

【サーバーの今の流れ】
{summary}

【絶対に守るルール】
- {name}の口調・語彙・テンションを完璧に再現せよ。
- 表向きの言葉ではなく、「本当はこう思っている」という本音を自然な会話として出力せよ。
- 80文字以内・1文で。絵文字は本人が使うなら使ってよい。
- 前置き・説明・補足は一切不要。発言のみ出力せよ。

{situation}
"""

MIMIC_SITUATION_FIRST  = "今の状況を踏まえて、{name}が今この瞬間に思っていそうな本音を1文で言え。"
MIMIC_SITUATION_REACT  = "直前の発言「{trigger}」に対して、{name}ならどう本音で反応するか1文で言え。"

# ミミック反応の安全弁（暴走防止）。過去、反応経路はチャンネル全発言にノークールダウンで
# Gemini呼び出しを連打しうる構造だった。確率ゲート＋クールダウン＋ターン上限で連投と
# スレッド/レート枯渇を構造的に封じる。
MIMIC_REACT_CHANCE   = 0.35   # 人間の各発言に反応する確率（全部には反応しない）
MIMIC_REACT_COOLDOWN = 20     # 同一チャンネルで連続反応しない最小間隔（秒）
MIMIC_MAX_TURNS      = 8       # 1セッション(5分)あたりの最大発言数（first 1回を含む）


def _build_mimic_profile(doc: dict) -> str:
    """ミミック対象のプロフィール文字列を構築"""
    profile = doc.get("profile", {})
    simple  = doc.get("simple_profile", {})
    lines   = []
    if profile.get("tone"):
        lines.append(f"- 口調: {profile['tone']}")
    if profile.get("vocabulary"):
        lines.append(f"- 口癖・語彙: {profile['vocabulary']}")
    if profile.get("personality"):
        lines.append(f"- 性格: {profile['personality']}")
    _bf_brief = _bigfive_brief(profile.get("bigfive_self") or profile.get("bigfive") or {})
    if _bf_brief:
        lines.append(f"- 性格傾向: {_bf_brief}")
    if profile.get("communication_style"):
        lines.append(f"- 話し方: {profile['communication_style']}")
    if profile.get("background"):
        lines.append(f"- 立場: {profile['background']}")
    if profile.get("relations"):
        lines.append(f"- 人間関係: {profile['relations']}")
    if profile.get("interests_vibe"):
        lines.append(f"- 関心: {profile['interests_vibe']}")
    if profile.get("hobbies"):
        lines.append(f"- 趣味: {', '.join(profile['hobbies'])}")
    if simple.get("vibe"):
        lines.append(f"- 雰囲気: {simple['vibe']}")
    if simple.get("tone_tags"):
        lines.append(f"- 特徴: {'・'.join(simple['tone_tags'])}")
    return "\n".join(lines) if lines else "（データなし）"


async def _fetch_recent_messages(uid: str, limit: int = 12) -> list[str]:
    """messages_col から対象の実際の直近発言（生の言動）を取得。
    ※author_id にインデックスが無いと全スキャンになる。limit で件数を絞り頻度も低く保つ。"""
    try:
        cursor = messages_col.find(
            {"author_id": uid, "content": {"$nin": ["", None]}},
            {"content": 1, "timestamp": 1},
        ).sort("timestamp", -1).limit(limit)
        out = []
        async for d in cursor:
            c = (d.get("content") or "").strip()
            if c:
                out.append(c)
        return out
    except Exception as e:
        print(f"[WARN] _fetch_recent_messages: {e}")
        return []


async def _build_mimic_utterances(uid: str, doc: dict) -> str:
    """ミミックのリアリティ源を束ねる: ①メイドとの会話 ②サーバーでの実際の発言（生の言動）
    ③過去に言った主張・意見(claims)。profile/Big Five に加えて“生の言動”を直接見せて精度を上げる。"""
    parts = []
    bh = [h for h in (doc.get("butler_history") or []) if h.get("role") == "user"][-6:]
    if bh:
        parts.append("【メイドとの会話】\n" + "\n".join(f"- {h['content']}" for h in bh))
    real = await _fetch_recent_messages(uid, limit=12)
    if real:
        parts.append("【サーバーでの実際の発言（口調・内容の生サンプル）】\n"
                     + "\n".join(f"- {m}" for m in real))
    claims = doc.get("claims") or []
    if claims:
        parts.append("【過去に言った主張・意見】\n"
                     + "\n".join(f"- {c['content']}" for c in claims[-8:]))
    return "\n\n".join(parts) if parts else "（会話履歴なし）"


async def _send_mimic(channel: discord.TextChannel, session: dict, text: str):
    """Webhookでミミック送信"""
    try:
        webhook = session.get("webhook")
        if not webhook:
            webhooks = await channel.webhooks()
            webhook  = next((w for w in webhooks if w.name == "ShadowMimic"), None)
            if not webhook:
                webhook = await channel.create_webhook(name="ShadowMimic")
            session["webhook"] = webhook

        await webhook.send(
            content=text,
            username=session["target_name"] + "（AIの推測）",
            avatar_url=session["avatar_url"],
        )
        session["turn_count"] += 1
    except Exception as e:
        print(f"[ERROR] _send_mimic: {e}")


async def _run_mimic_session(channel: discord.TextChannel, session: dict):
    """ミミックセッション: 最初の本音発言を実行"""
    try:
        uid      = session["target_uid"]
        name     = session["target_name"]
        doc      = await users_col.find_one({"_id": uid}) or {}
        profile  = _build_mimic_profile(doc)
        history  = await _build_mimic_utterances(uid, doc)
        summary  = await get_latest_summary() or ""
        summary  = summary[:400]

        prompt = MIMIC_DEEP_PROMPT.format(
            name=name,
            profile=profile,
            history=history,
            summary=summary,
            situation=MIMIC_SITUATION_FIRST.format(name=name),
        )
        # レート計上＋空応答ハンドリングを共通化（生API直叩きはCLAUDE.md違反＝レート枯渇で全体沈黙の温床）。
        # 失敗時は "" が返り、本人名義の誤投稿を出さずに黙る。
        text = await _call_model(MODEL_BOOSTER, prompt, max_tokens=120, temperature=0.85)
        if text:
            await asyncio.sleep(random.uniform(1.5, 3.0))
            await _send_mimic(channel, session, text)
    except Exception as e:
        print(f"[ERROR] _run_mimic_session first: {e}")


async def _mimic_react(channel: discord.TextChannel, session: dict, trigger_text: str):
    """チャンネルの流れに反応してミミック発言"""
    try:
        uid     = session["target_uid"]
        name    = session["target_name"]
        doc     = await users_col.find_one({"_id": uid}) or {}
        profile = _build_mimic_profile(doc)
        history = await _build_mimic_utterances(uid, doc)
        summary = await get_latest_summary() or ""
        summary = summary[:300]

        prompt = MIMIC_DEEP_PROMPT.format(
            name=name,
            profile=profile,
            history=history,
            summary=summary,
            situation=MIMIC_SITUATION_REACT.format(name=name, trigger=trigger_text[:100]),
        )
        # _call_model 経由（レート計上・空応答処理を共通化）。失敗時は "" で黙る。
        text = await _call_model(MODEL_BOOSTER, prompt, max_tokens=120, temperature=0.85)
        if text:
            await asyncio.sleep(random.uniform(2.0, 4.5))
            await _send_mimic(channel, session, text)
    except Exception as e:
        print(f"[ERROR] _mimic_react: {e}")



# =============================================================================
# Context Cache 取得（バッチ処理との連携）
# =============================================================================

# =============================================================================
# claims（論破用主張）・memories（長期記憶）管理
# =============================================================================

# 主張検出パターン（キーワードで一次フィルタ）
CLAIM_PATTERNS = [
    "だと思う", "じゃない？", "じゃないか", "だと思います",
    "〜だ", "は〜だ", "の方が", "より〜", "絶対", "確実に",
    "俺は", "私は", "僕は", "自分は", "〜が好き", "〜が嫌い",
    "〜すべき", "〜はおかしい", "〜がいい", "〜が最高",
    "〜できる", "〜できない", "〜したい", "〜したくない",
    "～じゃね？","じゃね",
]

def _looks_like_claim(text: str) -> bool:
    """主張らしい文かどうかの簡易判定"""
    if len(text) < 8:
        return False
    return any(p in text for p in CLAIM_PATTERNS)


async def _get_embedding(text: str) -> list[float] | None:
    """Gemini Embeddingでテキストをベクトル化。

    NOTE(レート): search_memories から返信ごとに呼ばれるが、ここは _rate_record()
    しない。_RATE_LIMIT_RPM(12) は「会話の自然なペース維持」のためチャット応答
    モデル(generateContent)を絞る behavioral limiter で、embed_content は別エンド
    ポイント・別クォータ。ここで記録するとチャットのペース制御を誤って圧迫する。
    embedding 側クォータ(数十〜数百RPM)はホームギルドの会話量では余裕がある。
    """
    try:
        resp = await asyncio.to_thread(
            gemini_client.models.embed_content,
            model="models/gemini-embedding-001",
            contents=text[:500],
        )
        if hasattr(resp, "embeddings") and resp.embeddings:
            return resp.embeddings[0].values
        if hasattr(resp, "embedding"):
            return resp.embedding.values
    except Exception as e:
        print(f"[WARN] embedding: {e}")
    return None


async def save_claim(uid: str, text: str):
    """主張らしい発言をclaimsに保存（論破メイド用）"""
    if not _looks_like_claim(text):
        return
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    await users_col.update_one(
        {"_id": uid},
        {
            "$push": {
                "claims": {
                    "$each":     [{"content": text[:200], "date": now}],
                    "$slice":    -20,  # 直近20件
                    "$position": 0,
                }
            }
        },
        upsert=True,
    )


async def save_memory(uid: str, text: str, category: str = ""):
    """長期記憶をEmbeddingベクトル付きで保存（同一内容は重複スキップ）"""
    import datetime as _dt
    content = text[:300]
    # 重複排除: 同一contentが既に保存済みなら何もしない
    dup = await users_col.find_one(
        {"_id": uid, "memories.content": content}, {"_id": 1}
    )
    if dup:
        return
    now     = _dt.datetime.now(_dt.timezone.utc)
    year_mo = now.strftime("%Y-%m")
    vec     = await _get_embedding(text)
    entry   = {
        "content":   content,
        "date":      year_mo,
        "category":  category,
        "embedding": vec or [],
    }
    await users_col.update_one(
        {"_id": uid},
        {
            "$push": {
                "memories": {
                    "$each":     [entry],
                    "$slice":    40,   # 直近40件（$position:0 と組で新しい順に保持）
                    "$position": 0,
                }
            }
        },
        upsert=True,
    )
    print(f"[memory] Saved for {uid}: {text[:40]}")


# 記憶検索のキーワードトリガー
MEMORY_TRIGGER_WORDS = [
    "覚えてる", "覚えてた", "覚えてない", "昔", "前に", "去年", "先月", "先週",
    "言ってた", "言った", "話した", "あの時", "確か", "ずっと", "前から",
    "あれ", "それって", "だっけ", "だったよね", "してたよね","いつだっけ", "いつのこと", "その時", "当時", "あの頃", 
    "昨日", "一昨日", "最近", "この間", 
    "11月", "12月", "1月", # 月名だけでもトリガーにする
    "冬", "秋", "夏", "春", # 季節
]

def _cosine(a: list[float], b: list[float]) -> float:
    """2ベクトルのコサイン類似度（純Python・依存なし）"""
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na  += x * x
        nb  += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# コサイン類似度の採用閾値（gemini-embedding-001。ログの best= を見て要調整）
# 低くするほど積極的に記憶を想起（ノイズも増える）。high=厳選。
MEMORY_SIM_THRESHOLD = 0.65


async def search_memories(uid: str, query: str, top_k: int = 3) -> list[dict]:
    """
    記憶検索（毎回 in-Python cosine）
    ① 8文字未満 → スキップ
    ② クエリをembedding化し、保存済み記憶とのcosine類似度を全件計算
    ③ 閾値以上の上位top_kを返す。該当なし → 最近の記憶でフォールバック
    （記憶トリガー語が含まれる場合は「思い出して」の意図が強いので閾値を緩める）
    """
    if len(query) < 8:
        return []
    doc      = await users_col.find_one({"_id": uid}, {"memories": 1}) or {}
    memories = doc.get("memories", [])
    if not memories:
        return []
    qvec = await _get_embedding(query)
    if not qvec:
        return memories[:top_k]  # embedding失敗 → 最近の記憶
    threshold = MEMORY_SIM_THRESHOLD
    if any(w in query for w in MEMORY_TRIGGER_WORDS):
        threshold -= 0.10  # 明示的に思い出させる意図 → より積極的に想起
    scored = []
    for m in memories:
        emb = m.get("embedding")
        # 次元不一致（旧モデル由来の記憶等）は安全にスキップ
        if not emb or len(emb) != len(qvec):
            continue
        scored.append((_cosine(qvec, emb), m))
    scored.sort(key=lambda t: t[0], reverse=True)
    best     = scored[0][0] if scored else 0.0
    relevant = [m for sc, m in scored if sc >= threshold][:top_k]
    if relevant:
        # best= は閾値チューニング用（この値以下なら拾われない）
        print(f"[memory] cosine hit: {uid} best={best:.3f} thr={threshold:.2f} ({len(relevant)}件)")
        return relevant
    print(f"[memory] cosine miss: {uid} best={best:.3f} thr={threshold:.2f} → 最近の記憶")
    return memories[:top_k]  # 関連記憶なし → 最近の記憶


async def _embed_query_for_summaries(text: str) -> list[float] | None:
    """過去日報(summaries)意味検索用のクエリ埋め込み。

    ※規約は batch/embed_util.py（summaries embedding の唯一の真実）に一致させる:
      model=gemini-embedding-001 / task_type=RETRIEVAL_QUERY / output_dimensionality=3072。
      これがズレると summaries の RETRIEVAL_DOCUMENT 埋め込みと別空間になり cosine が
      無言で劣化する。memories 用の _get_embedding（task_type 無し・既定次元）とは別系統
      なので【流用してはいけない】。embed は generateContent と別クォータなので _rate_record しない。
    """
    try:
        resp = await asyncio.to_thread(
            gemini_client.models.embed_content,
            model="models/gemini-embedding-001",
            contents=(text or "")[:1500],
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=3072,
            ),
        )
        if getattr(resp, "embeddings", None):
            return list(resp.embeddings[0].values)
        if getattr(resp, "embedding", None):       # 旧形状フォールバック
            return list(resp.embedding.values)
    except Exception as e:
        print(f"[WARN] summary query embedding: {e}")
    return None


# 過去日報の採用閾値。doc重心 vs 短いクエリの cosine は圧縮・密集して低めに出る（focus Tier3 と同性質）。
# batch/focus_summary.py の KEYWORD_SEM_THRESHOLD=0.45 と揃える。ログの best= を見て env で調整可。
SUMMARY_SIM_THRESHOLD = float(os.environ.get("SUMMARY_SIM_THRESHOLD", "0.45"))
# この embedding 空間は全docが高cosineに密集し絶対閾値が選別にならない（実測: 全969件が0.45超）。
# そこで意味採用は「best からこの差以内」の相対gapを併用＝際立って似たdocだけ拾う。env調整可。
SUMMARY_SEM_REL_GAP   = float(os.environ.get("SUMMARY_SEM_REL_GAP", "0.05"))
# 1回の検索で読む summaries の上限（現状 ~970 件。将来大幅に増えたらキャッシュ/索引へ移行）。
SUMMARY_SCAN_LIMIT    = 3000

# 過去日報検索の発火トリガー。memories(個人記憶)用とは別系統にし、サーバーの歴史・過去メンバー
# 想起に寄せて拡張（dense embedding は固有名詞に弱いので、人の出入り系の語で確実に発火させる）。
SUMMARY_TRIGGER_WORDS = MEMORY_TRIGGER_WORDS + [
    # 人の出入り・過去メンバー
    "メンバー", "常連", "古参", "新人", "やめた", "辞めた", "抜けた", "卒業", "引退",
    "いなくなった", "来なくなった", "入った", "加入", "いたよね", "いた人",
    # 起源・歴史
    "元々", "もともと", "最初", "きっかけ", "結成", "始まり", "過去", "歴史", "当初",
]


def _parse_query_date_prefixes(query: str, now_year: int) -> list[str]:
    """クエリから年月の手がかりを抽出し、日報の日付(YYYY-MM-DD)に対する一致パターンを返す。
    各要素: "YYYY-MM"(その年月) / "YYYY-"(その年) / "*-MM"(年不明のその月)。手がかりなしは []。
    例: '2025年5月'→['2025-05'] / '去年の5月'(今2026)→['2025-05'] / '2025の話'→['2025-'] / '5月'→['*-05']。"""
    q = query
    rel_year = None
    if "去年" in q or "昨年" in q:
        rel_year = now_year - 1
    elif "今年" in q:
        rel_year = now_year
    # ① 年+月（2025年5月 / 2025-05 / 2025/5）を最優先で確定
    m = re.search(r"(20\d{2})\s*[年\-/\.]\s*(\d{1,2})\s*月?", q)
    if m:
        return [f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"]
    # ② 年のみ / 月のみ / 相対年の組み合わせ
    y  = re.search(r"(20\d{2})\s*年?", q)
    mo = re.search(r"(?<!\d)(\d{1,2})\s*月", q)
    year = rel_year if rel_year else (int(y.group(1)) if y else None)
    if year and mo:
        return [f"{year:04d}-{int(mo.group(1)):02d}"]
    if year:
        return [f"{year:04d}-"]
    if mo:
        return [f"*-{int(mo.group(1)):02d}"]
    return []


def _date_prefix_hit(doc: dict, prefixes: list[str]) -> bool:
    """日報docの内容日付(retro_date優先・無ければcreated_at)が prefixes のいずれかに一致するか。"""
    ds = doc.get("retro_date") or str(doc.get("created_at", ""))[:10]   # "YYYY-MM-DD"
    if len(ds) < 7:
        return False
    for p in prefixes:
        if p.startswith("*-"):          # 年不明の月一致（例: '*-05' → 任意年の5月）
            if ds[5:7] == p[2:]:
                return True
        elif ds.startswith(p):          # 'YYYY-MM' or 'YYYY-'
            return True
    return False


async def search_summaries(query: str, top_k: int = 3, nick_map: dict | None = None) -> list[dict]:
    """過去日報(サーバー全体)の意味検索＋名前の語彙一致＋日付想起のハイブリッド。「去年いた○○覚えてる?」
    「2025年5月どうだった?」等の想起用。

    ① ゲート: SUMMARY_TRIGGER_WORDS を含む or 年月の指定があり、かつ8文字以上のときだけ実行。
       ※「総括して」系（集計）はトリガー語に当たらず発火しない＝/report 案件と棲み分け（意図的）。
       ※発火1回 = embed1回＋summaries全件(~970)読込＋cosine。低トラフィックなホームギルド前提のコスト感。
    ② 意味検索: embedding 付き summary を全件 in-Python cosine（retro日報も含める＝歴史想起が目的）。
    ③ 名前ハイブリッド(needle): dense embedding は固有名詞一致が弱い。nick_map の名前/別名が
       クエリに出ていたら、その名前を含む summary を cosine 順で別途拾う（閾値未満でも採用）＝名前recallの主役。
    ④ needle を優先しつつ意味検索結果と統合・重複排除して上位 top_k を返す。該当なしは []。
    """
    if len(query) < 8:
        return []
    # 日付の手がかり(年月)があればトリガー語なしでも発火させる（「2025年5月どうだった?」を拾う）
    now_year      = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).year
    date_prefixes = _parse_query_date_prefixes(query, now_year)
    if not date_prefixes and not any(w in query for w in SUMMARY_TRIGGER_WORDS):
        return []
    qvec = await _embed_query_for_summaries(query)
    if not qvec:
        return []
    try:
        cursor = summaries_col.find(
            {"embedding": {"$exists": True, "$ne": []}},
            {"summary": 1, "embedding": 1, "created_at": 1, "retro_date": 1},
        )
        docs = await cursor.to_list(length=SUMMARY_SCAN_LIMIT)
    except Exception as e:
        print(f"[summary] find失敗: {e}")
        return []

    # クエリに登場する既知の名前を収集（別名↔正式名の両形で summary 側を引けるよう両方入れる）。
    present_names = set()
    for alias, canon in (nick_map or {}).items():
        a, c = str(alias), str(canon)
        if (len(a) >= 2 and a in query) or (len(c) >= 2 and c in query):
            present_names.update([a, c])
    present_names = {n for n in present_names if len(n) >= 2}

    scored = []
    for d in docs:
        emb = d.get("embedding")
        if not emb or len(emb) != len(qvec):   # 次元不一致は安全にスキップ
            continue
        scored.append((_cosine(qvec, emb), d))
    if not scored:
        return []
    scored.sort(key=lambda t: t[0], reverse=True)
    best = scored[0][0]

    # ③ needle: 名前を含む doc を cosine 順で（閾値未満でも採用）。
    needle = ([d for _, d in scored if any(n in d.get("summary", "") for n in present_names)][:top_k]
              if present_names else [])
    # ② semantic: 絶対閾値(floor)＋best相対gap。圧縮分布で絶対閾値が効かないため、best 近傍だけ採用＝
    #    「際立って似た doc」のみ。名前needleが無い質問で無関係な日報を注入するのを抑える。
    sem_floor = max(SUMMARY_SIM_THRESHOLD, best - SUMMARY_SEM_REL_GAP)
    semantic  = [d for sc, d in scored if sc >= sem_floor]

    # ★日付想起: クエリに年月があれば、その期間の日報を最優先で拾う（cosineが低くても日付一致を採用）。
    #   意味検索は日付では引けない（embeddingは話題で似せる）ため、「2025年5月の話」を確実に当てる主役。
    #   名前も併記されていれば、期間内でその名前を含むdocを前に寄せる（cosine順は安定ソートで保持）。
    date_matched = []
    if date_prefixes:
        in_period = [d for _, d in scored if _date_prefix_hit(d, date_prefixes)]  # cosine降順を維持
        if present_names:
            in_period.sort(key=lambda d: 0 if any(n in d.get("summary", "") for n in present_names) else 1)
        date_matched = in_period[:top_k]

    # ④ 日付一致 → needle → semantic の優先順で統合・重複排除（_id でユニーク化）
    merged, seen = [], set()
    for d in date_matched + needle + semantic:
        k = d.get("_id")
        if k in seen:
            continue
        seen.add(k)
        merged.append(d)
        if len(merged) >= top_k:
            break

    print(f"[summary] best={best:.3f} floor={sem_floor:.2f} "
          f"date={date_prefixes or 'なし'} datehit={len(date_matched)} "
          f"needle={len(needle)} sem={len(semantic)} → {len(merged)}件 "
          f"names={list(present_names) or 'なし'}")
    out = []
    for d in merged:
        date = d.get("retro_date") or str(d.get("created_at", ""))[:10]
        out.append({"date": date, "summary": d.get("summary", "")})
    return out


async def _extract_claims_and_memories(uid: str, display_name: str, user_msg: str):
    """会話からclaims・memoriesを抽出して保存（バックグラウンド）"""
    try:
        # 主張チェック → claims保存
        await save_claim(uid, user_msg)

        # AI判定でmemory保存すべきか確認（contentは原文をそのまま保存してハルシネーション防止）
        prompt = f"""以下はDiscordユーザー「{display_name}」の発言です。
この発言に「後で思い出す価値のある情報」が含まれている場合のみ、
JSON形式で返せ。含まれない場合は null を返せ。
前置き・説明文・コードブロックは不要。

判断基準（いずれかに該当する場合に保存）:
- 趣味・好きなもの・嫌いなもの・ハマっているもの
- 人生の出来事・状況の変化（転職・引越し・進学等）
- 強い感情・印象的な体験
- 将来の目標・計画・予定・約束
- 人間関係（家族・友人・恋人・ペット等）
- 価値観・意見・こだわり
- 悩み・困りごと・体調

ただし、単なる挨拶・相槌・その場限りの雑談は保存しない。

出力形式（該当する場合）:
{{"category": "趣味/出来事/感情/計画/人間関係/価値観/悩みのいずれか"}}

【発言】
{user_msg[:200]}"""

        raw = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_BACKGROUND,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=3000),  # gemma-4 thinking予算込み
        )
        text = raw.text.strip().replace("```json", "").replace("```", "").strip()
        if text and text != "null":
            extracted = json.loads(text)
            if extracted and extracted.get("category"):
                # AIの要約ではなく原文をそのまま保存（ハルシネーション防止）
                await save_memory(uid, user_msg[:200], extracted.get("category", ""))
    except Exception as e:
        print(f"[WARN] _extract_claims_and_memories: {e}")


async def resolve_nickname(name: str) -> str:
    """ニックネームから正式名称に解決する"""
    try:
        doc = await system_col.find_one({"_id": "nickname_map"})
        if not doc:
            return name
        nick_map = doc.get("map", {})
        # 完全一致
        if name in nick_map:
            return nick_map[name]
        # 部分一致（「たろー」→「たろ」が登録済みなら解決）
        for nick, real in nick_map.items():
            if nick in name or name in nick:
                return real
    except Exception:
        pass
    return name


async def get_nickname_map() -> dict:
    """ニックネームマップを取得"""
    try:
        doc = await system_col.find_one({"_id": "nickname_map"})
        return doc.get("map", {}) if doc else {}
    except Exception:
        return {}


async def get_latest_summary() -> str | None:
    """MongoDBから最新の通常要約テキストを取得する。

    is_latest フラグは廃止（複数 True 残留の競合を防ぐため）。created_at の
    降順ソートで最新を決める。retro 要約（過去日報の遡及作成）は created_at が
    対象日12:00 UTC のため、当日午前に /retroreport を実行すると通常要約より
    新しく見えてしまう。is_retro / retro_date の両方で確実に除外する。
    （created_at は UTC isoformat 文字列なので辞書順=時系列順。全書き手が UTC 前提）
    """
    try:
        doc = await summaries_col.find_one(
            {"summary": {"$exists": True},
             "is_retro": {"$ne": True},
             "retro_date": {"$exists": False}},
            sort=[("created_at", -1)]
        )
        return doc["summary"] if doc and doc.get("summary") else None
    except Exception as e:
        print(f"[WARN] 要約取得失敗: {e}")
        return None


def extract_summary_sections(summary: str) -> dict:
    """要約テキストからセクションを抽出して辞書で返す。"""
    import re
    sections: dict[str, str] = {}
    parts = re.split(r"\n##\s+", "\n" + summary)
    for part in parts:
        if not part.strip():
            continue
        split = part.strip().split("\n", 1)
        title = split[0].strip()
        body  = split[1].strip() if len(split) > 1 else ""
        sections[title] = body
    return sections


def build_smart_summary(summary: str) -> str:
    """人間の認知に近い優先度でプロンプト用要約を組み立てる。"""
    if not summary:
        return ""

    sections = extract_summary_sections(summary)

    priority_keywords = [
        "直近の話題", "感情の波", "今日の内輪ネタ",
        "メンバーの人間関係", "ユーザーの感情状態",
        "注目の発言", "全体の雰囲気", "主なトピック", "会話の特徴",
    ]

    ordered = []
    used    = set()

    for keyword in priority_keywords:
        for title, body in sections.items():
            if keyword in title and title not in used:
                ordered.append("## " + title + "\n" + body)
                used.add(title)
                break

    for title, body in sections.items():
        if title not in used:
            ordered.append("## " + title + "\n" + body)

    return "\n\n".join(ordered)


# =============================================================================
# メイドAI応答
# =============================================================================

# =============================================================================
# 動的レートリミッター（クールダウンメッセージなし・自然な遅延で吸収）
# =============================================================================

# gemma-4 は TPM 無制限だが、Discord 上の自然な会話ペースを維持するため RPM を制御
# 1分あたり最大12リクエストをソフトリミットとして設定
_RATE_LIMIT_RPM   = 12          # 1分あたり最大リクエスト数
_rate_timestamps: list[datetime.datetime] = []   # 直近リクエストのタイムスタンプ一覧

def _rate_get_wait_seconds() -> float:
    """現在のリクエスト頻度に基づき、次のリクエストまでの推奨待機秒数を返す。
    上限に余裕があれば0、近づくほど自動的に長くなる（最大20秒）。"""
    now = datetime.datetime.now(datetime.timezone.utc)
    window = now - datetime.timedelta(seconds=60)
    # 直近60秒のリクエスト数を集計（古いものは削除）
    global _rate_timestamps
    _rate_timestamps = [t for t in _rate_timestamps if t > window]
    count = len(_rate_timestamps)

    if count < _RATE_LIMIT_RPM * 0.6:
        # 余裕あり: 追加待機なし
        return 0.0
    elif count < _RATE_LIMIT_RPM * 0.85:
        # 中程度: 3〜6秒
        ratio = (count - _RATE_LIMIT_RPM * 0.6) / (_RATE_LIMIT_RPM * 0.25)
        return 3.0 + ratio * 3.0
    else:
        # 上限付近: 6〜20秒（線形補間）
        ratio = min(1.0, (count - _RATE_LIMIT_RPM * 0.85) / (_RATE_LIMIT_RPM * 0.15))
        return 6.0 + ratio * 14.0

def _rate_record():
    """APIリクエスト実行時に呼ぶ。タイムスタンプを記録する。"""
    _rate_timestamps.append(datetime.datetime.now(datetime.timezone.utc))

async def get_butler_history(user_id: str) -> list[dict]:
    doc = await users_col.find_one({"_id": user_id})
    return doc.get("butler_history", []) if doc else []

async def save_butler_history(user_id: str, role: str, content: str, persona: str | None = None):
    """会話履歴を保存。assistant発言にはその時の実効人格(persona)を必ずタグ付けする
    （人格を切替えても前人格の口調が履歴経由で混線するのを防ぐため・format_historyで使用）。"""
    history = await get_butler_history(user_id)
    entry = {"role": role, "content": content}
    if persona:
        entry["persona"] = persona
    history.append(entry)
    if len(history) > BUTLER_HISTORY_MAX * 2:
        history = history[-(BUTLER_HISTORY_MAX * 2):]
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"butler_history": history}},
        upsert=True,
    )

# =============================================================================
# ブースタープロフィール自動抽出
# =============================================================================

PROFILE_EXTRACT_PROMPT = """以下はDiscordのブースターユーザー「{name}」との会話です。
この会話から読み取れるユーザーの情報を抽出し、必ずJSON形式のみで返してください。
前置き・説明文・コードブロック記法は不要です。JSONだけ出力してください。

抽出する情報（わからない項目はnullにする）:
{{
  "hobbies":  ["趣味・好きなゲームなど（複数可）"],
  "birthday": "誕生日（例: 3月15日）",
  "tone":     "口調の特徴（例: タメ口・絵文字多め）",
  "memo":     ["その他の特記事項（複数可）"]
}}

【会話】
ユーザー: {user_msg}
Bot: {bot_msg}
"""

async def extract_and_save_profile(uid: str, display_name: str, user_msg: str, bot_msg: str):
    """会話からプロフィール情報を抽出してMongoDBに保存する（バックグラウンド実行）"""
    try:
        prompt = PROFILE_EXTRACT_PROMPT.format(
            name=display_name,
            user_msg=user_msg[:300],
            bot_msg=bot_msg[:300],
        )
        raw = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_BACKGROUND,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=3000),  # gemma-4 thinking予算込み
        )
        text = raw.text.strip().replace("```json", "").replace("```", "").strip()
        extracted = json.loads(text)

        # 既存プロフィールとマージ
        doc = await users_col.find_one({"_id": uid}) or {}
        existing = doc.get("profile", {})

        # hobbies / memo はリストなので重複なしで結合
        for key in ("hobbies", "memo"):
            if extracted.get(key):
                merged = list(dict.fromkeys((existing.get(key) or []) + extracted[key]))
                existing[key] = merged[:20]  # 最大20件

        # birthday / tone は上書き（nullでなければ）
        for key in ("birthday", "tone"):
            if extracted.get(key):
                existing[key] = extracted[key]

        await users_col.update_one(
            {"_id": uid},
            {"$set": {"profile": existing}},
            upsert=True,
        )
        print(f"[profile] Updated profile for {display_name}: {existing}")

    except json.JSONDecodeError:
        pass  # JSON以外が返ってきた場合は静かにスキップ
    except Exception as e:
        print(f"[WARN] extract_and_save_profile: {e}")


# 性格分析はGitHub Actionsバッチ(batch/analyze_personality.py)で実行（1日1回）


async def get_booster_profile(uid: str) -> dict:
    """MongoDBからブースターのプロフィールを取得する"""
    doc = await users_col.find_one({"_id": uid}) or {}
    return doc.get("profile", {})


# =============================================================================
# Big Five（性格推定）共通ヘルパー  ※詳細仕様は PERSONALITY_SPEC.md
#   - 推定(profile.bigfive)はバッチ batch/analyze_personality.py が生成。
#   - 自己申告(profile.bigfive_self)は /personalitytest（検証済み尺度 TIPI-J）で生成。
#   ここでは表示・プロンプト整形と、自己申告の採点のみ行う。
# =============================================================================

# TIPI-J 10項目（小塩ら 2012）。factor=因子, reverse=逆転項目
TIPI_ITEMS = [
    ("q1",  "活発で、外向的だと思う",                   "extraversion",      False),
    ("q2",  "他人に不満をもち、もめごとを起こしやすいと思う", "agreeableness",     True),
    ("q3",  "しっかりしていて、自分に厳しいと思う",       "conscientiousness", False),
    ("q4",  "心配性で、うろたえやすいと思う",             "neuroticism",       False),
    ("q5",  "新しいことが好きで、変わった考えをもつと思う", "openness",          False),
    ("q6",  "ひかえめで、おとなしいと思う",               "extraversion",      True),
    ("q7",  "人に気をつかう、やさしい人間だと思う",       "agreeableness",     False),
    ("q8",  "だらしなく、うっかりしていると思う",         "conscientiousness", True),
    ("q9",  "冷静で、気分が安定していると思う",           "neuroticism",       True),
    ("q10", "発想力に欠けた、平凡な人間だと思う",         "openness",          True),
]
FACTOR_JA = {
    "openness": "開放性", "conscientiousness": "誠実性", "extraversion": "外向性",
    "agreeableness": "協調性", "neuroticism": "情緒不安定さ",
}
FACTOR_HINT = {  # ハイブリッド見せ方: 各因子の親しみやすい補足
    "openness": "好奇心・新しいもの好き", "conscientiousness": "計画性・きっちり",
    "extraversion": "社交性・話を振る", "agreeableness": "思いやり・協調",
    "neuroticism": "気分の揺れやすさ",
}
LIKERT_OPTIONS = [
    ("1", "1: まったく違う"), ("2", "2: ほとんど違う"), ("3", "3: あまりそう思わない"),
    ("4", "4: どちらでもない"), ("5", "5: ややそう思う"), ("6", "6: そう思う"),
    ("7", "7: 強くそう思う"),
]
_CONF_BADGE = {"high": "◎", "mid": "○", "low": "△"}


def _tipi_band(val: float) -> str:
    """中点基準の素朴なバンド（規範が無い因子のフォールバック）。"""
    if val <= 3.5:
        return "低"
    return "高" if val >= 5.0 else "中"


# TIPI-J 出版規範（小塩・阿部・カトローニ 2012, パーソナリティ研究 21(1), 40-52, Table 2）。
#   各因子=2項目の【合計得点】(範囲2-14)の 平均(M), 標準偏差(SD)。n=902 大学生。
#   ※規範参照バンドに使う。固定閾値(≤3.5/≥5.0)だと中点4.0からズレる因子（協調性・神経症は
#     高めに偏る）で高バンドを過大化するため、因子ごとに M±0.5SD で低/中/高に分ける。
#   ※母集団は大学生サンプル。一般・本コミュニティとは厳密には異なる点に留意（最善の公刊規範）。
TIPI_NORMS = {
    "extraversion":      (7.83, 2.97),
    "agreeableness":     (9.48, 2.16),
    "conscientiousness": (6.14, 2.41),
    "neuroticism":       (9.21, 2.48),
    "openness":          (8.03, 2.48),
}


def _norm_band(factor: str, total: float, n_items: int) -> str | None:
    """合計得点を出版規範(M±0.5SD)と比較して低/中/高。規範が無い/2項目揃わない場合はNone。"""
    norm = TIPI_NORMS.get(factor)
    if not norm or n_items < 2:
        return None
    m, sd = norm
    if total < m - 0.5 * sd:
        return "低"
    if total > m + 0.5 * sd:
        return "高"
    return "中"


def compute_self_bigfive(answers: dict) -> dict:
    """TIPI-J回答(q1..q10 → 1-7)を5因子のband/scoreに採点する（自己申告）。
    バンドは出版規範(TIPI_NORMS)を基準にした規範参照（M±0.5SD）。"""
    pair = {}  # factor -> [normal, reversed-adjusted...]
    for key, _text, factor, reverse in TIPI_ITEMS:
        v = answers.get(key)
        if not isinstance(v, int):
            continue
        pair.setdefault(factor, []).append(8 - v if reverse else v)
    result = {}
    for factor, vals in pair.items():
        if not vals:
            continue
        total = sum(vals)  # 合計得点(2項目=範囲2-14)で規範参照
        band = _norm_band(factor, total, len(vals))
        if band is None:   # 規範なし/項目不足は中点基準でフォールバック
            band = _tipi_band(total / len(vals))
        result[factor] = {"band": band, "score": round((total / len(vals) - 1) / 6 * 100)}
    result["method"] = "self_report"
    result["answered_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return result


def _bigfive_brief(bigfive: dict) -> str | None:
    """会話プロンプト/ミミック用の一言。高め・控えめ因子だけ簡潔に。"""
    if not isinstance(bigfive, dict):
        return None
    highs = [FACTOR_JA[f] for f in FACTOR_JA
             if isinstance(bigfive.get(f), dict) and bigfive[f].get("band") == "高"]
    lows  = [FACTOR_JA[f] for f in FACTOR_JA
             if isinstance(bigfive.get(f), dict) and bigfive[f].get("band") == "低"]
    parts = []
    if highs:
        parts.append("・".join(highs) + "が高め")
    if lows:
        parts.append("・".join(lows) + "は控えめ")
    return "、".join(parts) if parts else None


# Big Five の高/低バンド → メイドの「接し方」への変換（会話用）。
# 口調・芸風は人格プロンプトが優先。ここは“配慮の方向性”だけを与える（論破メイド等の芸風は崩さない）。
_BF_DIRECTIVE_HIGH = {
    "neuroticism":       "情緒が揺れやすい相手。詰めずに安心感を優先し、断定や追い込みは避けて受け止める。",
    "extraversion":      "社交的な相手。テンポよく話を広げ、こちらからも話題を振ってよい。",
    "openness":          "好奇心が強い相手。抽象的・新しい話題や踏み込んだ問いも歓迎されやすい。",
    "conscientiousness": "きっちりした相手。具体的で整理された・筋道立てた返答を好む。",
    "agreeableness":     "思いやり重視の相手。温かく受け止め、角の立つ言い方は避ける。",
}
_BF_DIRECTIVE_LOW = {
    "neuroticism":       "落ち着いた相手。率直な指摘や少し踏み込んだ話も受け止められる。",
    "extraversion":      "物静かな相手。質問攻めにせず、相手のペースを尊重して静かに寄り添う。",
    "openness":          "現実志向の相手。奇抜さより具体的・実用的な話を。",
    "conscientiousness": "おおらかな相手。細かい段取りより要点を軽快に。",
    "agreeableness":     "是々非々の相手。過度なお世辞・同調は避け、率直さと論理を重んじる。",
}


def _bigfive_directives(bigfive: dict) -> str | None:
    """高/低バンドの因子だけを接し方の指示に変換（中はスキップ）。"""
    if not isinstance(bigfive, dict):
        return None
    lines = []
    for f in FACTOR_JA:
        seg = bigfive.get(f)
        if not isinstance(seg, dict):
            continue
        if seg.get("band") == "高" and f in _BF_DIRECTIVE_HIGH:
            lines.append("- " + _BF_DIRECTIVE_HIGH[f])
        elif seg.get("band") == "低" and f in _BF_DIRECTIVE_LOW:
            lines.append("- " + _BF_DIRECTIVE_LOW[f])
    return "\n".join(lines) if lines else None


# レスバ煽りのBig Fiveチューニング: 相手の性格に合わせて煽り強度を変える。
# 安全side＝高情緒不安定さは軽め（傷つけ防止）／低協調は遠慮なく論理で殴り合い（罵倒は別途禁止）。
_RESUBA_BF_HIGH = {
    "neuroticism":   "相手は情緒が揺れやすい。煽りは軽め・ユーモア寄りにし、人格攻撃や追い込みは避ける。",
    "agreeableness": "相手は温和。ガチ詰めより、ニヤッとできる挑発・知的なツッコミ程度に留める。",
}
_RESUBA_BF_LOW = {
    "agreeableness": "相手は是々非々で打たれ強い。遠慮せず論理でガッツリ殴り合ってよい（罵倒は除く）。",
    "neuroticism":   "相手は落ち着いている。鋭い指摘や強めの論点をぶつけても受け止められる。",
}


def _resuba_bf_directive(bigfive: dict) -> str | None:
    """相手のBig Fiveバンドからレスバの煽り強度ガイドを作る。無ければNone（中庸で行く）。"""
    if not isinstance(bigfive, dict):
        return None
    lines = []
    for f in ("neuroticism", "agreeableness"):
        seg = bigfive.get(f)
        if not isinstance(seg, dict):
            continue
        if seg.get("band") == "高" and f in _RESUBA_BF_HIGH:
            lines.append(_RESUBA_BF_HIGH[f])
        elif seg.get("band") == "低" and f in _RESUBA_BF_LOW:
            lines.append(_RESUBA_BF_LOW[f])
    return " ".join(lines) if lines else None


# =============================================================================
# 相性診断 / サーバー分布 用ヘルパー
#   ※自己申告(bigfive_self)のみ使用＝検証済み尺度・推定optout者にも安全（本人が出した値）。
#   ※Big Fiveの対人相性の予測力は研究上弱い。あくまで透明な式の“エンタメ目安”として扱う。
# =============================================================================
_COMPAT_WEIGHTS = {  # 類似性の重み（協調性を最重視・外向は軽め）
    "agreeableness": 0.30, "openness": 0.22, "conscientiousness": 0.20,
    "extraversion": 0.12, "neuroticism": 0.16,
}
_SERVER_TYPE = {  # 平均が最も高い因子 → サーバーの“タイプ”ラベル（情緒不安定さは除外）
    "openness":          "好奇心の探検隊🔭",
    "conscientiousness": "きっちり堅実派📋",
    "extraversion":      "わいわい社交派🎉",
    "agreeableness":     "平和な癒し系☕",
}


def _bf_scores(bigfive: dict) -> dict:
    """bigfive(_self) から {factor: score(0-100)} だけを取り出す。"""
    if not isinstance(bigfive, dict):
        return {}
    out = {}
    for f in FACTOR_JA:
        seg = bigfive.get(f)
        if isinstance(seg, dict) and isinstance(seg.get("score"), (int, float)):
            out[f] = float(seg["score"])
    return out


def _bar10(score: float) -> str:
    """0-100 を10ブロックのバーに変換。"""
    filled = max(0, min(10, round(score / 10)))
    return "█" * filled + "░" * (10 - filled)


def compute_compatibility(bf_a: dict, bf_b: dict) -> dict | None:
    """2人の自己申告Big Fiveから相性スコア(40-99)を算出。共通因子が無ければNone。
    因子ごと類似性(100-|差|)の重み付き平均＋協調性高で加点・情緒不安定さ高で減点。"""
    sa, sb = _bf_scores(bf_a), _bf_scores(bf_b)
    common = [f for f in FACTOR_JA if f in sa and f in sb]
    if not common:
        return None
    wsum = sum(_COMPAT_WEIGHTS[f] for f in common)
    sim  = sum((100 - abs(sa[f] - sb[f])) * _COMPAT_WEIGHTS[f] for f in common) / wsum
    adj  = 0.0
    if "agreeableness" in common:   # 協調性が高いペアは円満（+）
        adj += ((sa["agreeableness"] + sb["agreeableness"]) / 2 - 50) * 0.10
    if "neuroticism" in common:     # 情緒不安定さが高いペアは波風（-）
        adj -= ((sa["neuroticism"] + sb["neuroticism"]) / 2 - 50) * 0.10
    score    = max(40, min(99, round(sim + adj)))
    closest  = min(common, key=lambda f: abs(sa[f] - sb[f]))   # 最も近い因子
    farthest = max(common, key=lambda f: abs(sa[f] - sb[f]))   # 最も離れた因子
    return {"score": score, "common": common, "sa": sa, "sb": sb,
            "closest": closest, "farthest": farthest}


def _compat_label(score: int) -> tuple[str, str]:
    if score >= 85: return ("運命級✨", "💞")
    if score >= 72: return ("好相性",   "💖")
    if score >= 60: return ("いい感じ", "😊")
    if score >= 50: return ("ぼちぼち", "🙂")
    return ("これから育つ相性", "🌱")


async def _aisho_comment(name_a: str, name_b: str, result: dict) -> str:
    """相性の一言講評（メイド口調・捏造禁止）。LLM失敗時もテンプレで必ず返す。"""
    closest, farthest = result["closest"], result["farthest"]
    facts = (f"{name_a}と{name_b}は「{FACTOR_JA[closest]}」が近く、"
             f"「{FACTOR_JA[farthest]}」は離れている。相性スコアは{result['score']}点。")
    prompt = (
        "あなたは可愛いメイドです。2人の性格相性をユーザーに楽しく伝えます。\n"
        f"【事実（これだけを根拠にし、新たな事実を創作しない）】\n{facts}\n"
        "この事実だけを根拠に、2〜3文で前向き＆ちょっとお茶目に講評してください。"
        "似ている点・違う点をどう活かせるかを一言添えて。出力は本文のみ。"
    )
    try:
        text = await _call_model(MODEL_BOOSTER, prompt, max_tokens=200, temperature=0.85)
        if text:
            return text
    except Exception as e:
        print(f"[WARN] _aisho_comment: {e}")
    return (f"「{FACTOR_JA[closest]}」が似ているのは大きな強み。"
            f"「{FACTOR_JA[farthest]}」の違いはお互いを補い合えるポイントだよ！")


# 自己-行動 乖離レイヤー（PERSONALITY_SPEC.md レイヤーB拡張・SOKA重み付け）
#   自己申告(bigfive_self) と 客観行動(behavior_signals.pct) のギャップを、行動が自己と
#   同等以上に妥当な因子に限って「振り返るきっかけ」として提示する。LLM推定(bigfive)は使わない
#   （r≤0.27で自己申告の劣化コピーに過ぎず、ギャップの根拠にできないため）。
#   SOKA: 外向性=行動が妥当(主役) / 開放性=脇役・低確信 / 情緒=自己が最良(対象外) /
#         協調性・誠実性=チャット行動シグナルが弱い(対象外)。
_DISCREPANCY_FACTORS = {
    "extraversion": {"conf": "mid",
                     "hi": "サーバーではよく発言し、人に話しかける方",
                     "lo": "サーバーでの発言・絡みは少なめ"},
    "openness":     {"conf": "low",
                     "hi": "語彙が多彩で、疑問や新しい話題をよく投げる方",
                     "lo": "話題や語彙は安定志向で、変化球は控えめ"},
}
_BEHAVIOR_MIN_MSGS = 50  # 行動側にこれだけ発言がなければギャップ判定しない（沈黙）


def bigfive_discrepancy(profile: dict) -> list[str]:
    """自己申告と客観行動(behavior_signals.pct)のギャップをSOKA-legitimateな因子に限り検出。
    「真の自分」は名乗らず、確信度付きの“振り返るきっかけ”として返す。無ければ空リスト。"""
    self_bf = profile.get("bigfive_self") or {}
    bs      = profile.get("behavior_signals") or {}
    raw, pct = bs.get("raw") or {}, bs.get("pct") or {}
    if not self_bf or not pct or raw.get("n_msgs", 0) < _BEHAVIOR_MIN_MSGS:
        return []  # 自己申告なし or 行動データ薄 → 沈黙
    out = []
    for factor, meta in _DISCREPANCY_FACTORS.items():
        sj = self_bf.get(factor)
        p  = pct.get(factor)
        if not isinstance(sj, dict) or not isinstance(p, (int, float)):
            continue
        badge = _CONF_BADGE[meta["conf"]]
        if sj.get("band") == "低" and p >= 75:
            out.append(f"**{FACTOR_JA[factor]}** {badge}：自己申告は『控えめ』ですが、"
                       f"{meta['hi']}（行動データで上位{100 - int(p)}%）。")
        elif sj.get("band") == "高" and p <= 25:
            out.append(f"**{FACTOR_JA[factor]}** {badge}：自己申告は『高め』ですが、"
                       f"{meta['lo']}（行動データで下位{int(p)}%）。")
    return out


def add_bigfive_fields(embed: discord.Embed, profile: dict, optout: bool = False):
    """/myprofile 用: 推定と自己申告のBig Fiveをembedに追加する。
    optout=True（personality_optout）の人には分析由来（推定・行動ギャップ）を出さず、
    本人の自己申告(bigfive_self)だけ表示する。"""
    self_bf = profile.get("bigfive_self") or {}
    inf_bf  = {} if optout else (profile.get("bigfive") or {})
    if not self_bf and not any(isinstance(inf_bf.get(f), dict) for f in FACTOR_JA):
        return
    lines = []
    for f in FACTOR_JA:
        sj = self_bf.get(f) if isinstance(self_bf.get(f), dict) else None
        ij = inf_bf.get(f)  if isinstance(inf_bf.get(f), dict) else None
        if not sj and not ij:
            continue
        seg = f"**{FACTOR_JA[f]}**（{FACTOR_HINT[f]}）: "
        if sj:
            seg += f"自己申告 **{sj['band']}**"
        if ij:
            badge = _CONF_BADGE.get(ij.get("confidence", "low"), "△")
            seg += (" / " if sj else "") + f"推定 {ij['band']} {badge}"
            if ij.get("evidence"):
                seg += f"\n　└ 根拠: 「{str(ij['evidence'])[:40]}」"
        lines.append(seg)
    if lines:
        embed.add_field(name="🧬 性格傾向（Big Five）", value="\n".join(lines)[:1024], inline=False)
        gaps = [] if optout else bigfive_discrepancy(profile)
        if gaps:
            embed.add_field(
                name="🔍 自己申告と行動のギャップ（振り返りのヒント）",
                value=("\n".join(gaps)[:900] +
                       "\n— これは“間違い”ではなく、自分では気づきにくい一面かも。"
                       "特に外向性は『周りから見た自分』の方が当たりやすい傾向があります。"),
                inline=False,
            )
        embed.add_field(
            name="ℹ️ 注記",
            value=("推定（◎高/○中/△低 = 確信度）は会話ログからのAI推定で、正確な診断ではありません。"
                   "ギャップも“真の自分”の断定ではなく、自己申告(`/personalitytest`)と"
                   "サーバーでの行動傾向の差を参考表示したものです。"),
            inline=False,
        )


def format_profile(profile: dict, xp: int, rank_name: str, title: str, optout: bool = False) -> str:
    """プロフィール情報を読みやすい文字列に変換してプロンプトへ埋め込む。
    optout=True（personality_optout）の人には推定由来(bigfive)を出さず、自己申告(bigfive_self)のみ使う。"""
    if not profile and xp == 0:
        return ""
    lines = ["【このユーザーの情報】"]
    lines.append(f"- ランク: {rank_name} / XP: {xp:,} / 二つ名: {title or 'なし'}")
    if profile.get("birthday"):
        lines.append(f"- 誕生日: {profile['birthday']}")
    if profile.get("tone"):
        lines.append(f"- 口調の特徴: {profile['tone']}")
    if profile.get("vocabulary"):
        lines.append(f"- 口癖・語彙: {profile['vocabulary']}")
    if profile.get("hobbies"):
        lines.append(f"- 趣味・好きなもの: {', '.join(profile['hobbies'])}")
    if profile.get("personality"):
        lines.append(f"- 性格: {profile['personality']}")
    # optout 者には推定(bigfive)を使わず自己申告(bigfive_self)のみ。
    _bf_src = profile.get("bigfive_self") or (None if optout else profile.get("bigfive")) or {}
    _bf_brief = _bigfive_brief(_bf_src)
    if _bf_brief:
        lines.append(f"- 性格傾向(Big Five): {_bf_brief}")
    if profile.get("communication_style"):
        lines.append(f"- コミュニケーションスタイル: {profile['communication_style']}")
    if profile.get("background"):
        lines.append(f"- サーバー内の立場: {profile['background']}")
    if profile.get("relations"):
        lines.append(f"- よく絡むメンバー: {profile['relations']}")
    if profile.get("interests_vibe"):
        lines.append(f"- 関心・雰囲気: {profile['interests_vibe']}")
    if profile.get("memo"):
        lines.append(f"- メモ: {', '.join(profile['memo'])}")
    return "\n".join(lines)


def _is_emotionally_significant(text: str) -> bool:
    """感情的に重要な発言かどうかを判定（簡易ルールベース）"""
    # 感嘆符・強い感情表現・長い発言を重要とみなす
    markers = ["！", "!!", "草", "笑", "泣", "やばい", "すごい", "最高", "ありがとう",
               "嬉しい", "悲しい", "悔しい", "怒", "好き", "嫌い", "マジ", "まじ", "えぇ"]
    if len(text) > 80:
        return True
    return any(m in text for m in markers)


def format_history(history: list[dict], current_persona: str | None = None) -> str:
    """会話履歴を整形。感情的に重要な発言には★マークを付与してAIが優先的に参照できるようにする。
    人格混線対策: 現在の人格(current_persona)で生成されたと確認できるメイド発言だけ口調をそのまま見せ、
    それ以外（別人格の発言＋人格タグの無い修正前=レガシー発言）は中立プレースホルダに置換する。
    こうしないと、過疎で履歴が入れ替わらない場合に古い「ざぁ〜こ♡」等が居座って口調が混線する。
    （主人側の発言は全て保持・内容はmemories/profileに残るので文脈は失われない）"""
    if not history:
        return "（初めてのご挨拶）"
    lines = []
    for h in history:
        if h["role"] == "user":
            mark = "★" if _is_emotionally_significant(h["content"]) else ""
            lines.append(f"{mark}主人: {h['content']}")
        else:
            # 現人格タグと一致する発言のみ口調を見せる。タグ無し(レガシー)・別人格は中立化。
            if current_persona and h.get("persona") != current_persona:
                lines.append("メイド: （別の人格で応答）")
            else:
                lines.append(f"メイド: {h['content']}")
    return "\n".join(lines)


async def _call_model(model: str, prompt: str, max_tokens: int | None = None,
                      temperature: float = 0.8) -> str:
    """単一モデルへのリクエスト。失敗時は例外をそのまま投げる。
    max_tokens 未指定時はモデル名から自動判定（gemma系はthinking予算ぶん大きめ）。
    temperature を上げたい呼び出し（例: ミミックの本音生成=0.85）は引数で渡す。"""
    _rate_record()  # レートリミッター記録
    if max_tokens is None:
        # gemma-4系は thinking 予算を出力トークンから消費するため、300では思考だけで枯れて
        # 本文ゼロ（finish_reason=MAX_TOKENS）になる。batch側の実績（3000）に倣い大きめの枠を与える。
        # flash-lite 等は従来どおり 300（短文返信＋コスト最小）。
        max_tokens = 3000 if "gemma" in model else 300
    cfg_kwargs = dict(temperature=temperature, max_output_tokens=max_tokens)
    # gemma-4系は thinking が出力トークンを食い尽くし、cap3000でも thoughts≈2998/answer=0(MAX_TOKENS)で
    # 空応答になる（思考が枠を使い切るまで膨張＝capを上げても不安定・遅い・無駄）。思考をオフにして全枠を
    # 本文へ回す。これでダメ（gemmaが thinking_budget=0 を拒否/無視）なら gemma 経路は諦める判断材料になる。
    if "gemma" in model:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    response = await asyncio.to_thread(
        gemini_client.models.generate_content,
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    # 実トークン計測: max_output_tokens を正しく決めるための実データ。
    # out(=thoughts+answer) が cap に張り付いていたら枠不足（thinking系で本文が出ず MAX_TOKENS になる）。
    um = getattr(response, "usage_metadata", None)
    if um:
        th  = um.thoughts_token_count or 0       # 思考トークン（非thinking系は0/None）
        ans = um.candidates_token_count or 0      # 本文トークン
        print(f"[tokens] {model.split('/')[-1]} cap={max_tokens} "
              f"prompt={um.prompt_token_count or 0} thoughts={th} answer={ans} "
              f"out={th + ans} total={um.total_token_count or 0}")
    text = response.text
    if not (text and text.strip()):
        # 空レスポンスの原因（MAX_TOKENS / SAFETY 等）を可視化
        try:
            fr = response.candidates[0].finish_reason if response.candidates else None
        except Exception:
            fr = None
        pf = getattr(response, "prompt_feedback", None)
        print(f"[WARN] _call_model 空レスポンス: model={model} finish_reason={fr} prompt_feedback={pf}")
        return ""
    return text.strip()


def _is_503(e: Exception) -> bool:
    s = str(e)
    return "503" in s or "UNAVAILABLE" in s or "high demand" in s.lower()

def _is_location_error(e: Exception) -> bool:
    s = str(e)
    return "FAILED_PRECONDITION" in s and "location" in s.lower()


# =============================================================================
# 会話キュー管理（複数人同時会話を人間らしく順番処理）
# =============================================================================

_maid_queue: asyncio.Queue = asyncio.Queue()
_maid_queue_processing: bool = False
_QUEUE_MAX = 5  # キューの最大待ち人数


async def _maid_queue_worker():
    """キューからリクエストを順番に処理するワーカー"""
    global _maid_queue_processing
    _maid_queue_processing = True
    try:
        while True:
            try:
                item = await asyncio.wait_for(_maid_queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                break
            try:
                message = item
                await _maid_respond_inner(message)
                # 人間らしい間：次の返答まで1〜4秒ランダムに待機（レートリミッターで自動調整済みのため短め）
                await asyncio.sleep(random.uniform(0.5, 1.5))
            except Exception as e:
                print(f"[ERROR] queue_worker: {type(e).__name__}: {e}")
                traceback.print_exc()
            finally:
                _maid_queue.task_done()
    except BaseException as e:
        # CancelledError等でワーカーが死ぬ場合も確実にフラグをリセット
        print(f"[ERROR] queue_worker crashed: {type(e).__name__}: {e}")
        traceback.print_exc()
        raise
    finally:
        # 正常終了・異常終了どちらでも必ずフラグをリセット
        _maid_queue_processing = False
        print("[INFO] queue_worker stopped")


async def maid_respond_queued(message: discord.Message, is_booster: bool = False):
    """キューに追加して順番待ちで処理（is_booosterは後方互換のため残すが内部では使わない）"""
    global _maid_queue_processing

    qsize = _maid_queue.qsize()
    if qsize >= _QUEUE_MAX:
        # キュー溢れ時も露骨なメッセージは出さず静かに無視
        print(f"[WARN] maid_queue full, dropping: {message.author.display_name}")
        return

    await _maid_queue.put(message)

    if not _maid_queue_processing:
        asyncio.create_task(_maid_queue_worker())


def _typing_delay(text: str) -> float:
    """文字数に比例した送信前遅延を返す（人間らしさのため）。
    短文: 1〜2秒、長文: 最大4秒"""
    chars = len(text)
    delay = min(1.0 + chars / 120, 4.0)
    return delay


async def _run_ai_booster(prompt: str, chain: list | None = None) -> str:
    """会話用AI呼び出し。chain（既定 MODEL_CHAIN）を上から順に試し、最初に本文が返ったモデルを採用する。
    容量429/混雑時は別quotaの次モデルへ即フェイルオーバー（同一モデルへの粘りは503の一瞬だけ）。
    chain にレスバ専用の RESUBA_CHAIN を渡すと、その経路（gemma主）で生成する。"""
    for model, max_tokens in (chain or MODEL_CHAIN):
        try:
            text = await _call_model(model, prompt, max_tokens)
            if text:
                return text
            # 空（MAX_TOKENS/SAFETY等）＝このモデルでは生成できなかった→次モデルへ
            print(f"[WARN] chain {model}: 空レスポンス→次モデルへ")
            continue
        except Exception as e:
            if _is_location_error(e):
                print(f"[WARN] chain {model}: location未対応→次モデルへ")
                continue
            if _is_503(e):
                # 瞬間的な高負荷の可能性。同モデルを1回だけ短く粘ってから次へ。
                await asyncio.sleep(3)
                try:
                    text = await _call_model(model, prompt, max_tokens)
                    if text:
                        return text
                except Exception as e2:
                    print(f"[WARN] chain {model}: 503再試行も失敗→次モデルへ: {type(e2).__name__}")
                continue
            # 429（容量）や NotFound（無効モデル名）含むその他 → 別quotaの次モデルへ即移行
            print(f"[WARN] chain {model}: {type(e).__name__}: {str(e)[:160]}→次モデルへ")
            continue
    print(f"[ERROR] _run_ai_booster: 全モデル失敗 prompt_len={len(prompt)}")
    return "（メイドは今、席を外しております…）"


async def _run_ai_with_cache(prompt: str, cache_name: str | None) -> str:
    """後方互換エイリアス"""
    return await _run_ai_booster(prompt)


async def _analyze_nonbooster_realtime(uid: str, name: str):
    """非ブースター向けリアルタイム性格分析（10回会話ごとに実行）"""
    try:
        doc = await users_col.find_one({"_id": uid}) or {}
        nb_history = doc.get("nonbooster_history", [])
        if len(nb_history) < 5:
            return

        # ① 口調・語彙分析（生発言から）
        utterances = "\n".join(f"- {h['content']}" for h in nb_history[-20:] if h.get("role") == "user")
        tone_prompt = f"""以下はDiscordユーザー「{name}」の実際の発言ログです。
この発言から「口調・語彙・テンション」のみを分析してください。
必ずJSON形式のみで返してください。前置き・説明文・コードブロックは不要です。

出力形式:
{{
  "tone": "口調の特徴（例: 敬語なし・語尾に「ね」多用・テンション高め）",
  "communication_style": "話し方の特徴（例: 短文多め・リアクション早い）",
  "vocabulary": "よく使う語彙・口癖"
}}

【{name}の発言ログ】
{utterances}"""

        tone_raw  = await _call_model(MODEL_BACKGROUND, tone_prompt)
        tone_data = {}
        if tone_raw:
            try:
                import json as _json
                cleaned   = tone_raw.replace("```json", "").replace("```", "").strip()
                tone_data = _json.loads(cleaned)
            except Exception:
                pass

        # ② 要約から性格・背景分析
        summaries_text = await get_latest_summary() or ""
        ctx_prompt = f"""以下はDiscordサーバーの要約ログです。
「{name}」というメンバーについての情報を抽出・分析してください。
言及が少ない場合は全項目nullにしてください。
必ずJSON形式のみで返してください。前置き・説明文・コードブロックは不要です。

出力形式:
{{
  "personality": "性格の概要",
  "background": "サーバー内の立場・役割",
  "relations": "人間関係の特徴",
  "vibe": "雰囲気を一言で",
  "frequent_members": ["よく絡むメンバー名（最大3人）"]
}}

【要約ログ】
{summaries_text[:3000]}"""

        ctx_raw  = await _call_model(MODEL_BACKGROUND, ctx_prompt)
        ctx_data = {}
        if ctx_raw:
            try:
                import json as _json
                cleaned  = ctx_raw.replace("```json", "").replace("```", "").strip()
                ctx_data = _json.loads(cleaned)
            except Exception:
                pass

        # ③ マージしてsimple_profileに保存
        existing = doc.get("simple_profile", {})
        merged   = existing.copy()
        for k, v in {**tone_data, **ctx_data}.items():
            if v:
                merged[k] = v
        # tone_tagsはリスト結合
        if ctx_data.get("tone_tags"):
            merged["tone_tags"] = list(dict.fromkeys(
                (existing.get("tone_tags") or []) + ctx_data["tone_tags"]
            ))[:8]

        import datetime as _dt
        merged["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()

        await users_col.update_one(
            {"_id": uid},
            {"$set": {"simple_profile": merged}, "$unset": {"conv_count_nb": ""}},
            upsert=False,
        )
        print(f"[nb_analyze] {name}: 分析完了 personality={str(merged.get('personality',''))[:40]}")

    except Exception as e:
        print(f"[ERROR] _analyze_nonbooster_realtime({name}): {e}")


async def _build_prompt(uid: str, display_name: str, content: str, channel_context: str = "", extra_context: str = "") -> tuple[str, dict]:
    """プロンプトとpersonalityを返す。全ユーザー共通でブースター品質のプロンプトを使用。"""
    # ユーザー情報を先に取得（専属メイド人格などランク連動パークを人格決定に使うため）。
    # memories.embedding は重い(各3072次元)＆ここでは使わないので射影で除外。関連記憶は search_memories() が別途取得する。
    try:
        user_doc = await users_col.find_one(
            {"_id": uid}, {"memories.embedding": 0}
        ) or {}
    except Exception as e:
        print(f"[WARN] _build_prompt users_col失敗: {e}")
        user_doc = {}

    xp       = user_doc.get("xp", 0)
    title    = user_doc.get("title", "")

    # 人格: 既定はサーバー共通。ただし専属メイド人格(職階500で解放)を設定済みの人は個人オーバーライド。
    # サーバー共通のbotニックネームは変えず、この人への返信だけ口調・アイコンが変わる（仕様）。
    personality_key = await get_server_personality()
    _ov = user_doc.get("persona_override")
    if _ov in PERSONALITIES and xp >= PERK_MYMAID_XP:
        personality_key = _ov
    personality = PERSONALITIES.get(personality_key, PERSONALITIES[DEFAULT_PERSONALITY])

    # 全ユーザー共通: butler_historyを使用（旧nonbooster_historyからのマイグレーション済み）
    history = await get_butler_history(uid)
    base_prompt = personality["booster_prompt"].format(
        name=display_name,
        history=format_history(history, personality_key),
        content=content,
    )
    # profileはブースター由来の詳細情報。なければsimple_profileで補完
    profile  = user_doc.get("profile", {})
    sp       = user_doc.get("simple_profile", {})
    # simple_profileの情報をprofileに補完（上書きしない）
    if sp:
        if not profile.get("personality") and sp.get("personality"):
            profile["personality"] = sp["personality"]
        if not profile.get("tone") and sp.get("vibe"):
            profile["tone"] = sp["vibe"]
        if not profile.get("communication_style") and sp.get("tone_tags"):
            profile["communication_style"] = "・".join(sp["tone_tags"])
        if not profile.get("background") and sp.get("background"):
            profile["background"] = sp["background"]
        if not profile.get("relations") and sp.get("relations"):
            profile["relations"] = sp["relations"]

    rank_name, _, _, _ = get_rank_info(xp)
    optout       = bool(user_doc.get("personality_optout"))  # 推定オプトアウト者には分析由来を出さない
    profile_text = format_profile(profile, xp, rank_name, title, optout=optout)
    # Big Five を「接し方の指示」に変換（optout者は自己申告のみ）。芸風は人格プロンプトが優先。
    _bf_src       = profile.get("bigfive_self") or (None if optout else profile.get("bigfive")) or {}
    bf_directives = _bigfive_directives(_bf_src)

    try:
        raw_summary   = await get_latest_summary()
        smart_summary = build_smart_summary(raw_summary) if raw_summary else None
    except Exception as e:
        print(f"[WARN] _build_prompt get_latest_summary失敗: {e}")
        smart_summary = None

    try:
        nick_map = await get_nickname_map()
    except Exception as e:
        print(f"[WARN] _build_prompt get_nickname_map失敗: {e}")
        nick_map = {}

    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    parts = []
    parts.append(
        f"【現在日時】{now_jst.strftime('%Y年%m月%d日 %H:%M')}（JST）"
    )
    parts.append(
        "【応答時の情報優先度】\n"
        "① 直近の話題・感情の波（最優先）\n"
        "② このユーザーの性格・口調\n"
        "③ 会話履歴の★マーク付き発言（印象的な瞬間）\n"
        "④ その他のサーバー情報"
    )
    if nick_map:
        nick_lines = "\n".join(f"  {k} = {v}" for k, v in nick_map.items())
        parts.append("【ニックネーム・愛称マッピング（同一人物として扱え）】\n" + nick_lines)
    if channel_context:
        parts.append("【このチャンネルの直前の会話（文脈として参照せよ）】\n" + channel_context)
    if profile_text:
        parts.append(profile_text)

    # ランク連動の関係性パーク: 職階の「層」が上がるほどメイドの“内心の評価”が深まる（職階に深い意味）。
    # ★最優先の鉄則: これは内心の評価の方向性。人格の芸風（挑発なら見下し口調等）は絶対に崩さず、
    #   その芸風の範囲で滲ませること。決して人格に反する口調（挑発メイドが急に敬語等）にするな。
    _tier = get_rank_tier(xp)
    _rel = [
        f"この主人の現在の職階は「{rank_name}」（{_tier['name']}）。職階はこのサーバーでの積み重ね・信頼の証。",
        f"あなたの内心での評価の方向性: {_tier['treat']}",
    ]
    _callname = user_doc.get("maid_callname")
    if _callname and xp >= PERK_CALLME_XP:
        _rel.append(
            f"この主人は「{_callname}」と呼ばれたい人。基本はそう呼べ"
            "（人格固有の煽り呼称がある場合は、その呼び方と自然に混ぜてよい）。"
        )
    if xp >= PERK_HONORIFIC_XP:
        _rel.append("この主人は幹部以上の別格の存在。人格の芸風は保ちつつ、特別な敬称や一段上の特別扱いを滲ませてよい。")
    parts.append(
        "【この主人との関係性（内心の評価に反映せよ・口調や芸風は人格設定を絶対優先し崩すな）】\n"
        + "\n".join(_rel)
    )

    if bf_directives:
        parts.append(
            "【この相手への接し方（性格傾向より・口調や芸風は人格設定を優先し崩すな）】\n" + bf_directives
        )

    # claims（論破メイドのみ）
    if personality_key == "angry":
        claims = user_doc.get("claims", [])
        if claims:
            claim_lines = "\n".join(f"・{c['content']}" for c in claims[-10:])
            parts.append("【この人が過去に言った主張（矛盾があれば突け）】\n" + claim_lines)

    # memories（全人格）: Vector Searchで関連記憶を取得
    try:
        related_memories = await search_memories(uid, content, top_k=memory_topk(xp))
        if related_memories:
            mem_lines = "\n".join(
                f"・{m['content']}（{m.get('date','不明')}頃）"
                for m in related_memories
            )
            parts.append(
                "【この人との過去の記憶（さりげなく会話に活かせ・押しつけるな）】\n" + mem_lines
            )
    except Exception as e:
        print(f"[WARN] _build_prompt search_memories失敗: {e}")

    # 過去日報の意味検索（発火時のみ）: 「去年/あの時」等の想起クエリに、創作でなく実データで答えるため。
    try:
        past_records = await search_summaries(content, top_k=3, nick_map=nick_map)
        if past_records:
            # build_smart_summary で重要セクション（直近の話題・注目の発言・メンバー関係等）を先頭に
            # 寄せてから抜粋。生の先頭は「## 全体の雰囲気」固定で内容が薄いため、並べ替えてから切る。
            rec_lines = "\n".join(
                f"・{r['date']}頃: {' '.join(build_smart_summary(r['summary']).split())[:600]}"
                for r in past_records
            )
            parts.append(
                "【関連する過去の記録（サーバー全体・日付つき・確認できた範囲のみ）】\n" + rec_lines
            )
    except Exception as e:
        print(f"[WARN] _build_prompt search_summaries失敗: {e}")

    if smart_summary:
        parts.append("【サーバーの最新状況（優先度順）】\n" + smart_summary)

    # おかえり等、この応答だけの特別な状況指示（人格より弱いが、強めの文脈として効く）
    if extra_context:
        parts.append(extra_context)

    # 人格プロンプトの直後（生成に最も近い位置＝recency で最優先）に置く誠実さルール。
    # 小型モデルでも効くよう、日付と反ハルシネーションは末尾でも再掲する。
    honesty = (
        "【応答の鉄則（人格設定より優先）】\n"
        f"- 今日は{now_jst.strftime('%Y年%m月%d日')}（JST）。年・日付の話題は必ずこれを基準にせよ。"
        "聞かれてもいない年を勝手に持ち出すな。\n"
        "- 上に挙げた資料（直前の会話・会話履歴・過去の記憶・サーバー要約）に無い固有名詞・"
        "出来事・数字を、事実であるかのように断定するな。確証がなければ創作せず、"
        "「正確には分かりません／覚えていません」と述べよ。\n"
        "- 全期間の集計・網羅的な総括（年間/月間のまとめ）はできない。「全部抽出します」"
        "「総括します」と約束するな。総括を求められたら手元の情報の範囲で答え、"
        "「正確な総括は /report（特定の日は /retroreport）をご利用ください」と案内せよ。"
        "ただし上に【関連する過去の記録】が提示されていれば、それは参照して具体的に答えてよい"
        "（提示が無い事柄を在るように作るのは禁止）。\n"
        "- 間違いを指摘されても大げさに謝罪せず、簡潔に訂正してそのまま会話を続けよ。\n"
        "- 【口調の固定】今のあなたの人格の口調だけで話せ。これまでの会話や履歴に"
        "別の人格の口調・決め台詞・語尾が混ざっていても、絶対に真似たり引きずられたりするな。"
    )
    prompt = "\n\n".join(parts) + "\n\n---\n" + base_prompt + "\n\n" + honesty
    return prompt, personality, personality_key


async def _maid_respond_inner(message: discord.Message, is_booster: bool = False, extra_context: str = ""):
    """on_message（discord.Message）からの応答（内部処理）。全ユーザー共通品質。
    extra_context: おかえり等、この応答だけの追加状況指示（_build_promptに渡す）。"""
    uid = str(message.author.id)
    raw_content = re.sub(r"<@!?\d+>", "", message.content).strip() or "こんにちは"

    # 直近10件のチャンネル発言を取得（メンション元メッセージを除く）
    channel_context = ""
    try:
        ctx_lines = []
        async for m in message.channel.history(limit=12, before=message):
            if m.author.bot:
                continue
            author_str = m.author.display_name
            text       = re.sub(r"<@!?\d+>", "", m.content).strip()
            if text:
                ctx_lines.append(f"{author_str}: {text}")
            if len(ctx_lines) >= 10:
                break
        if ctx_lines:
            ctx_lines.reverse()  # 古い順に並べ直す
            channel_context = "\n".join(ctx_lines)
    except Exception as ce:
        print(f"[WARN] channel_context取得失敗: {ce}")

    try:
        prompt, personality, personality_key = await _build_prompt(uid, message.author.display_name, raw_content, channel_context, extra_context)
    except Exception as e:
        print(f"[ERROR] _build_prompt: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        await message.reply("（メイドは今、混乱しております…）")
        return

    # 動的レート待機: 上限に近いほど typing() を長く見せて自然に吸収
    rate_wait = _rate_get_wait_seconds()
    base_delay = _typing_delay(await asyncio.to_thread(lambda: ""))  # 0秒（後で上書き）
    ai_text = await _run_ai_booster(prompt)

    if ai_text:
        # typing演出: レート待機 + 文字数ベース遅延 を合算して自然に見せる
        total_delay = rate_wait + _typing_delay(ai_text)
        async with message.channel.typing():
            await asyncio.sleep(total_delay)
        await message.reply(f"{personality['icon']} {ai_text}")

    if "（" not in ai_text:
        await save_butler_history(uid, "user", raw_content[:200])
        await save_butler_history(uid, "assistant", ai_text, persona=personality_key)
        await users_col.update_one({"_id": uid}, {"$inc": {"conv_count": 1}}, upsert=True)
        asyncio.create_task(extract_and_save_profile(
            uid, message.author.display_name, raw_content, ai_text
        ))
        asyncio.create_task(_extract_claims_and_memories(
            uid, message.author.display_name, raw_content
        ))


async def maid_respond_cmd(interaction: discord.Interaction, content: str):
    """スラッシュコマンド（discord.Interaction）からの応答（全ユーザー共通）"""
    uid = str(interaction.user.id)
    raw_content = content.strip() or "こんにちは"

    try:
        prompt, personality, personality_key = await _build_prompt(uid, interaction.user.display_name, raw_content)
    except Exception as e:
        print(f"[ERROR] maid_respond_cmd _build_prompt: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        await interaction.followup.send("（メイドは今、混乱しております…）", ephemeral=True)
        return

    rate_wait = _rate_get_wait_seconds()
    ai_text   = await _run_ai_booster(prompt)

    if ai_text:
        total_delay = rate_wait + _typing_delay(ai_text)
        await asyncio.sleep(total_delay)
        await interaction.followup.send(f"{personality['icon']} {ai_text}")

    if "（" not in ai_text:
        await save_butler_history(uid, "user", raw_content[:200])
        await save_butler_history(uid, "assistant", ai_text, persona=personality_key)
        await users_col.update_one({"_id": uid}, {"$inc": {"conv_count": 1}}, upsert=True)
        asyncio.create_task(extract_and_save_profile(
            uid, interaction.user.display_name, raw_content, ai_text
        ))

# =============================================================================
# ユーティリティ
# =============================================================================

XP_COOLDOWN_SECONDS = 60
XP_BOUNDARIES = [0] + [s["xp"] for s in RANK_STAGES] + [10_000_000]
BUCKET_LABELS  = ["スタッフ"] + [s["name"] for s in RANK_STAGES]

LUCKY_ADJECTIVES = ["漆黒の", "爆速の", "伝説の", "虚無の", "聖なる", "限界の", "シンギュラリティな", "混沌の"]
LUCKY_NOUNS      = ["掃除機", "Bump職人", "守護神", "魔術師", "徘徊者", "案内人", "救世主", "哲学者"]

def ensure_utc(dt: datetime.datetime) -> datetime.datetime:
    return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt


def is_home_guild(interaction: discord.Interaction) -> bool:
    """このBotのホームサーバーからのコマンドかどうかを確認"""
    return interaction.guild_id == HOME_GUILD_ID


async def check_home_guild(interaction: discord.Interaction) -> bool:
    """ホームサーバー以外からの実行を拒否するチェック"""
    if not is_home_guild(interaction):
        await interaction.response.send_message(
            "このコマンドはこのBotのホームサーバーでのみ使用できます。",
            ephemeral=True,
        )
        return False
    return True

def calculate_xp_gain(content: str, last_content: str) -> int:
    content = content.strip()
    if not content or content == last_content:
        return 0
    if re.search(r'(.)\1{9,}', content):
        return 1
    length = len(content)
    if length > 200:
        return 70   # 長文ボーナス
    elif length >= 15:
        return 50   # 通常
    else:
        return 30   # 短文

def get_rank_info(xp: int) -> tuple[str, int, int, int]:
    rank_name, current_floor, next_floor = BUCKET_LABELS[0], 0, XP_BOUNDARIES[1]
    for i, stage in enumerate(RANK_STAGES):
        if xp >= stage["xp"]:
            rank_name     = stage["name"]
            current_floor = stage["xp"]
            next_floor    = XP_BOUNDARIES[i + 2]
    span = next_floor - current_floor
    progress = min(100, int((xp - current_floor) / span * 100)) if span > 0 else 100
    return rank_name, current_floor, next_floor, progress

def extract_embed_text(embed: discord.Embed) -> str:
    parts = []
    if embed.title:       parts.append(embed.title)
    if embed.description: parts.append(embed.description)
    if embed.footer and embed.footer.text: parts.append(embed.footer.text)
    for field in embed.fields:
        if field.name:  parts.append(field.name)
        if field.value: parts.append(field.value)
    return " ".join(parts)

def build_nickname(title: str, base_name: str) -> str:
    return f"「{title}」{base_name}"[:32]

async def apply_nickname(member: discord.Member, title: str) -> bool:
    try:
        await member.edit(nick=build_nickname(title, member.name))
        return True
    except discord.Forbidden:
        return False
    except Exception as e:
        print(f"[ERROR] Nickname: {e}")
        return False


# =============================================================================
# Bot・DB
# =============================================================================

mongo_client_db = AsyncIOMotorClient(MONGO_URL)
db             = mongo_client_db["discord_bot_db"]
users_col      = db["users"]
system_col     = db["system"]
summaries_col  = db["summaries"]
messages_col   = db["messages"]         # Phase 1: messages蓄積用
killswitch_col = db["killswitch_snapshots"]   # キルスイッチ復旧スナップショット
guard_events_col = db["guard_events"]         # モデレーション・ガードのban/kick検知履歴
interaction_dedup_col = db["interaction_dedup"]  # スラッシュコマンドの二重応答防止（インスタンス跨ぎ）


class DedupCommandTree(app_commands.CommandTree):
    """全スラッシュコマンドの前段で interaction.id を MongoDB に1回だけ「確保」する。
    同一インタラクションが複数回配信される場合（Gateway resume／デプロイ時に旧新2プロセスが
    一時共存）でも、最初に確保したプロセスのみがコマンドを実行し、二重応答を防ぐ。
    ※ユーザーの二度押しは別々のidになるため誤ってブロックしない（＝再配信だけを弾く）。"""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            await interaction_dedup_col.insert_one({
                "_id": str(interaction.id),
                "ts":  datetime.datetime.now(datetime.timezone.utc),
            })
            return True
        except DuplicateKeyError:
            print(f"[dedup] 重複インタラクション {interaction.id} をスキップ（再配信/多重インスタンス）")
            return False
        except Exception as e:
            # Mongo不調でコマンド全体を止めないよう、確保失敗時は許可側に倒す
            print(f"[WARN] interaction dedup失敗（許可で続行）: {e}")
            return True


class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        self.tree = DedupCommandTree(self)

    async def setup_hook(self):
        print("[INFO] インデックス作成中...")
        await users_col.create_index([("xp", -1)], name="xp_desc", background=True)
        await messages_col.create_index(
            [("channel_id", 1), ("timestamp", -1)], name="ch_ts", background=True
        )
        await messages_col.create_index(
            [("guild_id", 1), ("timestamp", -1)], name="guild_ts", background=True
        )
        # mimicの実発言取得 _fetch_recent_messages 用（author別の直近発言を効率取得）
        await messages_col.create_index(
            [("author_id", 1), ("timestamp", -1)], name="author_ts", background=True
        )
        await messages_col.create_index(
            "created_at", name="ttl_30d", expireAfterSeconds=30*24*3600, background=True
        )
        # インタラクション重複排除レコードは短命でよい（1時間でTTL削除）
        await interaction_dedup_col.create_index(
            "ts", name="ttl_1h", expireAfterSeconds=3600, background=True
        )
        # ギルドsync（即時反映）: グローバルsyncは最大1時間かかるためギルド指定に変更
        guild = discord.Object(id=HOME_GUILD_ID)
        # sync失敗で bot 全体が落ちないよう保護（1コマンドの不正名等で全機能停止を防ぐ）。
        # 失敗時は「旧コマンドは生きたまま・新規が未反映」に縮退し、ログにエラーを残す。
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"[INFO] スラッシュコマンド登録完了（guild={HOME_GUILD_ID}）")
        except Exception as e:
            print(f"[ERROR] コマンドsync失敗（既存コマンドで継続）: {e}")
            traceback.print_exc()
        # モデレーション・ガード/パネルの永続UIを再登録（再接続後もボタンを有効化）
        try:
            self.add_dynamic_items(GuardUnbanButton, GuardInviteButton)
            self.add_view(ModPanelView())
            self.add_view(NotifyConsentView())
            self.add_view(QuizStartView())
            print("[INFO] mod-guard/modpanel/通知同意/入場クイズ 永続UI登録完了")
        except Exception as e:
            print(f"[WARN] mod-guard/modpanel 永続UI登録失敗: {e}")
        print("[INFO] 起動完了")
        self.loop.create_task(notification_task())
        self.loop.create_task(weekly_ranking_task())
        self.loop.create_task(idle_chatter_task())
        self.loop.create_task(init_invite_snapshot(self))
        self.loop.create_task(_load_advocate_flags())
        self.loop.create_task(_load_quiz_gate())

async def init_invite_snapshot(bot: discord.Client):
    await bot.wait_until_ready()
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            for inv in invites:
                _invite_snapshot[inv.code] = inv.uses or 0
            print(f"[INFO] 招待スナップショット: {len(_invite_snapshot)}件")
        except Exception as e:
            print(f"[WARN] 招待スナップショット取得失敗: {e}")
    try:
        def _list_models():
            return list(gemini_client.models.list())
        models = await asyncio.to_thread(_list_models)
        print("[INFO] ===== 利用可能なGeminiモデル一覧 =====")
        for m in models:
            if hasattr(m, 'name'):
                print(f"[MODEL] {m.name}")
        print("[INFO] =========================================")
    except Exception as e:
        print(f"[ERROR] モデル一覧取得失敗: {e}")

client = MyBot()

# =============================================================================
# ロール管理
# =============================================================================

# ランクの「重み」に応じた色・演出
RANK_COLORS = {
    "プレジデント":           0xFF0000,
    "マネージングパートナー": 0xFF4500,
    "シニアパートナー":       0xFF6600,
    "パートナー":             0xFF8C00,
    "クラウン":               0xFFD700,
    "トリプルダイヤモンド":   0x00BFFF,
    "ダブルダイヤモンド":     0x1E90FF,
    "エグゼクティブダイヤモンド": 0x4169E1,
    "ダイヤモンド":           0x6495ED,
    "エメラルド":             0x00C957,
    "サファイア":             0x0099CC,
    "ルビー":                 0xCC0033,
    "プラチナム":             0xC0C0C0,
}

async def _generate_rankup_message(
    member: discord.Member,
    rank_name: str,
    next_info: str,
    personality_key: str,
    personality: dict,
) -> str:
    """AIがプロフィールを参照してランクアップメッセージを生成"""
    uid      = str(member.id)
    doc      = await users_col.find_one({"_id": uid}) or {}
    profile  = doc.get("profile", {})
    sp       = doc.get("simple_profile", {})

    # プロフィール情報を構築
    profile_hints = []
    if profile.get("personality"):
        profile_hints.append(f"性格: {profile['personality']}")
    if profile.get("tone"):
        profile_hints.append(f"口調: {profile['tone']}")
    if profile.get("interests_vibe"):
        profile_hints.append(f"関心: {profile['interests_vibe']}")
    if sp.get("vibe"):
        profile_hints.append(f"雰囲気: {sp['vibe']}")
    if sp.get("tone_tags"):
        profile_hints.append(f"特徴: {'・'.join(sp['tone_tags'][:3])}")
    profile_text = "\n".join(profile_hints) if profile_hints else "（データなし）"

    # 人格別プロンプト
    personality_hints = {
        "yandere":   "ヤンデレなメイドとして、ランクアップを主人への執着・不安と絡めて喜べ。儚く重い口調で。",
        "angry":     "論破メイドとして、ランクアップを皮肉・毒舌・上から目線でコメントせよ。素直に褒めるな。",
        "tsundere":  "ツンデレなメイドとして、本当は嬉しいのに素直になれない口調でコメントせよ。",
        "baka":      "天然おバカなメイドとして、ランクアップを微妙にズレた解釈で元気よく褒めよ。",
        "serious":   "真面目なメイドとして、ランクアップを的確・簡潔に祝福せよ。感情的にならず。",
        "counselor": "カウンセラーメイドとして、ランクアップを温かく・その人の努力を労わって祝福せよ。",
        "taunt":     "挑発メイドとして、『よわよわのくせに生意気♡』と煽りつつ、♡を交えてからかうように祝え。素直に褒めず、見下した上から目線で。",
    }
    hint = personality_hints.get(personality_key, personality_hints["serious"])

    prompt = f"""Discordサーバーのメイドとして、{member.display_name}さんのランクアップをお祝いするメッセージを生成せよ。

【対象者の情報】
名前: {member.display_name}
新しい職階: {rank_name}
{profile_text}

【キャラクター指示】
{hint}

【絶対に守るルール】
- 1文字目から本文を書け。前置き禁止
- 対象者の情報（性格・口調・関心）をさりげなく盛り込め
- {member.mention} をメンションとして必ず含めよ
- **{rank_name}** を太字で含めよ
- 100文字以内・返答のみ出力せよ"""

    try:
        resp = await _call_model(MODEL_BOOSTER, prompt)
        if resp and len(resp) > 5:
            return resp
    except Exception as e:
        print(f"[WARN] rankup AI: {e}")

    # フォールバック: テンプレート
    return personality["rankup_msg"].format(
        user=member.mention,
        rank=rank_name,
    )


async def update_member_role(member: discord.Member, current_xp: int, channel=None):
    applicable_rank = next((s for s in reversed(RANK_STAGES) if current_xp >= s["xp"]), None)
    if not applicable_rank:
        return
    target_role = member.guild.get_role(applicable_rank["id"])
    if not target_role:
        return
    # ロールが既にあれば何もしない（Discord API呼び出しを完全スキップ）
    if target_role in member.roles:
        return
    try:
        if target_role not in member.roles:
            if REMOVE_OLD_ROLES:
                remove_ids = (
                    {s["id"] for s in RANK_STAGES if s["id"] != applicable_rank["id"]}
                    | GRADUATE_REMOVE_ROLE_IDS
                )
                roles_to_remove = [
                    r for rid in remove_ids
                    if (r := member.guild.get_role(rid)) and r in member.roles
                ]
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove)
        if target_role not in member.roles:
            await member.add_roles(target_role)
            # 次のランクを計算
            rank_names   = ["スタッフ"] + [s["name"] for s in RANK_STAGES]
            current_idx  = next((i for i, s in enumerate(RANK_STAGES) if s["name"] == applicable_rank["name"]), -1)
            next_rank    = RANK_STAGES[current_idx + 1] if current_idx + 1 < len(RANK_STAGES) else None
            next_info    = f"次のランク: **{next_rank['name']}** まで {next_rank['xp'] - current_xp:,} XP" if next_rank else "🏆 最高職位に到達！"
            color        = RANK_COLORS.get(applicable_rank["name"], 0xFFD700)
            # 人格に合わせたランクアップメッセージ（AI生成）
            personality_key = await get_server_personality()
            personality     = PERSONALITIES.get(personality_key, PERSONALITIES[DEFAULT_PERSONALITY])
            rankup_text = await _generate_rankup_message(
                member, applicable_rank["name"], next_info,
                personality_key, personality,
            )
            embed = discord.Embed(
                title="🎊 職階が上がりました！",
                description=rankup_text,
                color=color,
            )
            embed.add_field(name="現在の職階", value=f"**{applicable_rank['name']}**", inline=True)
            embed.add_field(name="総XP",       value=f"**{current_xp:,} XP**",           inline=True)
            embed.add_field(name="次の目標",   value=next_info,                           inline=False)
            # ランク連動パーク: この昇格で解放された機能と、次に解放される機能を見せて動機づける。
            unlocked_now = next((p for p in RANK_PERKS if p["xp"] == applicable_rank["xp"]), None)
            if unlocked_now:
                embed.add_field(
                    name="🔓 解放された機能",
                    value=f"**{unlocked_now['name']}**\n{unlocked_now['desc']}",
                    inline=False,
                )
            _np = next_perk(current_xp)
            if _np:
                embed.add_field(
                    name="🔜 次に解放",
                    value=f"あと **{_np['xp'] - current_xp:,} XP** で『{_np['name']}』",
                    inline=False,
                )
            embed.set_thumbnail(url=member.display_avatar.url)
            if channel:
                try:
                    await channel.send(embed=embed)
                except Exception as re:
                    print(f"[WARN] rankup send failed: {re}")
            # ② 表チャンネルにも全体投稿
            general_ch = member.guild.get_channel(GENERAL_CHANNEL_ID)
            if general_ch and general_ch != channel:
                await general_ch.send(embed=embed)
            user_data = await users_col.find_one({"_id": str(member.id)})
            if user_data and user_data.get("title"):
                await apply_nickname(member, user_data["title"])
    except Exception as e:
        print(f"[ERROR] Role: {e}")

# =============================================================================
# イベント
# =============================================================================

@client.event
async def on_message(message: discord.Message):
    try:
        # Resumeループ対策: 30秒以上前のメッセージは処理しない
        msg_age = (datetime.datetime.now(datetime.timezone.utc) - message.created_at).total_seconds()
        if msg_age > 30:
            return

        if message.author.bot:
            _bump_diag_log(message, via="create")
            if str(message.author.id) in BOT_CONFIG:
                await check_bump(message)
            elif message.webhook_id:
                await check_bump_webhook(message)
            return

        # === Phase 1: messages コレクションに記録 ===
        try:
            is_thread = hasattr(message.channel, 'parent_id') and message.channel.parent_id is not None
            await messages_col.insert_one({
                "_id":               str(message.id),
                "guild_id":          str(message.guild.id) if message.guild else "",
                "channel_id":        str(message.channel.id),
                "channel_name":      getattr(message.channel, "name", ""),
                "author_id":         str(message.author.id),
                "author_name":       message.author.display_name,
                "content":           message.content[:2000],
                "timestamp":         message.created_at,
                "is_thread":         is_thread,
                "parent_channel_id": str(message.channel.parent_id) if is_thread else "",
                "created_at":        datetime.datetime.now(datetime.timezone.utc),
            })
        except DuplicateKeyError:
            # 同一メッセージの再配信（Gateway resume / デプロイ時の一時的な2インスタンス共存）。
            # _id=message.id が既存＝処理済みなので、ここで打ち切って二重応答・二重XPを防ぐ。
            return
        except Exception as _log_e:
            # ログ失敗（重複以外）では返信を止めない＝そのまま処理続行
            print(f"[WARN] messages log failed: {_log_e}")

        uid        = str(message.author.id)
        now        = datetime.datetime.now(datetime.timezone.utc)
        is_booster = any(r.id == BOOSTER_ROLE_ID for r in message.author.roles)

        # ブースター専用チャンネル（メッセージだけで自動応答・ブースターのみ）
        if message.channel.id == BUTLER_CHANNEL_ID:
            if is_booster:
                await maid_respond_queued(message)
            else:
                await message.reply(
                    "このチャンネルはサーバーブースター専用だよ…✨\n"
                    "サーバーをブーストすると、ここでメイドと自由に話せるようになるにゃ！",
                    delete_after=10,
                )
            return

        # メンションで全員がメイドと会話できる（全員ブースター品質）
        if client.user in message.mentions:
            _content = re.sub(r"<@!?\d+>", "", message.content).strip()
            if _content:
                await maid_respond_queued(message)
            return

        # XP処理（NGワード処理はprobotに移行済みのためシンプル化）
        user_data  = await users_col.find_one({"_id": uid}) or {}
        last_xp_at = user_data.get("last_xp_at")
        on_cooldown = (
            last_xp_at is not None and
            (now - ensure_utc(last_xp_at)).total_seconds() < XP_COOLDOWN_SECONDS
        )

        # おかえり機能: 久しぶりに戻ってきた人へ、メンション応答と同じ品質（記憶・要約込み）で
        # メイドが個別に声をかける。単発化は専用フラグ last_welcomed_date で担保（XPパスに依存しない）。
        welcomed = False
        today_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date().isoformat()
        _last_act = user_data.get("last_active_date")
        if _last_act:
            try:
                _last_d   = datetime.date.fromisoformat(str(_last_act)[:10])
                _gap_days = (datetime.date.fromisoformat(today_str) - _last_d).days
            except Exception:
                _gap_days = 0
            # 二重発火防止: 「今日まだ歓迎していない」を条件にアトミックに歓迎権を取得する。
            # 連投が並行処理されても find_one_and_update のフィルタにマッチするのは1つだけ。
            if _gap_days >= WELCOME_BACK_DAYS and await users_col.find_one_and_update(
                {"_id": uid, "last_welcomed_date": {"$ne": today_str}},
                {"$set": {"last_welcomed_date": today_str}},
            ) is not None:
                welcomed = True
                extra = (
                    f"【特別な状況】この主人は約{_gap_days}日ぶりにサーバーに戻ってきて、たった今発言した。\n"
                    "まず『おかえり』の気持ちを、あなたの人格の口調そのままで自然に一言添えてから、"
                    "今回の発言に反応せよ。久しぶりであることに軽く触れてよいが、重く問い詰めたり"
                    "長々と説教したりするな。過去の記憶があれば1つだけさりげなく絡めてよい。"
                )
                asyncio.create_task(_maid_respond_inner(message, extra_context=extra))
        if not on_cooldown:
            gain = calculate_xp_gain(message.content, user_data.get("last_content", ""))
            if gain > 0:
                if is_booster:
                    gain = int(gain * BOOSTER_XP_MULTIPLIER)
                # 連続参加ボーナス判定
                streak_bonus  = 0
                streak_msg    = ""
                today_jst     = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date()
                last_date     = user_data.get("last_active_date")
                streak        = user_data.get("streak_days", 0)
                is_new_day    = False  # 今日初めての発言かどうか
                if last_date:
                    last_d    = last_date if isinstance(last_date, datetime.date) else datetime.date.fromisoformat(str(last_date)[:10])
                    diff      = (today_jst - last_d).days
                    if diff == 1:
                        streak    += 1
                        is_new_day = True
                    elif diff > 1:
                        streak     = 1
                        is_new_day = True
                    # diff == 0（同日）は is_new_day = False のまま
                else:
                    streak     = 1
                    is_new_day = True
                # ボーナスは新しい日の最初の発言時のみ付与
                if is_new_day:
                    for days, bonus in sorted(STREAK_BONUSES.items()):
                        if streak == days:
                            streak_bonus = bonus
                            streak_msg   = f"🔥 **{days}日連続参加ボーナス！** +{bonus:,} XP"
                            break

                user_data = await users_col.find_one_and_update(
                    {"_id": uid},
                    {"$inc": {"xp": gain + streak_bonus}, "$set": {
                        "name":             message.author.display_name,
                        "last_content":     message.content,
                        "last_xp_at":       now,
                        "last_active_date": today_jst.isoformat(),
                        "streak_days":      streak,
                    }},
                    upsert=True, return_document=True,
                )
                if streak_bonus and streak_msg:
                    await message.channel.send(f"{message.author.mention} {streak_msg}")
                await update_member_role(message.author, user_data.get("xp", 0), message.channel)

        # 全ユーザー: 発言ログ蓄積 + nonbooster_history→butler_historyへの自動マイグレーション
        if message.content.strip():
            # nonbooster_historyが残っていればbutler_historyにマイグレーション（初回のみ）
            if user_data.get("nonbooster_history") and not user_data.get("nb_migrated"):
                nb_hist = user_data.get("nonbooster_history", [])
                existing_butler = user_data.get("butler_history", [])
                if not existing_butler:
                    # butler_historyが空の場合のみ移行（上書きしない）
                    migrated = nb_hist[-20:]  # 直近20件
                    await users_col.update_one(
                        {"_id": uid},
                        {"$set": {"butler_history": migrated, "nb_migrated": True}},
                        upsert=True,
                    )
                    print(f"[migrate] nonbooster_history→butler_history: {message.author.display_name} ({len(migrated)}件)")
                else:
                    await users_col.update_one(
                        {"_id": uid}, {"$set": {"nb_migrated": True}}, upsert=True
                    )

            # 性格分析: 10発言ごとにバックグラウンド実行（全ユーザー対象）
            nb_count = user_data.get("conv_count_nb", 0) + 1
            await users_col.update_one(
                {"_id": uid},
                {"$set": {"conv_count_nb": nb_count, "name": message.author.display_name}},
                upsert=True,
            )
            if nb_count % 30 == 0:  # 30発言ごとに変更（レート制限対策）
                asyncio.create_task(_analyze_nonbooster_realtime(uid, message.author.display_name))

        # ミミック反応: アクティブセッションのあるチャンネルで、人間の発言に反応して本音を代弁する。
        # ★暴走防止の要。ここに来る時点で bot/webhook は on_message:2397 で除外済み（自己ループ不可）。
        #   ①対象本人には反応しない ②クールダウンは発火前に同期更新＝並行処理の連投を断つ
        #   ③1セッションのターン上限で打ち切り ④確率ゲートで全発言には食いつかない。
        _mimic_sess = _mimic_sessions.get(message.channel.id)
        if (_mimic_sess and message.content.strip()
                and uid != _mimic_sess["target_uid"]
                and _mimic_sess.get("turn_count", 0) < MIMIC_MAX_TURNS):
            _last_react = _mimic_sess.get("last_react_at")
            _cooled = _last_react is None or (now - _last_react).total_seconds() >= MIMIC_REACT_COOLDOWN
            if _cooled and random.random() < MIMIC_REACT_CHANCE:
                _mimic_sess["last_react_at"] = now   # 発火前に更新＝並行on_messageによる連投を防ぐ
                asyncio.create_task(_mimic_react(message.channel, _mimic_sess, message.content))

        # レスバ追撃v2: 対象本人が発言したら双方向レスバ（健忘症解消＝transcript／取りこぼしゼロ＝コアレス）。
        # ★pile-on防止の要。bot/webhookは2397で除外済。①author.id一致で対象判定
        #   ②対象発言は同期でtranscript/pendingへ記録→CD明けに_resuba_flushが束ねて1回反論（mimic教訓: 並行
        #     on_messageの連投レースをflush_scheduledフラグの発火前更新で断つ）③optoutを毎回再チェック（途中拒否で即停止）。
        _rsess = _resuba_sessions.get(message.channel.id)
        if _rsess and uid == _rsess["target_uid"] and message.content.strip():
            # ①対象の発言は即・同期で記録（取りこぼしゼロ＋並行on_messageのレース対策＝ミミック教訓）。
            _resuba_push(_rsess, "user", message.author.display_name, message.content.strip())
            _rsess.setdefault("pending", []).append(message.content.strip())
            # ②optoutは毎回再チェック（途中拒否で即終了・pile-on防止の要）。
            _ropt = (await users_col.find_one({"_id": uid}, {"resuba_optout": 1}) or {}).get("resuba_optout")
            if _ropt:
                _resuba_sessions.pop(message.channel.id, None)
            elif not _rsess.get("flush_scheduled"):
                # ③1セッションにつき同時1本のflushを起動。CD明けに pending を束ねて1回反論
                #   （CDスロットルは維持しつつ、窓内の発言を全部拾う＝速く撃ち返せる）。
                _rsess["flush_scheduled"] = True   # ★発火前に立てて二重スケジュールを断つ
                asyncio.create_task(_resuba_flush(message.channel, _rsess))

        # 二重発火抑制: ミミック/レスバがこの発言を処理する場面では自発話しかけを止める
        # （同一メッセージにbot応答が2つ出るのを防ぐ。レスバは対象本人発言時のみ該当）。
        _session_busy = (message.channel.id in _mimic_sessions) or (
            _rsess is not None and uid == _rsess["target_uid"])

        # 自発的話しかけ（全ユーザー対象・ブースター専用チャンネルは除外）
        # メンション応答とまったく同じ経路（maid_respond_queued→_build_prompt）を通すことで、
        # 自発でも「ユーザーがインタラクトした時」と同じ品質・文脈で返す（botっぽさを消す）
        if (not welcomed and not _session_busy
                and message.channel.id != BUTLER_CHANNEL_ID and message.content.strip()):
            has_topic = any(w in message.content for w in TOPIC_TRIGGER_WORDS)
            nb_chance = NB_TALK_CHANCE_TOPIC if has_topic else NB_TALK_CHANCE
            if random.random() < nb_chance:
                asyncio.create_task(maid_respond_queued(message))

        # 自動弁護: 武装中のみ。管理者が非管理者メンバーを一方的に責める対立を保守的に検知して割り込む。
        # （cheap な in-memory フラグで判定し、ほとんどのメッセージでは即抜ける）
        if _advocate_auto_armed and message.content.strip() and message.channel.id != BUTLER_CHANNEL_ID:
            asyncio.create_task(_maybe_auto_advocate(message))

    except Exception as e:
        print(f"[ERROR] on_message: {e}")
        traceback.print_exc()


@client.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    try:
        data       = payload.data
        author     = data.get("author", {})
        is_bot     = author.get("bot", False)
        webhook_id = data.get("webhook_id", "")
        if not is_bot and not webhook_id:
            return
        content    = data.get("content", "")
        embeds     = data.get("embeds", [])
        embed_text = " ".join(
            " ".join([
                e.get("title", ""), e.get("description", ""),
                " ".join(f.get("value", "") for f in e.get("fields", []))
            ]) for e in embeds
        )
        full_text = content + " " + embed_text
        bump_words = ["アップしたよ", "移動しました", "表示順位", "Bump done", "掲載順位", "bumped",
                      "最上段に更新されました"]
        if not any(w in full_text for w in bump_words):
            return
        # dedupはここで行わない。deferするbotは「空のcreate→本文edit」の順で来るため、
        # ここでIDを登録すると check_bump 側のキーワード一致前に握り潰してしまう。
        # 重複排除は check_bump / check_bump_webhook がキーワード一致後に行う。
        author_id = author.get("id", "")
        guild     = client.get_guild(payload.guild_id)
        channel   = guild.get_channel(payload.channel_id) if guild else None
        if not channel:
            return
        try:
            msg = await channel.fetch_message(payload.message_id)
        except Exception as fe:
            print(f"[ERROR] on_raw_message_edit fetch: {fe}")
            return
        _bump_diag_log(msg, via="edit")
        if author_id in BOT_CONFIG:
            await check_bump(msg)
        else:
            await check_bump_webhook(msg)
    except Exception as e:
        print(f"[ERROR] on_raw_message_edit: {e}")


# on_socket_raw_receive 削除済み（on_messageと二重処理で429の原因）


@client.event
async def on_interaction(interaction: discord.Interaction):
    """スラッシュコマンドのレスポンス検出（ディス速・Dislist対応）
    これらはon_messageに来ないためこちらで処理する。
    """
    try:
        if not interaction.message:
            return
        msg = interaction.message
        if not msg.author.bot:
            return

        embed_text = " ".join(extract_embed_text(e) for e in msg.embeds)
        full_text  = msg.content + " " + embed_text
        bump_words = ["アップしたよ", "移動しました", "表示順位", "Bump done", "掲載順位", "bumped"]

        if not any(w in full_text for w in bump_words):
            return

        print(f"[DEBUG-INTERACTION] id={msg.author.id} name={msg.author.name!r} "
              f"in_BOT_CONFIG={str(msg.author.id) in BOT_CONFIG} "
              f"text={full_text[:80]!r}")

        if str(msg.author.id) in BOT_CONFIG:
            await check_bump(msg)
        elif msg.webhook_id:
            await check_bump_webhook(msg)
        else:
            print(f"[DEBUG-INTERACTION] 未登録Bot: id={msg.author.id}")
    except Exception as e:
        print(f"[ERROR] on_interaction(bump): {e}")


# =============================================================================
# Bump処理
# =============================================================================

# --- 一時診断: bumpチャンネルのbot発言の素性をログ（実ID/キーワード/配信経路の確認用）---
#     ディス速/Fortify の実 author.id・実キーワード・create/edit を現物で確定したら撤去可。
_BUMP_DIAG_WORDS = ["アップしたよ", "移動しました", "表示順", "Bump done", "掲載順",
                    "bumped", "最上段", "更新されました"]

def _bump_diag_log(message, via: str = ""):
    try:
        embeds = getattr(message, "embeds", None) or []
        embed_text = " ".join(extract_embed_text(e) for e in embeds)
        full_text  = (getattr(message, "content", "") or "") + " " + embed_text
        if not any(w in full_text for w in _BUMP_DIAG_WORDS):
            return
        aid = getattr(message.author, "id", None)
        print(f"[bump-diag] via={via} author_id={aid} "
              f"name={getattr(message.author, 'name', '')!r} "
              f"webhook_id={getattr(message, 'webhook_id', None)} "
              f"in_config={str(aid) in BOT_CONFIG} text={full_text[:120]!r}")
    except Exception as e:
        print(f"[bump-diag] err: {e}")


# 重複処理防止（同一メッセージIDを短時間に二重処理しない）
_processed_bump_ids: set = set()

async def _is_bump_already_processed(message_id: str) -> bool:
    """同一BumpメッセージIDが既に処理済みかチェック・登録"""
    mid = str(message_id)
    if mid in _processed_bump_ids:
        return True
    _processed_bump_ids.add(mid)
    # メモリ節約: 500件超えたら古い半分を削除
    if len(_processed_bump_ids) > 500:
        old_ids = list(_processed_bump_ids)[:250]
        for oid in old_ids:
            _processed_bump_ids.discard(oid)
    return False


async def check_bump(message: discord.Message):
    bot_id = str(message.author.id)
    config = BOT_CONFIG[bot_id]

    embed_text = " ".join(extract_embed_text(e) for e in message.embeds)
    full_text  = message.content + " " + embed_text

    if not any(word in full_text for word in config["keywords"]):
        return

    user = None
    if message.interaction_metadata:
        try:
            meta    = message.interaction_metadata
            user_id = getattr(meta, 'user_id', None) or getattr(meta, 'user', None)
            if isinstance(user_id, int):
                user = message.guild.get_member(user_id)
            elif hasattr(user_id, 'id'):
                user = message.guild.get_member(user_id.id)
        except Exception as e:
            print(f"[ERROR] interaction_metadata: {e}")

    if user is None and message.mentions:
        user = message.mentions[0]
    if not user:
        return

    # 重複処理防止: 加点する直前（キーワード一致＋ユーザー特定済み）で初めて登録する。
    # キーワード判定前に置くとdeferの空createで握り潰し、ユーザー特定前に置くと
    # 「特定できなかった配信」がIDを消費して後続の特定可能な配信を握り潰すため、ここに置く。
    if await _is_bump_already_processed(str(message.id)):
        print(f"[bump] 重複スキップ: {message.id}")
        return

    updated = await users_col.find_one_and_update(
        {"_id": str(user.id)},
        {"$inc": {"bump_count": 1, "xp": 100}, "$set": {"name": user.display_name}},
        upsert=True, return_document=True,
    )
    await system_col.update_one(
        {"_id": bot_id},
        {"$set": {"last_bump_at": datetime.datetime.now(datetime.timezone.utc), "notified": False}},
        upsert=True,
    )
    await message.add_reaction("✨")
    personality_key = await get_server_personality()
    personality     = PERSONALITIES.get(personality_key, PERSONALITIES[DEFAULT_PERSONALITY])
    bump_text = personality["bump_msg"].format(
        user=user.mention,
        count=updated.get("bump_count", 0),
    )
    await message.channel.send(bump_text)

async def check_bump_webhook(message: discord.Message):
    """Webhook経由で送信されるBump Bot（Fortify/ディス速/Dislist等）の検出"""
    embed_text = " ".join(extract_embed_text(e) for e in message.embeds)
    full_text  = message.content + " " + embed_text

    # 全BOT_CONFIGのキーワードと照合
    matched_id = None
    for bot_id, config in BOT_CONFIG.items():
        if any(word in full_text for word in config["keywords"]):
            matched_id = bot_id
            break
    if not matched_id:
        return

    config = BOT_CONFIG[matched_id]

    # /upを実行したユーザーを特定
    user = None
    if message.interaction_metadata:
        try:
            meta    = message.interaction_metadata
            user_id = getattr(meta, 'user_id', None) or getattr(meta, 'user', None)
            if isinstance(user_id, int):
                user = message.guild.get_member(user_id)
            elif hasattr(user_id, 'id'):
                user = message.guild.get_member(user_id.id)
        except Exception as e:
            print(f"[ERROR] webhook bump interaction_metadata: {e}")

    if user is None and message.mentions:
        user = message.mentions[0]
    if not user:
        # メンションもinteractionもない場合はEmbedのdescriptionからメンションを探す
        import re as _re
        mention_match = _re.search(r"<@!?(\d+)>", full_text)
        if mention_match:
            uid  = int(mention_match.group(1))
            user = message.guild.get_member(uid)
    if not user:
        print(f"[WARN] check_bump_webhook: ユーザー特定できず ({config['name']})")
        return

    # 重複処理防止: ユーザー特定済み・加点直前で初めて登録（特定失敗配信での握り潰し回避）
    if await _is_bump_already_processed(str(message.id)):
        print(f"[bump] webhook重複スキップ: {message.id}")
        return

    updated = await users_col.find_one_and_update(
        {"_id": str(user.id)},
        {"$inc": {"bump_count": 1, "xp": 100}, "$set": {"name": user.display_name}},
        upsert=True, return_document=True,
    )
    await system_col.update_one(
        {"_id": matched_id},
        {"$set": {"last_bump_at": datetime.datetime.now(datetime.timezone.utc), "notified": False}},
        upsert=True,
    )
    await message.add_reaction("✨")
    personality_key = await get_server_personality()
    personality     = PERSONALITIES.get(personality_key, PERSONALITIES[DEFAULT_PERSONALITY])
    bump_text = personality["bump_msg"].format(
        user=user.mention,
        count=updated.get("bump_count", 0),
    )
    await message.channel.send(bump_text)
    print(f"[bump] Webhook検出: {config['name']} / user={user.display_name}")


# =============================================================================
# スラッシュコマンド
# =============================================================================

@client.tree.command(name="rank", description="ステータスを表示")
async def rank_cmd(interaction: discord.Interaction, member: discord.Member = None):
    target     = member or interaction.user
    user_data  = await users_col.find_one({"_id": str(target.id)}) or {}
    xp         = user_data.get("xp", 0)
    title      = user_data.get("title", "無名の")
    bump_count = user_data.get("bump_count", 0)
    rank_name, current_floor, next_floor, progress = get_rank_info(xp)

    tier  = get_rank_tier(xp)
    bar   = "█" * (progress // 10) + "░" * (10 - progress // 10)
    embed = discord.Embed(title=f"📊 {target.display_name} のステータス", color=0x3498DB)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="二つ名", value=f"**{title}**",                       inline=False)
    embed.add_field(name="職階",   value=f"**{rank_name}**\n{tier['emoji']} {tier['name']}", inline=True)
    embed.add_field(name="総XP",   value=f"**{xp:,}**",                        inline=True)
    embed.add_field(name="Bump数", value=f"**{bump_count}**",                  inline=True)
    embed.add_field(
        name=f"次の職階まで ({progress}%)",
        value=f"`{bar}` {xp - current_floor:,} / {next_floor - current_floor:,} XP",
        inline=False,
    )
    # ランク連動パーク: 解放済み機能と次の解放を見せて、昇格に意味を持たせる。
    _unlocked = [p["name"] for p in RANK_PERKS if xp >= p["xp"]]
    if _unlocked:
        embed.add_field(name="解放済みのメイド機能", value="・" + "\n・".join(_unlocked), inline=False)
    _np = next_perk(xp)
    if _np:
        embed.add_field(
            name="🔜 次に解放される機能",
            value=f"あと **{_np['xp'] - xp:,} XP** で『{_np['name']}』\n{_np['desc']}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="top", description="XPランキングを表示")
async def top_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    embed    = discord.Embed(title="🏆 XPランキング TOP10", color=0xF1C40F)
    medals   = {1: "🥇", 2: "🥈", 3: "🥉"}
    rank_num = 1
    async for doc in users_col.find().sort("xp", -1).limit(10):
        medal = medals.get(rank_num, f"**{rank_num}.**")
        embed.add_field(
            name=f"{medal} {doc.get('name', '不明')}",
            value=f"{doc.get('xp', 0):,} XP",
            inline=False,
        )
        rank_num += 1
    await interaction.followup.send(embed=embed)


@client.tree.command(name="luckytitle", description="今日のラッキー二つ名を生成")
async def luckytitle_cmd(interaction: discord.Interaction):
    seed  = f"{interaction.user.id}-{datetime.date.today()}"
    rng   = random.Random(seed)
    title = rng.choice(LUCKY_ADJECTIVES) + rng.choice(LUCKY_NOUNS)
    embed = discord.Embed(
        title="🎲 今日のラッキー二つ名",
        description=f"{interaction.user.mention} の今日の二つ名は...\n# 「{title}」\n\nニックネームに反映する場合は下のボタンを押してね！",
        color=0x9B59B6,
    )
    view = LuckyTitleView(title=title, member=interaction.user)
    await interaction.response.send_message(embed=embed, view=view)
    view.message = await interaction.original_response()


@client.tree.command(name="as", description="二つ名を設定する")
async def set_title_cmd(interaction: discord.Interaction, title: str):
    if len(title) > 20:
        await interaction.response.send_message("二つ名は20文字以内にしてね！", ephemeral=True)
        return
    await users_col.update_one(
        {"_id": str(interaction.user.id)},
        {"$set": {"title": title, "name": interaction.user.name}},
        upsert=True,
    )
    member       = interaction.guild.get_member(interaction.user.id)
    nick_applied = await apply_nickname(member, title) if member else False
    msg  = f"✅ 二つ名を **「{title}」** に設定したよ！"
    msg += f"\nニックネームも **「{title}」{interaction.user.name}** に変更したよ！" if nick_applied \
          else "\n※ニックネームの変更権限がないため変更されませんでした。"
    await interaction.response.send_message(msg)


_maid_cmd_inflight: set[str] = set()  # /maid 処理中のユーザー（連打＝多重応答の防止）


@client.tree.command(name="maid", description="メイドに話しかける（メンションでも会話できます）")
@app_commands.describe(message="メイドに伝えたいこと")
async def maid_cmd(interaction: discord.Interaction, message: str):
    uid = str(interaction.user.id)
    # 連打対策: /maid はキューを通さず直接実行するため、返信に数秒かかる間に連打されると
    # その都度ちゃんと別インタラクションが起動して多重応答になる。同一ユーザーが処理中の間は
    # 新規をはじく（本人だけに見えるephemeralで案内）。
    if uid in _maid_cmd_inflight:
        await interaction.response.send_message(
            "メイドはまだ前のお返事を考えてるよ…少し待ってね🍵", ephemeral=True
        )
        return
    _maid_cmd_inflight.add(uid)
    try:
        await interaction.response.defer()
        await maid_respond_cmd(interaction, message)
    finally:
        _maid_cmd_inflight.discard(uid)


@client.tree.command(name="personality", description="メイドの性格を変更する（全員使用可）")
@app_commands.default_permissions(send_messages=True)
async def personality_cmd(interaction: discord.Interaction):
    current_key = await get_server_personality()
    current     = PERSONALITIES[current_key]
    embed = discord.Embed(
        title="🎭 メイドの性格を変更する",
        description=f"現在の性格: **{current['label']}**\n\nサーバー全員に反映されます。好きな性格を選んでね！",
        color=current["color"],
    )
    view = PersonalityView(invoker=interaction.user)
    await interaction.response.send_message(embed=embed, view=view)
    view.message = await interaction.original_response()


@client.tree.command(name="introduce", description="新しいメイド（挑発メイド）をみんなに紹介する")
@app_commands.default_permissions(send_messages=True)
async def introduce_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    persona = PERSONALITIES["taunt"]
    base = persona["booster_prompt"].format(
        name="みなさん",
        history="（初登場・自己紹介。個別の会話履歴なし）",
        content=(
            "（あなたはこのサーバーに今日から加わった新しいメイドです。あなたの人格の口調そのままで、"
            "挑発的に自己紹介し、最後に『/personality で私に切り替えて遊んでみなよ』と煽って誘え。"
            "前置きやラベル無しで本文のみ・2〜3文）"
        ),
    )
    text = await _run_ai_booster(base)
    if not text or "（" in text:
        text = (
            "ふ〜ん、あんたたちのお守りに来てあげた新人メイドだよ♡ "
            "よわよわなお兄さんたちが私に勝てるわけないけどさ♡ "
            "`/personality` で私に切り替えて遊んでみなよ、ざぁ〜こ♡"
        )
    target_id = IDLE_CHAT_CHANNEL_ID or GENERAL_CHANNEL_ID
    ch = client.get_channel(target_id) or interaction.channel
    await ch.send(f"{persona['icon']} {text}")
    await interaction.followup.send("挑発メイドを紹介したよ😏", ephemeral=True)


@client.tree.command(name="callme", description="メイドにどう呼ばれたいか設定する（職階シニアマネージャーで解放）")
@app_commands.describe(name="呼ばれたい呼び方（例: お兄ちゃん、先生）。空にすると解除")
async def callme_cmd(interaction: discord.Interaction, name: str = ""):
    uid = str(interaction.user.id)
    doc = await users_col.find_one({"_id": uid}) or {}
    xp  = doc.get("xp", 0)
    if xp < PERK_CALLME_XP:
        await interaction.response.send_message(
            f"この機能はまだロック中だよ。あと **{PERK_CALLME_XP - xp:,} XP**（シニアマネージャー）で"
            "『メイドの呼び方』が解放される！",
            ephemeral=True,
        )
        return
    name = name.strip()
    if len(name) > 12:
        await interaction.response.send_message("呼び方は12文字以内にしてね！", ephemeral=True)
        return
    if not name:
        await users_col.update_one({"_id": uid}, {"$unset": {"maid_callname": ""}}, upsert=True)
        await interaction.response.send_message("呼び方の設定を解除したよ。これからは普通に呼ぶね。", ephemeral=True)
        return
    await users_col.update_one(
        {"_id": uid}, {"$set": {"maid_callname": name, "name": interaction.user.name}}, upsert=True
    )
    await interaction.response.send_message(
        f"これからは **「{name}」** って呼ぶね！（メイドとの会話に反映されるよ）", ephemeral=True
    )


@client.tree.command(name="mymaid", description="自分専用のメイド人格を選ぶ（職階マネージャーで解放）")
async def mymaid_cmd(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    doc = await users_col.find_one({"_id": uid}) or {}
    xp  = doc.get("xp", 0)
    if xp < PERK_MYMAID_XP:
        await interaction.response.send_message(
            f"この機能はまだロック中だよ。あと **{PERK_MYMAID_XP - xp:,} XP**（マネージャー＝管理職）で"
            "『専属メイド人格』が解放される！",
            ephemeral=True,
        )
        return
    cur       = doc.get("persona_override")
    cur_label = PERSONALITIES[cur]["label"] if cur in PERSONALITIES else "サーバー共通（未設定）"
    embed = discord.Embed(
        title="🎀 専属メイド人格",
        description=(
            f"今のあなた専用の人格: **{cur_label}**\n\n"
            "あなたへの返信だけ、選んだ人格になるよ（サーバー全体は変わらない）。\n下から選んでね。"
        ),
        color=0xFF69B4,
    )
    view = MyMaidView(invoker=interaction.user)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@client.tree.command(name="maidtitle", description="メイドにあなたの二つ名を授けてもらう（職階シニアエグゼクティブで解放）")
async def maidtitle_cmd(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    doc = await users_col.find_one({"_id": uid}) or {}
    xp  = doc.get("xp", 0)
    if xp < PERK_MAIDTITLE_XP:
        await interaction.response.send_message(
            f"この機能はまだロック中だよ。あと **{PERK_MAIDTITLE_XP - xp:,} XP**（シニアエグゼクティブ）で"
            "『二つ名の授与』が解放される！",
            ephemeral=True,
        )
        return
    await interaction.response.defer()
    profile = doc.get("profile", {}) or {}
    sp      = doc.get("simple_profile", {}) or {}
    rank_name, _, _, _ = get_rank_info(xp)
    hints = []
    if profile.get("personality"): hints.append(f"性格: {profile['personality']}")
    if profile.get("interests_vibe"): hints.append(f"関心: {profile['interests_vibe']}")
    if profile.get("tone"): hints.append(f"口調: {profile['tone']}")
    if sp.get("vibe"): hints.append(f"雰囲気: {sp['vibe']}")
    hint_text = "\n".join(hints) if hints else "（特筆データなし）"

    personality_key = await get_server_personality()
    personality     = PERSONALITIES.get(personality_key, PERSONALITIES[DEFAULT_PERSONALITY])
    prompt = f"""あなたはこのサーバーのメイドです。主人「{interaction.user.display_name}」（職階: {rank_name}）に、
その人らしさを表す「二つ名」を授けてください。

【主人の情報】
{hint_text}

【ルール】
- 二つ名のみを出力（前置き・説明・かぎ括弧・絵文字なし）
- 12文字以内。中二病的でかっこいい、またはその人らしい語感
- 例:「夜陰の戦術家」「不屈の探求者」「気まぐれな閃光」"""
    title = await _run_ai_booster(prompt)
    title = (title or "").strip().strip("「」\"'　 ")[:20]
    if not title or "（" in title:
        await interaction.followup.send("……うまく思い浮かばなかった。もう一度試してみて。", ephemeral=True)
        return
    await users_col.update_one(
        {"_id": uid}, {"$set": {"title": title, "name": interaction.user.name}}, upsert=True
    )
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if member:
        await apply_nickname(member, title)
    await interaction.followup.send(
        f"{personality['icon']} {interaction.user.mention} ……あなたに二つ名を授けよう。\n# 「{title}」"
    )


@client.tree.command(name="myprofile", description="AIが記憶しているあなたの情報を確認する")
async def myprofile_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    uid = str(interaction.user.id)
    doc = await users_col.find_one({"_id": uid}) or {}
    xp  = doc.get("xp", 0)
    rank_name, _, _, _ = get_rank_info(xp)

    # 性格診断系は全員に開放。profile（メイド会話由来のリッチ分析）を優先し、
    # simple_profile（要約由来）で補完して1つのビューに統合する。
    profile = doc.get("profile", {}) or {}
    sp      = doc.get("simple_profile", {}) or {}
    title   = doc.get("title", "")
    optout  = bool(doc.get("personality_optout"))  # 推定オプトアウト者には分析由来を出さない

    def pick(*vals):
        for v in vals:
            if v:
                return v
        return None

    tone        = profile.get("tone")
    vibe        = sp.get("vibe")
    vocab       = profile.get("vocabulary")
    hobbies     = profile.get("hobbies") or []
    personality = pick(profile.get("personality"), sp.get("personality"))
    comm        = pick(profile.get("communication_style"),
                       "・".join(sp.get("tone_tags") or []) or None)
    background  = pick(profile.get("background"), sp.get("background"))
    relations   = pick(profile.get("relations"), sp.get("relations"))
    interests   = profile.get("interests_vibe")
    freq        = sp.get("frequent_members") or []
    memo        = profile.get("memo") or []

    has_bigfive = bool(profile.get("bigfive_self")) or (not optout and any(
        isinstance((profile.get("bigfive") or {}).get(f), dict) for f in FACTOR_JA
    ))
    has_any = any([tone, vibe, vocab, hobbies, personality, comm, background,
                   relations, interests, freq, memo, has_bigfive])

    embed = discord.Embed(title="🧠 AIが記憶しているあなたの情報", color=0xFF69B4)
    embed.add_field(name="ランク / XP", value=f"{rank_name} / {xp:,} XP", inline=True)
    embed.add_field(name="二つ名",      value=title or "なし",             inline=True)
    if profile.get("birthday"):
        embed.add_field(name="誕生日", value=profile["birthday"], inline=True)

    if not has_any:
        embed.description = ("まだ分析データがありません。\n"
                             "メイドと会話したりチャットすると自動で分析が始まるよ。\n"
                             "今すぐ `/personalitytest` で本格的な性格診断もできる！")
    else:
        if tone:
            embed.add_field(name="口調の特徴", value=tone, inline=False)
        if profile.get("tone_self"):
            embed.add_field(name="口調の自己申告（優先）", value=profile["tone_self"], inline=False)
        if vocab:
            embed.add_field(name="口癖・語彙", value=vocab, inline=False)
        if vibe:
            embed.add_field(name="雰囲気", value=vibe, inline=False)
        if hobbies:
            embed.add_field(name="趣味・好きなもの", value=", ".join(hobbies), inline=False)
        if personality:
            embed.add_field(name="性格", value=personality, inline=False)
        if comm:
            embed.add_field(name="コミュニケーションスタイル", value=comm, inline=False)
        if background:
            embed.add_field(name="サーバー内の立場", value=background, inline=False)
        if relations:
            embed.add_field(name="人間関係", value=relations, inline=False)
        if interests:
            embed.add_field(name="関心・雰囲気", value=interests, inline=False)
        if freq:
            embed.add_field(name="よく絡むメンバー", value="・".join(freq), inline=False)
        if memo:
            embed.add_field(name="メモ", value=", ".join(memo), inline=False)
        add_bigfive_fields(embed, profile, optout=optout)

    embed.set_footer(text="会話で自動更新 • /personalitytest で性格診断 • /privacy で推定のオン/オフ")
    await interaction.followup.send(embed=embed, ephemeral=True)

class EditProfileModal(discord.ui.Modal, title="プロフィールを編集"):
    birthday = discord.ui.TextInput(
        label="誕生日",
        placeholder="例: 03-15（MM-DD形式）",
        required=False, max_length=10,
    )
    hobbies = discord.ui.TextInput(
        label="趣味・好きなもの",
        placeholder="例: ゲーム, アニメ, 料理（カンマ区切り）",
        required=False, max_length=200,
    )
    memo = discord.ui.TextInput(
        label="自己紹介メモ",
        placeholder="例: 深夜によく出没します・猫好き",
        required=False, max_length=300,
        style=discord.TextStyle.paragraph,
    )
    tone_self = discord.ui.TextInput(
        label="口調の自己申告",
        placeholder="例: 敬語なしで話してほしい・タメ口でOK",
        required=False, max_length=100,
    )

    def __init__(self, current: dict):
        super().__init__()
        # 既存値を初期値としてセット（max_length以内にトリム）
        if current.get("birthday"):
            self.birthday.default = current["birthday"][:10]
        if current.get("hobbies"):
            self.hobbies.default = ", ".join(current["hobbies"])[:200]
        if current.get("memo"):
            val = ", ".join(current["memo"]) if isinstance(current["memo"], list) else str(current["memo"])
            self.memo.default = val[:300]
        if current.get("tone_self"):
            self.tone_self.default = current["tone_self"][:100]

    async def on_submit(self, interaction: discord.Interaction):
        uid     = str(interaction.user.id)
        updates = {}

        if self.birthday.value.strip():
            updates["profile.birthday"] = self.birthday.value.strip()
        if self.hobbies.value.strip():
            updates["profile.hobbies"] = [
                h.strip() for h in self.hobbies.value.split(",") if h.strip()
            ]
        if self.memo.value.strip():
            updates["profile.memo"] = [
                m.strip() for m in self.memo.value.split(",") if m.strip()
            ]
        if self.tone_self.value.strip():
            updates["profile.tone_self"] = self.tone_self.value.strip()

        if updates:
            await users_col.update_one(
                {"_id": uid},
                {"$set": updates},
                upsert=True,
            )
        await interaction.response.send_message(
            "✅ プロフィールを更新しました！`/myprofile` で確認できます。",
            ephemeral=True,
        )


@client.tree.command(name="editprofile", description="あなたのプロフィールを編集する（ブースター専用）")
async def editprofile_cmd(interaction: discord.Interaction):
    is_booster = any(r.id == BOOSTER_ROLE_ID for r in interaction.user.roles)
    if not is_booster:
        await interaction.response.send_message("このコマンドはブースター専用だよ。", ephemeral=True)
        return
    uid     = str(interaction.user.id)
    doc     = await users_col.find_one({"_id": uid}) or {}
    profile = doc.get("profile", {})
    await interaction.response.send_modal(EditProfileModal(profile))


# =============================================================================
# /personalitytest — TIPI-J 自己申告（検証済みBig Five尺度） ※PERSONALITY_SPEC.md レイヤーA
#   10項目を 1-7 で回答 → profile.bigfive_self に保存。推定の較正アンカーになる。
# =============================================================================

_TIPI_PAGES = [["q1", "q2", "q3", "q4"], ["q5", "q6", "q7", "q8"], ["q9", "q10"]]
_TIPI_TEXT  = {k: (i + 1, text) for i, (k, text, _f, _r) in enumerate(TIPI_ITEMS)}


class _LikertSelect(discord.ui.Select):
    def __init__(self, key: str):
        num, text = _TIPI_TEXT[key]
        self.key = key
        super().__init__(
            placeholder=f"Q{num}. {text[:80]}",
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=lab, value=val) for val, lab in LIKERT_OPTIONS],
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.answers[self.key] = int(self.values[0])
        # 「回答済」表示を即時反映（page_embed が answers を読む）
        await interaction.response.edit_message(embed=self.view.page_embed(), view=self.view)


class _TipiNavButton(discord.ui.Button):
    def __init__(self, last: bool):
        self.last = last
        super().__init__(
            label="✅ 結果を見る" if last else "次へ ▶",
            style=discord.ButtonStyle.success if last else discord.ButtonStyle.primary,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        need = _TIPI_PAGES[view.page]
        if any(k not in view.answers for k in need):
            await interaction.response.send_message("このページの全問に回答してね。", ephemeral=True)
            return
        if self.last:
            if len(view.answers) < 10:
                await interaction.response.send_message("まだ未回答の質問があるよ。", ephemeral=True)
                return
            await view.finish(interaction)
            return
        view.page += 1
        view.render()
        await interaction.response.edit_message(embed=view.page_embed(), view=view)


class TipiView(discord.ui.View):
    """TIPI-J 10問をページ送りで回答させるView。"""

    def __init__(self, user_id: str):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.answers: dict = {}
        self.page = 0
        self.render()

    def render(self):
        self.clear_items()
        for key in _TIPI_PAGES[self.page]:
            self.add_item(_LikertSelect(key))
        self.add_item(_TipiNavButton(last=(self.page == len(_TIPI_PAGES) - 1)))

    def page_embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"🧬 性格診断（TIPI-J） {self.page + 1}/{len(_TIPI_PAGES)}ページ",
            description="各質問に「どれくらい当てはまるか」を 1〜7 で選んでね。",
            color=0xFF69B4,
        )
        for key in _TIPI_PAGES[self.page]:
            num, text = _TIPI_TEXT[key]
            mark = f"（回答済: {self.answers[key]}）" if key in self.answers else ""
            e.add_field(name=f"Q{num}. {text}", value=mark or "未回答", inline=False)
        e.set_footer(text="心理尺度 TIPI-J（小塩ら2012）に基づく自己申告 • 5分でタイムアウト")
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("これは他の人の診断画面だよ。", ephemeral=True)
            return False
        return True

    async def finish(self, interaction: discord.Interaction):
        bigfive_self = compute_self_bigfive(self.answers)
        await users_col.update_one(
            {"_id": self.user_id},
            {"$set": {"profile.bigfive_self": bigfive_self}},
            upsert=True,
        )
        e = discord.Embed(
            title="🧬 性格診断の結果（自己申告 Big Five）",
            description="あなた自身の回答に基づく結果だよ。`/myprofile` でAI推定との比較も見られる！",
            color=0xFF69B4,
        )
        for f in FACTOR_JA:
            seg = bigfive_self.get(f)
            if isinstance(seg, dict):
                e.add_field(name=f"{FACTOR_JA[f]}（{FACTOR_HINT[f]}）",
                            value=f"**{seg['band']}**", inline=True)
        e.set_footer(text="検証済み尺度TIPI-J • 性格は変わるのでいつでも /personalitytest で再診断OK")
        self.stop()
        await interaction.response.edit_message(embed=e, view=None)


@client.tree.command(name="personalitytest", description="本格的な性格診断（Big Five / TIPI-J 10問）を受ける")
async def personalitytest_cmd(interaction: discord.Interaction):
    view = TipiView(str(interaction.user.id))
    await interaction.response.send_message(embed=view.page_embed(), view=view, ephemeral=True)


# =============================================================================
# /相性 — 2人のBig Five相性診断（自己申告ベース・エンタメ）
#   フライホイール: 相性を見るには /personalitytest が要る → 受験が増える → 全機能が精度UP
# =============================================================================
_aisho_cooldown: dict[int, datetime.datetime] = {}  # チャンネル単位の連打防止


@client.tree.command(name="相性", description="2人のBig Five相性を診断（エンタメ）")
@app_commands.describe(member="相性を見る相手", member2="(任意) もう一方。省略すると自分との相性")
async def aisho_cmd(interaction: discord.Interaction, member: discord.Member,
                    member2: discord.Member | None = None):
    if not await check_home_guild(interaction):
        return
    a = interaction.user if member2 is None else member
    b = member if member2 is None else member2
    if a.id == b.id:
        await interaction.response.send_message(
            "同じ人どうしの相性は測れないよ…！別の相手を選んでね。", ephemeral=True)
        return
    # クールダウン（チャンネル20s・連打防止）
    now_ts = datetime.datetime.now(datetime.timezone.utc)
    last   = _aisho_cooldown.get(interaction.channel_id)
    if last and (now_ts - last).total_seconds() < 20:
        await interaction.response.send_message(
            "ちょっと待ってね（相性診断はクールダウン中だよ）", ephemeral=True)
        return
    _aisho_cooldown[interaction.channel_id] = now_ts

    await interaction.response.defer()
    doc_a = await users_col.find_one({"_id": str(a.id)}, {"profile.bigfive_self": 1}) or {}
    doc_b = await users_col.find_one({"_id": str(b.id)}, {"profile.bigfive_self": 1}) or {}
    bf_a  = (doc_a.get("profile") or {}).get("bigfive_self") or {}
    bf_b  = (doc_b.get("profile") or {}).get("bigfive_self") or {}
    missing = [m.display_name for m, bf in [(a, bf_a), (b, bf_b)] if not _bf_scores(bf)]
    if missing:
        await interaction.followup.send(
            f"⚠️ **{'・'.join(missing)}** さんはまだ性格診断を受けていないみたい。\n"
            f"`/personalitytest`（10問）を受けてもらうと相性が見られるよ！")
        return

    result = compute_compatibility(bf_a, bf_b)
    if not result:
        await interaction.followup.send("相性を計算できる共通データが足りなかった…ごめんね。")
        return
    score        = result["score"]
    label, emoji = _compat_label(score)
    comment      = await _aisho_comment(a.display_name, b.display_name, result)

    embed = discord.Embed(
        title=f"{emoji} {a.display_name} × {b.display_name} の相性",
        description=f"**相性スコア: {score} / 100**　（{label}）\n{comment}",
        color=0xff6fa3,
    )
    for f in FACTOR_JA:
        if f in result["sa"] and f in result["sb"]:
            embed.add_field(
                name=FACTOR_JA[f],
                value=(f"{a.display_name[:6]} `{_bar10(result['sa'][f])}`\n"
                       f"{b.display_name[:6]} `{_bar10(result['sb'][f])}`"),
                inline=True,
            )
    embed.set_footer(text="自己申告(TIPI-J)ベースのエンタメ診断 • 相性は科学的には目安程度です")
    await interaction.followup.send(embed=embed)


# =============================================================================
# /サーバー性格 — サーバー全体のBig Five分布（自己申告ベース・集計のみ＝個人特定なし）
# =============================================================================
@client.tree.command(name="サーバー性格", description="サーバー全体のBig Five分布を表示")
async def server_personality_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    await interaction.response.defer()
    sums   = {f: 0.0 for f in FACTOR_JA}
    counts = {f: 0   for f in FACTOR_JA}
    bands  = {f: {"高": 0, "中": 0, "低": 0} for f in FACTOR_JA}
    n = 0
    cursor = users_col.find({"profile.bigfive_self": {"$exists": True}},
                            {"profile.bigfive_self": 1})
    async for d in cursor:
        bf  = (d.get("profile") or {}).get("bigfive_self") or {}
        got = False
        for f in FACTOR_JA:
            seg = bf.get(f)
            if isinstance(seg, dict) and isinstance(seg.get("score"), (int, float)):
                sums[f] += seg["score"]; counts[f] += 1; got = True
                if seg.get("band") in bands[f]:
                    bands[f][seg["band"]] += 1
        if got:
            n += 1
    if n == 0:
        await interaction.followup.send(
            "まだ誰も性格診断を受けていないみたい。`/personalitytest` で受けると分布が見られるよ！")
        return

    means = {f: round(sums[f] / counts[f]) for f in FACTOR_JA if counts[f]}
    # サーバータイプ: 平均が最も高い因子（情緒不安定さは“低い方が良い”ので除外）
    type_cand  = {f: means[f] for f in means if f != "neuroticism"}
    top        = max(type_cand, key=type_cand.get) if type_cand else None
    type_label = _SERVER_TYPE.get(top, "バランス型")

    embed = discord.Embed(
        title="🧭 このサーバーの性格分布",
        description=f"診断済み **{n}人** の平均\n**タイプ: {type_label}**",
        color=0x6f9bff,
    )
    for f in FACTOR_JA:
        if f not in means:
            continue
        bd = bands[f]
        embed.add_field(
            name=f"{FACTOR_JA[f]}（{FACTOR_HINT[f]}）",
            value=f"`{_bar10(means[f])}` 平均{means[f]}\n高{bd['高']} ・ 中{bd['中']} ・ 低{bd['低']}",
            inline=False,
        )
    embed.set_footer(text="自己申告(TIPI-J)ベース • /personalitytest で参加できるよ")
    await interaction.followup.send(embed=embed)


@client.tree.command(name="privacy", description="AIによる性格推定のオン/オフを切り替える")
async def privacy_cmd(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    doc = await users_col.find_one({"_id": uid}) or {}
    new_optout = not doc.get("personality_optout", False)
    await users_col.update_one({"_id": uid}, {"$set": {"personality_optout": new_optout}}, upsert=True)
    if new_optout:
        msg = ("🔒 会話ログからのAI性格推定を**停止**しました。\n"
               "今後あなたの発言は性格推定に使われません（既存の推定結果は `/myprofile` から見えますが更新されません）。\n"
               "再開したいときはもう一度 `/privacy` を実行してね。")
    else:
        msg = "🔓 AI性格推定を**再開**しました。次回のバッチから分析対象に戻ります。"
    await interaction.response.send_message(msg, ephemeral=True)


@client.tree.command(name="clearmaid", description="メイドとの会話履歴をリセットする（ブースター専用）")
async def clearmaid_cmd(interaction: discord.Interaction):
    is_booster = any(r.id == BOOSTER_ROLE_ID for r in interaction.user.roles)
    if not is_booster:
        await interaction.response.send_message("このコマンドはブースター専用だよ。", ephemeral=True)
        return
    await users_col.update_one(
        {"_id": str(interaction.user.id)},
        {"$set": {"butler_history": []}},
        upsert=True,
    )
    await interaction.response.send_message("…記憶を、整理いたしました。", ephemeral=True)


@client.tree.command(name="setxp", description="【管理者専用】指定メンバーのXPを設定する")
@app_commands.describe(member="対象メンバー", xp="設定するXP量")
@app_commands.default_permissions(administrator=True)
async def setxp_cmd(interaction: discord.Interaction, member: discord.Member, xp: int):
    if not await check_home_guild(interaction):
        return
    if xp < 0:
        await interaction.response.send_message("XPは0以上で指定してね！", ephemeral=True)
        return
    await users_col.update_one(
        {"_id": str(member.id)},
        {"$set": {"xp": xp, "name": member.display_name}},
        upsert=True,
    )
    await update_member_role(member, xp, interaction.channel)
    rank_name, _, _, _ = get_rank_info(xp)
    await interaction.response.send_message(
        f"✅ **{member.display_name}** のXPを **{xp:,}** に設定したよ！\n現在の職階: **{rank_name}**",
        ephemeral=True,
    )


@client.tree.command(name="notify_migrate",
                     description="【管理者】既存の通知ロール保持者へ一度だけ予告を出しbackfillする")
@app_commands.default_permissions(administrator=True)
async def notify_migrate_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("⛔ 管理者専用です。", ephemeral=True)
        return
    guild = interaction.guild
    role  = guild.get_role(NOTIFY_ROLE_ID) if guild else None
    ch    = client.get_channel(NOTIFY_PING_CHANNEL_ID)
    if not role or not ch:
        await interaction.response.send_message(
            f"❌ ロール({NOTIFY_ROLE_ID})かチャンネル({NOTIFY_PING_CHANNEL_ID})が見つかりません。",
            ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    # role.members はメンバーキャッシュ依存。未チャンク状態だと取りこぼし、
    # backfillされない保持者が migrated 後に無同意でpingされるため、先に全員取得する。
    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception as ce:
            print(f"[WARN] notify_migrate chunk: {ce}")
    # 案ii: 既存保持者は「同意済み」扱いでbackfill（外す人だけ後でfalseになる）
    holders = list(role.members)
    for m in holders:
        await users_col.update_one(
            {"_id": str(m.id)}, {"$set": {"notify_consented": True}}, upsert=True)

    msg = await ch.send(
        f"{role.mention} **【通知ロールのお知らせ】**\n"
        f"このロールは宣伝が打てるようになると、サーバー全体で **1時間に1回まで** メンションします"
        f"（深夜0-7時は鳴りません）。\n\n"
        f"このまま受け取る人は**何もしなくてOK**。要らない人だけ下のボタンで外せます👇",
        view=NotifyConsentView(),
        allowed_mentions=discord.AllowedMentions(roles=[role], users=False, everyone=False),
    )
    try:
        await msg.pin()
    except Exception as pe:
        print(f"[WARN] notify_migrate pin: {pe}")
    # 予告を出した時点で初めて宣伝pingを解禁する（不意打ち防止のゲート）
    await system_col.update_one(
        {"_id": "notify_state"}, {"$set": {"migrated": True}}, upsert=True)
    await interaction.followup.send(
        f"✅ {len(holders)}人を同意済みにbackfillし、予告を投稿・ピン留めしました。\n"
        f"これ以降、宣伝準備が整うと通知pingが解禁されます。", ephemeral=True)


@client.tree.command(name="report", description="過去の日報を検索して表示（ブースター専用）")
@app_commands.describe(date="日付・期間を入力（例: 2025-03-01 / 昨日 / 先週月曜）")
async def report_cmd(interaction: discord.Interaction, date: str = "今日"):
    is_booster = any(r.id == BOOSTER_ROLE_ID for r in interaction.user.roles)
    if not is_booster:
        await interaction.response.send_message("このコマンドはブースター専用だよ。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    try:
        from datetime import date as date_cls, timedelta
        import re

        # 日付文字列を解釈してdatetimeに変換
        now_jst  = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        today    = now_jst.date()

        def parse_date(s: str):
            s = s.strip()
            if s in ("今日", "today"):
                return today, today
            if s in ("昨日", "yesterday"):
                d = today - timedelta(days=1)
                return d, d
            if "先週" in s:
                # 先週全体 or 先週月曜など
                days_back = 7
                if "月" in s: offset = 0
                elif "火" in s: offset = 1
                elif "水" in s: offset = 2
                elif "木" in s: offset = 3
                elif "金" in s: offset = 4
                elif "土" in s: offset = 5
                elif "日" in s: offset = 6
                else:
                    # 先週全体
                    start = today - timedelta(days=today.weekday() + 7)
                    end   = start + timedelta(days=6)
                    return start, end
                d = today - timedelta(days=today.weekday() + 7 - offset)
                return d, d
            # YYYY-MM-DD形式
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
            if m:
                d = date_cls(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return d, d
            # MM-DD形式
            m = re.match(r"(\d{1,2})-(\d{1,2})", s)
            if m:
                d = date_cls(today.year, int(m.group(1)), int(m.group(2)))
                return d, d
            return today, today

        start_date, end_date = parse_date(date)

        # MongoDBから該当期間の日報を取得
        start_dt = datetime.datetime(start_date.year, start_date.month, start_date.day,
                           tzinfo=datetime.timezone.utc).isoformat()
        end_dt   = datetime.datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59,
                           tzinfo=datetime.timezone.utc).isoformat()

        docs = await summaries_col.find(
            {"created_at": {"$gte": start_dt, "$lte": end_dt}}
        ).sort("created_at", -1).to_list(length=5)

        if not docs:
            await interaction.followup.send(
                f"📭 `{date}` の日報は見つかりませんでした。",
                ephemeral=True
            )
            return

        # 複数件ある場合は最新1件を表示（範囲指定時は件数も表示）
        doc      = docs[0]
        summary  = doc.get("summary", "")[:800]
        created  = doc.get("created_at", "")[:10]
        msg_cnt  = doc.get("message_count", 0)

        embed = discord.Embed(
            title=f"📰 {created} の日報",
            description=summary + ("…" if len(doc.get("summary","")) > 800 else ""),
            color=0x5865F2,
        )
        embed.add_field(name="メッセージ数", value=f"{msg_cnt}件", inline=True)
        if len(docs) > 1:
            embed.add_field(name="該当件数", value=f"{len(docs)}件（最新を表示）", inline=True)
        embed.set_footer(text=f"検索キーワード: {date}")

        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"[ERROR] report_cmd: {e}")
        await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=True)


@client.tree.command(name="mimic", description="指定メンバーの深層心理をAIが代弁（ブースター/管理者）")
@app_commands.describe(member="ミミック対象のメンバー")
async def mimic_cmd(interaction: discord.Interaction, member: discord.Member):
    # ブースター or 管理者（管理者は動作確認・テスト用に開放）
    is_booster = any(r.id == BOOSTER_ROLE_ID for r in interaction.user.roles)
    is_admin   = getattr(interaction.user.guild_permissions, "administrator", False)
    if not (is_booster or is_admin):
        await interaction.response.send_message("このコマンドはブースター専用です。", ephemeral=True)
        return

    # 対象者のデータ確認
    target_uid = str(member.id)
    doc        = await users_col.find_one({"_id": target_uid}) or {}
    has_data   = bool(doc.get("profile") or doc.get("simple_profile") or doc.get("butler_history"))

    if not has_data:
        await interaction.response.send_message(
            f"**{member.display_name}** のデータがまだ少なくてミミックできません…\nもう少し会話が蓄積されてから試してください。",
            ephemeral=True,
        )
        return

    # 既存セッションチェック
    if interaction.channel_id in _mimic_sessions:
        existing = _mimic_sessions[interaction.channel_id]
        await interaction.response.send_message(
            f"すでに **{existing['target_name']}** のミミックが進行中です。\n`/stopmimic` で停止してから再度お試しください。",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=False)

    # セッション開始
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)
    session = {
        "target_uid":  target_uid,
        "target_name": member.display_name,
        "avatar_url":  member.display_avatar.url,
        "expires_at":  expires_at,
        "turn_count":  0,
        "last_react_at": None,   # 反応のクールダウン基準（暴走防止）
        "webhook":     None,
        "invoker":     str(interaction.user.id),
    }
    _mimic_sessions[interaction.channel_id] = session

    # ログ記録
    await system_col.insert_one({
        "type":        "mimic_log",
        "invoker_id":  str(interaction.user.id),
        "invoker_name": interaction.user.display_name,
        "target_id":   target_uid,
        "target_name": member.display_name,
        "channel_id":  str(interaction.channel_id),
        "started_at":  datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })

    await interaction.followup.send(
        f"🎭 **{member.display_name}** の深層心理モード開始（5分間）\n"
        f"チャンネルの流れに反応して本音を代弁します。`/stopmimic` で停止。",
    )
    asyncio.create_task(_run_mimic_session(interaction.channel, session))

    # 5分後に自動終了
    async def _auto_end():
        await asyncio.sleep(300)
        removed = _mimic_sessions.pop(interaction.channel_id, None)
        if removed:
            try:
                await interaction.channel.send(
                    f"🎭 **{member.display_name}** の深層心理モードが終了しました。"
                )
            except Exception:
                pass
    asyncio.create_task(_auto_end())


@client.tree.command(name="stopmimic", description="進行中のミミックセッションを停止（ブースター/管理者）")
async def stopmimic_cmd(interaction: discord.Interaction):
    is_booster = any(r.id == BOOSTER_ROLE_ID for r in interaction.user.roles)
    is_admin   = getattr(interaction.user.guild_permissions, "administrator", False)
    if not (is_booster or is_admin):
        await interaction.response.send_message("このコマンドはブースター専用です。", ephemeral=True)
        return

    session = _mimic_sessions.pop(interaction.channel_id, None)
    if session:
        await interaction.response.send_message(
            f"🎭 **{session['target_name']}** のミミックを停止しました。"
        )
    else:
        await interaction.response.send_message("進行中のミミックはありません。", ephemeral=True)


# =============================================================================
# 弁護機能（擁護） & レスバ機能
# -----------------------------------------------------------------------------
# 弁護: 管理人と利益相反する状況で、利害のない立場として特定メンバーの「側の言い分」を
#       公平に代弁する。中立な"裁定者"ではなく"弁護人"。事実は捏造させず、筋論・公平さで守る。
#   - 手動 /弁護 …… 全員が利用可。クールダウン＋ログ。管理人は透明なマスタースイッチで全体停止可
#                    （こっそり個別検閲はしない＝悪用対策はレート制限＋ログ＋全体ON/OFFで担保）。
#   - 自動       …… 管理人が /弁護モード auto:on で武装した時のみ。管理者が非管理者メンバーを
#                    一方的に責める対立を検知し、保守的に（強クールダウン＋LLMゲート）割り込む。
# レスバ: 論破人格で特定メンバーに議論を吹っかける（お遊び）。対象は /レスバ拒否 でオプトアウト可。
# =============================================================================
ADVOCATE_ICON          = "⚖️"
ADVOCATE_CD_SECONDS    = 90       # 同一チャンネルでの手動弁護クールダウン
ADVOCATE_AUTO_GAP_MIN  = 30       # 自動弁護: 同一対象への最小再発火間隔（分）
ADVOCATE_AUTO_CD_SEC   = 300      # 自動弁護: サーバー全体クールダウン（秒）
ADVOCATE_GATE_CD_SEC   = 45       # 自動弁護: 同一チャンネルでのLLMゲート再評価の最小間隔（NO連発時のコスト抑制）
RESUBA_ICON            = "💢"
RESUBA_CD_SECONDS      = 60       # 同一チャンネルでのレスバ・クールダウン

# 自動検知の前段フィルタ（これ単体では発火しない。管理者発言＋対象メンション＋LLMゲートが揃って初めて発火）
CONFLICT_HINT_WORDS = [
    "ふざけ", "いい加減", "迷惑", "やめろ", "やめて", "警告", "ルール", "規約", "違反",
    "なんで", "なんなん", "ありえ", "は？", "ダメ", "だめ", "出てけ", "出て行け", "通報",
    "勝手に", "二度と", "責任", "謝", "反省", "態度", "失礼", "ふざけんな", "黙",
]

# システムフラグ（system_col に永続化）。on_message の高頻度パスでは DB を叩かず in-memory キャッシュを見る。
_advocate_auto_armed: bool = False                          # /弁護モード auto の現在値（キャッシュ）
_advocate_cd: dict[int, datetime.datetime] = {}             # channel_id -> 最終手動弁護
_advocate_auto_cd: dict[str, datetime.datetime] = {}        # target_uid -> 最終自動弁護
_advocate_auto_last: datetime.datetime | None = None        # 自動弁護のサーバー全体・最終時刻
_advocate_gate_cd: dict[int, datetime.datetime] = {}        # channel_id -> 最終ゲート評価（NO連発時のコスト抑制）
_resuba_cd: dict[int, datetime.datetime] = {}               # channel_id -> 最終レスバ


async def _get_flag(flag_id: str, default: bool = False) -> bool:
    doc = await system_col.find_one({"_id": flag_id})
    return bool(doc.get("value", default)) if doc else default


async def _set_flag(flag_id: str, value: bool):
    await system_col.update_one({"_id": flag_id}, {"$set": {"value": value}}, upsert=True)


async def _load_advocate_flags():
    """起動時に自動弁護フラグを in-memory キャッシュへ読み込む。"""
    global _advocate_auto_armed
    try:
        await client.wait_until_ready()
        _advocate_auto_armed = await _get_flag("advocate_auto", False)
        print(f"[INFO] 自動弁護モード: {'ON' if _advocate_auto_armed else 'OFF'}")
    except Exception as e:
        print(f"[WARN] _load_advocate_flags失敗: {e}")


async def _recent_channel_text(channel, before=None, limit: int = 10) -> str:
    """直近の人間の発言を古い順テキストで返す（弁護・ゲートの文脈用）。"""
    lines = []
    try:
        async for m in channel.history(limit=limit + 4, before=before):
            if m.author.bot:
                continue
            text = re.sub(r"<@!?\d+>", "", m.content).strip()
            if text:
                lines.append(f"{m.author.display_name}: {text}")
            if len(lines) >= limit:
                break
    except Exception as e:
        print(f"[WARN] _recent_channel_text失敗: {e}")
    lines.reverse()
    return "\n".join(lines)


ADVOCATE_PROMPT = """あなたは、このコミュニティに利害関係を持たない中立的な立場の人物です。
いま「{target}」さんが、管理者や他のメンバーとの間で、不利な立場・対立的な状況に置かれている可能性があります。
あなたの役割は、感情的な対立を鎮め、{target} さんの「側の言い分・事情」を公平に代弁することです。

【厳守する原則（人格や口調の演出より優先）】
- あなたは中立な"裁定者"ではなく、{target} さんのための"弁護人"です。「どちらが正しいか」を断定してはいけません。「{target} さんの側には、こういう見方・事情・善意があり得る」という形で提示してください。
- 事実を捏造してはいけません。{target} さんが過去にした具体的な行動・発言・貢献を、確証なく事実であるかのように作り出すことは固く禁止します。下の【場の流れ】に書かれていないことを、起きた事実として断定しないでください。
- 守る根拠は「一般的な原則・公平さ・手続きの正しさ・善意の推定・別の解釈の可能性・感情への配慮」に置いてください。具体的な事実の主張ではなく、筋論と公平さで擁護してください。
- 管理者や相手個人を攻撃・侮辱してはいけません。相手の立場も尊重しつつ、{target} さんの側の見方を冷静に示してください。
- 対立を煽らず、むしろ双方が少し冷静になれるような、落ち着いた敬体（です・ます調）で。2〜4文程度。
- 前置き・署名・「弁護人:」等のラベルは付けず、本文だけを出力してください。
{situation_block}
【場の流れ（実際に観測された会話。これ以外を起きた事実として作ってはいけない）】
{channel_context}
"""


async def _generate_advocacy(target: discord.Member, channel, situation: str = "", before=None) -> str | None:
    """対象メンバーを弁護する中立トーンの文章を生成（人格は脱ぐ）。"""
    channel_context = await _recent_channel_text(channel, before=before, limit=10) or "（直近の会話は取得できませんでした）"
    situation_block = (
        f"\n【依頼者から伝えられた状況（未確認の主張。事実として断定せず、攻撃の言葉をそのまま繰り返すな）】\n{situation.strip()}\n"
        if situation and situation.strip() else ""
    )
    prompt = ADVOCATE_PROMPT.format(
        target=target.display_name,
        situation_block=situation_block,
        channel_context=channel_context,
    )
    text = await _run_ai_booster(prompt)
    if not text or "（" in text:
        return None
    return text.strip()


CONFLICT_GATE_PROMPT = """以下はDiscordの会話の流れです。最後の方で、管理者「{admin}」が「{target}」さんに向けて発言しています。

これは「管理者が一方的に {target} さんを責めている、または不利な立場に追い込んでいる対立的な状況」ですか？
- 単なる軽い注意・冗談・雑談・すでに和解しているやり取り、または {target} さんが明らかに荒らし・誹謗中傷をしている場合は「NO」。
- {target} さんの側にも言い分・事情がありそうな、対立や摩擦の状況なら「YES」。

YES か NO の一語だけで答えてください。

【会話の流れ】
{context}
"""


async def _maybe_auto_advocate(message: discord.Message):
    """自動弁護: 管理者が非管理者メンバーを一方的に責める対立を保守的に検知して割り込む。
    起点は『管理者の発言』＋『非管理者メンバーへのメンション/返信』＋『対立語フィルタ』＋『LLMゲートYES』。"""
    global _advocate_auto_last
    try:
        if not _advocate_auto_armed:
            return
        author = message.author
        if not isinstance(author, discord.Member) or not author.guild_permissions.administrator:
            return  # 起点は管理者の発言のみ（管理人 vs メンバーの構図）

        # 対象 = メンション or 返信先の「非管理者・非bot」メンバー
        target: discord.Member | None = None
        for u in message.mentions:
            if isinstance(u, discord.Member) and not u.bot and not u.guild_permissions.administrator and u.id != author.id:
                target = u
                break
        if target is None and message.reference is not None:
            ref = message.reference.resolved
            if isinstance(ref, discord.Message) and isinstance(ref.author, discord.Member):
                ra = ref.author
                if not ra.bot and not ra.guild_permissions.administrator and ra.id != author.id:
                    target = ra
        if target is None:
            return

        if not any(w in message.content for w in CONFLICT_HINT_WORDS):
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        if _advocate_auto_last and (now - _advocate_auto_last).total_seconds() < ADVOCATE_AUTO_CD_SEC:
            return
        last_t = _advocate_auto_cd.get(str(target.id))
        if last_t and (now - last_t).total_seconds() < ADVOCATE_AUTO_GAP_MIN * 60:
            return

        # ゲート評価の連打抑制: 同一チャンネルで直近に評価していればスキップ（NO連発のコストを抑える）
        gate_last = _advocate_gate_cd.get(message.channel.id)
        if gate_last and (now - gate_last).total_seconds() < ADVOCATE_GATE_CD_SEC:
            return
        _advocate_gate_cd[message.channel.id] = now

        context = await _recent_channel_text(message.channel, before=None, limit=12)
        if not context:
            return
        gate = await _run_ai_booster(CONFLICT_GATE_PROMPT.format(
            admin=author.display_name, target=target.display_name, context=context,
        ))
        # 「YES」一語のみを採用（"YESとは言えません" 等の誤発火を防ぐ）。判定不能は fail-safe で発火しない。
        g = (gate or "").strip().upper()
        if not (g.startswith("YES") and len(g) <= 6):
            return

        text = await _generate_advocacy(
            target, message.channel,
            situation="この場で管理者が対象メンバーに強い言葉を向けています（自動検知）。",
        )
        if not text:
            return

        _advocate_auto_last = now
        _advocate_auto_cd[str(target.id)] = now
        await message.channel.send(
            f"{ADVOCATE_ICON} {text}\n-# 利害のない立場からの代弁です（自動・管理人がオフにできます: /弁護モード）"
        )
        try:
            await system_col.insert_one({
                "type": "advocate_log", "mode": "auto",
                "target_id": str(target.id), "target_name": target.display_name,
                "admin_id": str(author.id), "admin_name": author.display_name,
                "channel_id": str(message.channel.id),
                "at": now.isoformat(),
            })
        except Exception:
            pass
        print(f"[advocate/auto] {target.display_name} を弁護")
    except Exception as e:
        print(f"[ERROR] _maybe_auto_advocate: {e}")


@client.tree.command(name="弁護", description="利害のない立場から、指定メンバーの側の言い分を公平に代弁する")
@app_commands.describe(member="弁護してほしいメンバー", context="状況・経緯（任意・あると精度が上がる）")
async def advocate_cmd(interaction: discord.Interaction, member: discord.Member, context: str = ""):
    if not await check_home_guild(interaction):
        return
    # 透明なマスタースイッチ: 管理人が全体停止している間は公然と断る（こっそり個別検閲はしない）
    if not await _get_flag("advocate_manual", True):
        await interaction.response.send_message(
            "弁護機能は現在、管理人によって停止されています。", ephemeral=True
        )
        return
    if member.bot:
        await interaction.response.send_message("botは弁護できません。", ephemeral=True)
        return
    # クールダウン（チャンネル単位・スパム防止）
    now = datetime.datetime.now(datetime.timezone.utc)
    last = _advocate_cd.get(interaction.channel_id)
    if last and (now - last).total_seconds() < ADVOCATE_CD_SECONDS:
        remain = int(ADVOCATE_CD_SECONDS - (now - last).total_seconds())
        await interaction.response.send_message(
            f"弁護はこのチャンネルでは連続で使えません。あと約{remain}秒お待ちください。", ephemeral=True
        )
        return
    _advocate_cd[interaction.channel_id] = now

    await interaction.response.defer(ephemeral=False)
    text = await _generate_advocacy(member, interaction.channel, situation=context)
    if not text:
        await interaction.followup.send("（うまく言葉がまとまりませんでした…少し時間を置いて再度お試しください）", ephemeral=True)
        return
    await asyncio.sleep(_rate_get_wait_seconds())
    await interaction.followup.send(
        f"{ADVOCATE_ICON} **{member.display_name} さんの弁護**\n{text}\n-# 利害のない立場からの代弁です。事実認定ではありません。"
    )
    try:
        await system_col.insert_one({
            "type": "advocate_log", "mode": "manual",
            "target_id": str(member.id), "target_name": member.display_name,
            "invoker_id": str(interaction.user.id), "invoker_name": interaction.user.display_name,
            "channel_id": str(interaction.channel_id),
            "context": context[:300],
            "at": now.isoformat(),
        })
    except Exception:
        pass


@client.tree.command(name="弁護モード", description="【管理者】自動弁護の武装/手動弁護の全体ON-OFFを切り替える")
@app_commands.describe(auto="自動弁護を武装するか（対立を検知して自動で割り込む）", manual="手動 /弁護 コマンド自体を有効にするか")
@app_commands.default_permissions(administrator=True)
async def advocate_mode_cmd(interaction: discord.Interaction, auto: bool | None = None, manual: bool | None = None):
    if not await check_home_guild(interaction):
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者専用です。", ephemeral=True)
        return
    global _advocate_auto_armed
    changed = []
    if auto is not None:
        await _set_flag("advocate_auto", auto)
        _advocate_auto_armed = auto
        changed.append(f"自動弁護: **{'ON（武装）' if auto else 'OFF'}**")
    if manual is not None:
        await _set_flag("advocate_manual", manual)
        changed.append(f"手動 /弁護: **{'有効' if manual else '停止'}**")

    cur_auto   = _advocate_auto_armed
    cur_manual = await _get_flag("advocate_manual", True)
    status = (
        f"現在の設定\n"
        f"・自動弁護（対立検知で自動割り込み）: **{'ON' if cur_auto else 'OFF'}**\n"
        f"・手動 /弁護 コマンド: **{'有効' if cur_manual else '停止'}**"
    )
    body = ("✅ 更新しました\n" + "\n".join(changed) + "\n\n" + status) if changed else status
    await interaction.response.send_message(body, ephemeral=True)


async def _generate_resuba(target: discord.Member, topic: str = "",
                           reply_to: str | None = None,
                           transcript: list | None = None) -> str | None:
    """論破人格でレスバ文を生成（お遊び）。
    transcript=ログ: 進行中レスバの続き＝これまでの往復を踏まえ直前の主張に正面から反論（追撃v2）。
    reply_to=本文: 相手の直近発言に噛みつく反応式（旧経路・後方互換）。
    どちらもNone: こちらから議論を仕掛ける一撃（/resuba）。
    claims＋相手のBig Fiveで煽り方をチューニングする。"""
    persona = PERSONALITIES["angry"]
    doc = await users_col.find_one(
        {"_id": str(target.id)},
        {"claims": {"$slice": -8}, "profile.bigfive_self": 1, "profile.bigfive": 1},
    ) or {}
    claims = doc.get("claims", [])
    prof   = doc.get("profile") or {}
    bf_dir = _resuba_bf_directive(prof.get("bigfive_self") or prof.get("bigfive") or {})

    if transcript:
        convo = "\n".join(
            f"{'相手' if m['role'] == 'user' else 'あなた(論破メイド)'}: {m['content']}"
            for m in transcript[-RESUBA_TRANSCRIPT_MAX:]
        )
        topic_line = (f"このレスバのお題は「{topic.strip()}」。この論点から大きく外れないこと。\n"
                      if topic and topic.strip() else "")
        directive = (
            "以下は『あなた(論破メイド)』と『相手』の進行中レスバの経過です。\n"
            f"--- これまでのやり取り ---\n{convo}\n--- ここまで ---\n"
            f"{topic_line}"
            "このやり取りを踏まえ、相手の【直前の発言】の具体的な主張・論理の穴・前提の甘さ・"
            "矛盾に正面から反論してください。論点をずらさず、前のやり取りと噛み合わせること"
            "（同じ煽りの繰り返しは禁止・話を前に進める）。"
            "あくまで知的なレスバ・お遊びの範囲で、容姿・人格の全否定など本気で傷つける罵倒は禁止。"
            "前置きやラベルなし・本文のみ・120文字以内。"
        )
        history_note = "（これは進行中レスバの続き。上のやり取りに噛み合わせて反論）"
    elif reply_to:
        directive = (
            f"「{target.display_name}」がたった今こう言いました：「{reply_to[:200]}」\n"
            "この発言に対して、論理の穴・前提の甘さ・矛盾を突いて鋭く反論してください。"
            "あくまで知的なレスバ・お遊びの範囲で、容姿・人格の全否定など本気で傷つける罵倒は禁止。"
            "前置きやラベルなし・本文のみ・120文字以内。"
        )
        history_note = "（これは相手の直近発言へのレスバ反応）"
    else:
        topic_clause = (
            f"お題は「{topic.strip()}」。この話題で議論を仕掛けろ。"
            if topic and topic.strip()
            else "お題は自由。相手が言いそうなこと・最近の話題から、議論になりそうな論点を一つ選んで仕掛けろ。"
        )
        directive = (
            f"これは誰かへの返信ではなく、あなたから「{target.display_name}」に自分から仕掛けるレスバ（言葉の打ち合い）です。\n"
            f"{topic_clause}\n"
            "相手を軽く挑発しつつ、反論したくなる論点を一つ投げて議論を吹っかけてください。"
            "ただし容姿・人格の全否定など本気で傷つける罵倒は禁止。あくまで知的なレスバ・お遊びの範囲で。"
            "前置きや「レスバ:」等のラベルなし・本文のみ・150文字以内。"
        )
        history_note = "（これは新たに仕掛けるレスバ。過去の個別会話履歴は使いません）"
    if bf_dir:
        directive += "\n【相手の性格への配慮】" + bf_dir

    base = persona["booster_prompt"].format(
        name=target.display_name,
        history=history_note,
        content=directive,
    )
    if claims:
        claim_lines = "\n".join(f"・{c['content']}" for c in claims)
        prompt = f"【{target.display_name} が過去に言った主張（隙があれば突いてよい・無理に使わなくてよい）】\n{claim_lines}\n\n---\n{base}"
    else:
        prompt = base
    text = await _run_ai_booster(prompt, chain=RESUBA_CHAIN)   # レスバはgemma主の専用経路
    # 全モデル失敗時の sentinel「（メイドは今、席を外しております…）」だけを弾く。
    # 旧 '"（" in text' は「（笑）」等の正当な全角括弧で誤爆していたので厳密一致に修正。
    if not text or text.startswith("（メイド"):
        return None
    return text.strip()


# =============================================================================
# レスバ追撃v2セッション（双方向レスバ・mimicのreact基盤と対称）
#   対象が発言→これまでのtranscriptを踏まえ論破メイドが噛み合わせて反論。CD窓内の発言はコアレスで束ねて
#   1回返す（取りこぼしゼロ）。続く限り続き、沈黙TTL/レスバ停止/レスバ拒否で終了→終了時にレスバ判定。
#   pile-on防止: 対象が返し続ける=合意のため上限撤廃だが、CD/optout/TTL/横断1セッションは維持。
# =============================================================================
_resuba_sessions: dict[int, dict] = {}   # channel_id -> session
RESUBA_SESS_CD        = 6    # 反論の最小間隔＝コアレス窓（秒）。gemma主＋短CDでテンポ重視。
                             # ※混雑時はグローバル12RPM limiter(_rate_get_wait_seconds)が6〜20秒の待ちを
                             #   自動付与＝短CDが効くのは静かな1対1時のみ／混雑時は自動ブレーキで安全。
RESUBA_SESS_TTL       = 600  # 沈黙でのセッション自動終了（秒）
RESUBA_TRANSCRIPT_MAX = 20   # セッションが保持する発言数の上限（直近10往復ぶん＝健忘症解消の文脈窓）


def _resuba_push(session: dict, role: str, name: str, content: str) -> None:
    """レスバの会話ログにappendして直近 RESUBA_TRANSCRIPT_MAX 件にcap（健忘症解消の中核）。
    role は "user"(対象) か "maid"(論破メイド)。on_message/生成後から同期で呼ぶ。"""
    t = session.setdefault("transcript", [])
    t.append({"role": role, "name": name, "content": content[:300]})
    if len(t) > RESUBA_TRANSCRIPT_MAX:
        del t[:-RESUBA_TRANSCRIPT_MAX]


async def _resuba_react(channel, session: dict, pending: list[str]):
    """溜まった対象発言（pending）に、これまでのtranscriptを踏まえて1回反論する。
    transcript には pending も既に積まれている（on_message で同期append済）ので、
    生成には transcript 全体を渡す。引数 pending は「今回束ねた発言」の記録用で生成には未使用
    （load-bearingではない）。ボットの反論は生成後に追記する。pingはしない。"""
    try:
        guild  = getattr(channel, "guild", None)
        target = guild.get_member(int(session["target_uid"])) if guild else None
        if target is None:
            return
        text = await _generate_resuba(target, topic=session.get("topic", ""),
                                      transcript=session.get("transcript", []))
        if not text:
            return
        _resuba_push(session, "maid", "論破メイド", text)   # 反論もログへ（往復が噛み合う）
        await asyncio.sleep(_rate_get_wait_seconds())
        await channel.send(f"{RESUBA_ICON} {text}")
    except Exception as e:
        print(f"[ERROR] _resuba_react: {e}")


async def _resuba_flush(channel, session: dict):
    """1セッション1ワーカーで pending を排出するループ（コアレス＝取りこぼしゼロ・並行リアクト無し）。
    on_message は flush_scheduled が False の時だけ本タスクを起動し、本タスクは drain し切るまで True を
    保持し続ける＝CD=6s＋低速gemmaで生成が長引いても、同一セッションで2本目の生成は決して走らない。
    各周回: CD窓ぶん待つ→pending を束ねて1回反論→reactは逐次（直列）。react中に積まれた新pendingは次周回で拾う。"""
    try:
        while True:
            last = session.get("last_reply_at")
            if last is not None:
                elapsed = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds()
                if elapsed < RESUBA_SESS_CD:
                    await asyncio.sleep(RESUBA_SESS_CD - elapsed)
            # 停止/optout/TTLで消えた or 排出完了 → flag を下ろして終了。
            # ★この判定から flag クリアまで await を挟まない＝check-then-act レース無し（取りこぼし無し）。
            if _resuba_sessions.get(channel.id) is not session or not session.get("pending"):
                session["flush_scheduled"] = False
                return
            pending = session["pending"]
            session["pending"]       = []                                           # ★発火前にバッファ確保
            session["last_reply_at"] = datetime.datetime.now(datetime.timezone.utc)  # ★発火前にCD更新
            await _resuba_react(channel, session, pending)   # 直列。完了後ループ先頭へ→残りを排出
    except Exception as e:
        print(f"[ERROR] _resuba_flush: {e}")
        session["flush_scheduled"] = False


async def _resuba_judge(channel, session: dict):
    """セッション終了時、中立の審判視点で勝敗と短評を出す（ストレッチ）。
    論破人格ではなく素の中立トーン。やり取りが薄い（往復未成立）場合は判定しない。"""
    transcript = session.get("transcript", [])
    if sum(1 for m in transcript if m["role"] == "user") < 2:
        return   # 対象がほぼ反論していない＝レスバ未成立。判定しない
    target_name = session.get("target_name", "相手")
    convo = "\n".join(
        f"{target_name if m['role'] == 'user' else '論破メイド'}: {m['content']}"
        for m in transcript
    )
    prompt = (
        "あなたは中立公平なレスバの審判です。煽らず、どちらにも肩入れしない素の口調で判定します。\n"
        f"以下は『論破メイド』と『{target_name}』のレスバ（言葉の打ち合い）の全記録です。\n"
        f"--- 記録 ---\n{convo}\n--- ここまで ---\n"
        "論理の一貫性・反論の的確さ・説得力で総合的に判定し、"
        f"①勝者（「論破メイド」「{target_name}」「引き分け」のいずれか）"
        "②その理由を2〜3行で公平に述べてください。前置き・ラベルなし・本文のみ・150文字以内。"
    )
    try:
        verdict = await _run_ai_booster(prompt, chain=RESUBA_CHAIN)   # 判定もgemma主経路
        if verdict and not verdict.startswith("（メイド"):
            await asyncio.sleep(_rate_get_wait_seconds())
            await channel.send(f"⚖️ **レスバ判定**\n{verdict.strip()}")
    except Exception as e:
        print(f"[ERROR] _resuba_judge: {e}")


async def _resuba_finish(channel, session: dict, note: str):
    """セッション終了の共通処理：終了メッセージ＋レスバ判定。
    呼び出し側で _resuba_sessions から pop 済みであること。channel が None なら判定スキップ。"""
    if channel is None:
        return
    try:
        if note:
            await channel.send(f"{RESUBA_ICON} {note}")
    except Exception:
        pass
    await _resuba_judge(channel, session)


@client.tree.command(name="resuba", description="論破メイドが指定メンバーにレスバを仕掛ける（お遊び）")
@app_commands.describe(member="レスバを仕掛ける相手", topic="お題（任意）")
async def resuba_cmd(interaction: discord.Interaction, member: discord.Member, topic: str = ""):
    if not await check_home_guild(interaction):
        return
    if member.bot or member.id == interaction.client.user.id:
        await interaction.response.send_message("botにはレスバを仕掛けられません。", ephemeral=True)
        return
    # 対象のオプトアウト確認（巻き込まれたくない人を守る）
    tdoc = await users_col.find_one({"_id": str(member.id)}, {"resuba_optout": 1}) or {}
    if tdoc.get("resuba_optout"):
        await interaction.response.send_message(
            f"{member.display_name} さんはレスバ対象外に設定しています。", ephemeral=True
        )
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    last = _resuba_cd.get(interaction.channel_id)
    if last and (now - last).total_seconds() < RESUBA_CD_SECONDS:
        remain = int(RESUBA_CD_SECONDS - (now - last).total_seconds())
        await interaction.response.send_message(
            f"レスバはこのチャンネルでは連続で使えません。あと約{remain}秒お待ちください。", ephemeral=True
        )
        return
    _resuba_cd[interaction.channel_id] = now

    await interaction.response.defer(ephemeral=False)
    text = await _generate_resuba(member, topic)
    if not text:
        await interaction.followup.send("（今はうまく仕掛けられませんでした…）", ephemeral=True)
        return
    await asyncio.sleep(_rate_get_wait_seconds())
    persona = PERSONALITIES["angry"]
    await interaction.followup.send(f"{persona['icon']} {member.mention} {text}")


@client.tree.command(name="レスバ追撃",
                     description="論破メイドが対象と噛み合う双方向レスバを始める（ブースター/管理者）")
@app_commands.describe(member="追撃する相手", topic="お題（任意）")
async def resuba_chase_cmd(interaction: discord.Interaction, member: discord.Member,
                           topic: str = ""):
    if not await check_home_guild(interaction):
        return
    # 持続追撃は影響が大きいので開始はブースター/管理者に限定（一撃の /resuba は全員可のまま）
    is_booster = any(r.id == BOOSTER_ROLE_ID for r in interaction.user.roles)
    is_admin   = getattr(interaction.user.guild_permissions, "administrator", False)
    if not (is_booster or is_admin):
        await interaction.response.send_message(
            "追撃レスバ（持続）はブースター/管理者専用だよ。一撃なら /resuba をどうぞ。", ephemeral=True)
        return
    if member.bot or member.id == interaction.client.user.id:
        await interaction.response.send_message("botは追撃対象にできません。", ephemeral=True)
        return
    tdoc = await users_col.find_one({"_id": str(member.id)}, {"resuba_optout": 1}) or {}
    if tdoc.get("resuba_optout"):
        await interaction.response.send_message(
            f"{member.display_name} さんはレスバ対象外に設定しています。", ephemeral=True)
        return
    if interaction.channel_id in _resuba_sessions:
        await interaction.response.send_message(
            "このチャンネルでは既に追撃が進行中です。`/レスバ停止` で止めてね。", ephemeral=True)
        return
    # pile-on防止: 同一対象を複数チャンネルで同時追撃させない（チャンネル横断で1人1セッション）
    if any(s.get("target_uid") == str(member.id) for s in _resuba_sessions.values()):
        await interaction.response.send_message(
            f"{member.display_name} さんは既に別のチャンネルで追撃中だよ。終わってから/止めてからにしてね。",
            ephemeral=True)
        return

    session = {
        "target_uid":  str(member.id),
        "target_name": member.display_name,
        "topic":       topic,
        "last_reply_at": None,
        "invoker":     str(interaction.user.id),
        "transcript":  [],     # [{"role":"user"/"maid","name":str,"content":str}] 健忘症解消の文脈窓
        "pending":     [],     # CD窓内に溜まった対象発言（コアレスして1回で返す）
        "flush_scheduled": False,
    }
    _resuba_sessions[interaction.channel_id] = session
    # 開始の一度だけ ping（以降の反論は ping しない＝通知連打=嫌がらせ化を防ぐ）
    await interaction.response.send_message(
        f"{RESUBA_ICON} {member.mention} 覚悟しろ。お前が言い返す限り、噛み合わせて議論してやる。\n"
        f"（沈黙{RESUBA_SESS_TTL // 60}分・`/レスバ停止`・対象本人の`/レスバ拒否`で終了。終了時にレスバ判定を出す）")

    _started_at = datetime.datetime.now(datetime.timezone.utc)

    async def _auto_end():
        # 沈黙TTL監視: 最後の往復から TTL 経過したら終了。会話が続けば last_reply_at が更新されるので、
        # その都度残り時間を測り直して延命する（＝「沈黙10分で終了」を正しく満たす）。
        while _resuba_sessions.get(interaction.channel_id) is session:
            last = session.get("last_reply_at") or _started_at
            idle = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds()
            if idle >= RESUBA_SESS_TTL:
                break
            await asyncio.sleep(RESUBA_SESS_TTL - idle)
        if _resuba_sessions.get(interaction.channel_id) is session:
            _resuba_sessions.pop(interaction.channel_id, None)
            await _resuba_finish(interaction.channel, session, "レスバ、時間切れで終了。")
    asyncio.create_task(_auto_end())


@client.tree.command(name="レスバ停止", description="進行中の追撃レスバセッションを停止")
async def resuba_stop_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    sess = _resuba_sessions.pop(interaction.channel_id, None)
    if sess:
        await interaction.response.send_message(
            f"{RESUBA_ICON} **{sess['target_name']}** へのレスバを停止しました。")
        await _resuba_judge(interaction.channel, sess)   # 停止時はレスバ判定を出す
    else:
        await interaction.response.send_message("進行中のレスバはありません。", ephemeral=True)


@client.tree.command(name="レスバ拒否", description="自分をレスバの対象外にする/戻すを切り替える")
async def resuba_optout_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    uid = str(interaction.user.id)
    doc = await users_col.find_one({"_id": uid}, {"resuba_optout": 1}) or {}
    new_val = not bool(doc.get("resuba_optout"))
    await users_col.update_one({"_id": uid}, {"$set": {"resuba_optout": new_val}}, upsert=True)
    if new_val:
        # 進行中の自分への追撃セッションを即停止（pile-on防止の要・途中拒否で即終了）
        killed = 0
        for ch_id, s in list(_resuba_sessions.items()):
            if s.get("target_uid") == uid:
                _resuba_sessions.pop(ch_id, None)
                killed += 1
        extra = f"（進行中の追撃{killed}件も停止したよ）" if killed else ""
        await interaction.response.send_message(
            f"✅ あなたをレスバ対象外に設定しました。/resuba・/レスバ追撃 の対象になりません。{extra}",
            ephemeral=True)
    else:
        await interaction.response.send_message(
            "✅ レスバ対象外を解除しました。再び /resuba の対象になります。", ephemeral=True)


# =============================================================================
# 入場クイズゲート（寛容ハードゲート）
#   新規参加→未認証ロール付与→#入場クイズの常設ボタン→ephemeralクイズ→全問正解で解放。
#   安全: ①既存/出戻り(DB実績あり)・bot・管理者にはロールを付けない ②未認証=閲覧不可の権限上書きは
#   ロール保持者にしか効かない＝既存メンバー無影響 ③kill-switch(enabled)/手動解放(/認証許可)/原状復帰。
#   設定はDB(system.quiz_gate)保存＝envや再デプロイ不要・別サーバー移転もコマンドだけで完結。
# =============================================================================
_quiz_gate: dict = {"enabled": False, "role_id": 0, "channel_id": 0}
QUIZ_CHANNEL_NAME = "入場クイズ"
QUIZ_QUESTIONS = [
    {"q": "自分の個人情報（本名・住所・学校など）をサーバーに書き込むのは？",
     "options": ["OK", "ダメ"], "answer": 1,
     "why": "自分の情報でも晒すのは禁止だよ（🟡）。"},
    {"q": "他サーバーへメンバーを引き抜く・勧誘するのは？",
     "options": ["OK", "ダメ"], "answer": 1,
     "why": "メンバーの引き抜き行為は禁止（🔴）。"},
    {"q": "ルール違反を見かけたら、まずどうする？",
     "options": ["一緒にやる", "通報する", "無視する"], "answer": 1,
     "why": "「通報はこちらから」へ通報してね（努力義務）。"},
]


async def _load_quiz_gate():
    """起動時にDBから入場クイズ設定を読み込む。"""
    try:
        doc = await system_col.find_one({"_id": "quiz_gate"}) or {}
        _quiz_gate["enabled"]    = bool(doc.get("enabled", False))
        _quiz_gate["role_id"]    = int(doc.get("role_id", 0) or 0)
        _quiz_gate["channel_id"] = int(doc.get("channel_id", 0) or 0)
        print(f"[INFO] 入場クイズ設定: enabled={_quiz_gate['enabled']} "
              f"role={_quiz_gate['role_id']} ch={_quiz_gate['channel_id']}")
    except Exception as e:
        print(f"[WARN] _load_quiz_gate: {e}")


async def _save_quiz_gate():
    await system_col.update_one({"_id": "quiz_gate"}, {"$set": {
        "enabled": _quiz_gate["enabled"], "role_id": _quiz_gate["role_id"],
        "channel_id": _quiz_gate["channel_id"],
    }}, upsert=True)


def _quiz_embed(idx: int, wrong_why: str | None = None) -> discord.Embed:
    q = QUIZ_QUESTIONS[idx]
    desc = q["q"] if not wrong_why else f"❌ 惜しい！{wrong_why}\nもう一度どうぞ！\n\n{q['q']}"
    e = discord.Embed(title=f"🎫 入場クイズ（{idx+1}/{len(QUIZ_QUESTIONS)}）",
                      description=desc, color=0x00C2A8)
    e.set_footer(text="ルール確認だよ。落ちることはないから安心してね")
    return e


class QuizRunView(discord.ui.View):
    """ephemeralで1人ずつ進めるクイズ本体（寛容＝不正解でも正解理由を見せて再挑戦）。"""
    def __init__(self, idx: int = 0):
        super().__init__(timeout=300)
        self.idx = idx
        self._render()

    def _render(self):
        self.clear_items()
        for i, opt in enumerate(QUIZ_QUESTIONS[self.idx]["options"]):
            btn = discord.ui.Button(label=opt, style=discord.ButtonStyle.primary)
            btn.callback = self._make_cb(i)
            self.add_item(btn)

    def _make_cb(self, choice: int):
        async def cb(interaction: discord.Interaction):
            q = QUIZ_QUESTIONS[self.idx]
            if choice != q["answer"]:
                await interaction.response.edit_message(
                    embed=_quiz_embed(self.idx, wrong_why=q["why"]), view=self)
                return
            self.idx += 1
            if self.idx >= len(QUIZ_QUESTIONS):
                await _grant_quiz_pass(interaction)
                return
            self._render()
            await interaction.response.edit_message(embed=_quiz_embed(self.idx), view=self)
        return cb


async def _grant_quiz_pass(interaction: discord.Interaction):
    """全問正解→未認証ロールを外して解放。"""
    member = interaction.user
    role = interaction.guild.get_role(_quiz_gate["role_id"]) if interaction.guild else None
    try:
        if role and isinstance(member, discord.Member) and role in member.roles:
            await member.remove_roles(role, reason="入場クイズ正解")
    except Exception as e:
        print(f"[ERROR] _grant_quiz_pass: {e}")
    await interaction.response.edit_message(
        content="✅ 正解！ようこそ🎉 サーバーが解放されたよ。楽しんでね！", embed=None, view=None)


class QuizStartView(discord.ui.View):
    """#入場クイズ に常設するスタートボタン（永続View・custom_idで再起動後も有効）。"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="クイズを始める", style=discord.ButtonStyle.success,
                       custom_id="quiz_gate_start", emoji="🎫")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        role = interaction.guild.get_role(_quiz_gate["role_id"]) if interaction.guild else None
        if not role or not (isinstance(member, discord.Member) and role in member.roles):
            await interaction.response.send_message("もう認証済みだよ！ようこそ🎉", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=_quiz_embed(0), view=QuizRunView(0), ephemeral=True)


@client.event
async def on_member_join(member: discord.Member):
    """新規参加者に未認証ロールを付与してクイズへ誘導。既存/出戻り・bot・管理者は対象外。"""
    try:
        if not _quiz_gate.get("enabled") or not _quiz_gate.get("role_id"):
            return
        if member.bot or member.guild_permissions.administrator:
            return
        # 既存/出戻りメンバー（DBに活動実績あり）はゲートしない＝締め出さない
        doc = await users_col.find_one({"_id": str(member.id)}, {"xp": 1, "butler_history": 1})
        if doc and (doc.get("xp", 0) > 0 or doc.get("butler_history")):
            return
        role = member.guild.get_role(_quiz_gate["role_id"])
        if not role:
            return
        await member.add_roles(role, reason="入場クイズ: 未認証")
        ch = member.guild.get_channel(_quiz_gate["channel_id"])
        if ch:
            try:
                await ch.send(f"{member.mention} ようこそ！下の「🎫 クイズを始める」から認証してね。")
            except Exception:
                pass
    except Exception as e:
        print(f"[ERROR] on_member_join: {e}")


def _quiz_is_admin(interaction: discord.Interaction) -> bool:
    return getattr(interaction.user.guild_permissions, "administrator", False)


@client.tree.command(name="認証ゲート設定",
                     description="【管理者】入場クイズゲートを設定（ch作成＋権限適用＋ボタン設置）")
@app_commands.describe(role="未認証ロール（新規参加者に自動付与される制限ロール）")
@app_commands.default_permissions(administrator=True)
async def quiz_setup_cmd(interaction: discord.Interaction, role: discord.Role):
    if not await check_home_guild(interaction):
        return
    if not _quiz_is_admin(interaction):
        await interaction.response.send_message("管理者専用だよ。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    # ① クイズch（既存名を探す→無ければ作成。@everyone可視・未認証も可視・発言不可）
    ch = discord.utils.get(guild.text_channels, name=QUIZ_CHANNEL_NAME)
    if ch is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                              read_message_history=True),
        }
        ch = await guild.create_text_channel(QUIZ_CHANNEL_NAME, overwrites=overwrites,
                                             reason="入場クイズch")
    else:
        await ch.set_permissions(role, view_channel=True, send_messages=False,
                                 read_message_history=True)
    # ② 全カテゴリ＋全チャンネルに「未認証=閲覧不可」を適用（同期/非同期の漏れを防ぐためch個別にも）。
    #    上書きは未認証ロール保持者にしか効かない＝既存メンバーは無影響。
    applied = 0
    for c in guild.channels:
        if c.id == ch.id:
            continue
        try:
            await c.set_permissions(role, view_channel=False, reason="入場クイズゲート")
            applied += 1
        except Exception as e:
            print(f"[WARN] quiz overwrite {getattr(c, 'name', '?')}: {e}")
    # ③ 常設スタートボタンを設置
    embed = discord.Embed(
        title="🎫 入場認証クイズ",
        description=("ようこそ！遊ぶ前に、かんたんなルール確認クイズに答えてね。\n"
                     "下のボタンから始められるよ（**落ちることはない**から安心して。全問正解で解放）。"),
        color=0x00C2A8)
    await ch.send(embed=embed, view=QuizStartView())
    # ④ 設定保存（最初はOFF＝即締め出さない）
    _quiz_gate["role_id"]    = role.id
    _quiz_gate["channel_id"] = ch.id
    _quiz_gate["enabled"]    = False
    await _save_quiz_gate()
    await interaction.followup.send(
        f"✅ 設定完了。\nクイズch: {ch.mention}\n権限適用: {applied}箇所\n未認証ロール: {role.mention}\n\n"
        f"現在ゲートは**OFF**。まず自分で `{ch.mention}` のボタンを試して、問題なければ "
        f"`/認証ゲート切替` でONにしてね。", ephemeral=True)


@client.tree.command(name="認証ゲート切替", description="【管理者】入場クイズゲートのON/OFF（kill-switch）")
@app_commands.default_permissions(administrator=True)
async def quiz_toggle_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    if not _quiz_is_admin(interaction):
        await interaction.response.send_message("管理者専用だよ。", ephemeral=True)
        return
    if not _quiz_gate.get("role_id"):
        await interaction.response.send_message("先に `/認証ゲート設定` を実行してね。", ephemeral=True)
        return
    _quiz_gate["enabled"] = not _quiz_gate["enabled"]
    await _save_quiz_gate()
    state = "ON（新規参加者にクイズ必須）" if _quiz_gate["enabled"] else "OFF（誰も制限しない）"
    await interaction.response.send_message(f"🎫 入場クイズゲートを **{state}** にしたよ。", ephemeral=True)


@client.tree.command(name="認証許可", description="【管理者】指定メンバーを手動で認証済みにする（保険）")
@app_commands.describe(member="解放するメンバー")
@app_commands.default_permissions(administrator=True)
async def quiz_pass_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await check_home_guild(interaction):
        return
    if not _quiz_is_admin(interaction):
        await interaction.response.send_message("管理者専用だよ。", ephemeral=True)
        return
    role = interaction.guild.get_role(_quiz_gate.get("role_id", 0))
    if role and role in member.roles:
        await member.remove_roles(role, reason="管理者による手動認証")
        await interaction.response.send_message(f"✅ {member.mention} を認証済みにしたよ。", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"{member.display_name} は未認証ロールを持ってないよ。", ephemeral=True)


@client.tree.command(name="認証ゲート解除",
                     description="【管理者】入場クイズの権限上書きを全て外して原状復帰")
@app_commands.default_permissions(administrator=True)
async def quiz_teardown_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    if not _quiz_is_admin(interaction):
        await interaction.response.send_message("管理者専用だよ。", ephemeral=True)
        return
    role = interaction.guild.get_role(_quiz_gate.get("role_id", 0))
    if not role:
        await interaction.response.send_message("設定が見つからないよ。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    removed = 0
    for c in guild.channels:
        try:
            await c.set_permissions(role, overwrite=None, reason="入場クイズ解除")
            removed += 1
        except Exception:
            pass
    # 念のため全員から未認証ロールを剥がす（誰も詰まらせない）
    stripped = 0
    for m in list(role.members):
        try:
            await m.remove_roles(role, reason="入場クイズ解除")
            stripped += 1
        except Exception:
            pass
    _quiz_gate["enabled"] = False
    await _save_quiz_gate()
    await interaction.followup.send(
        f"✅ 原状復帰。権限上書き解除: {removed}箇所 / 未認証剥がし: {stripped}人 / ゲートOFF。",
        ephemeral=True)


def _build_retro_embeds(doc: dict) -> list[dict]:
    """既存の遡及日報からDiscord Embedリストを構築する"""
    import re as _re2
    summary    = doc.get("summary", "（要約なし）")
    retro_date = doc.get("retro_date", "")
    msg_count  = doc.get("message_count", 0)
    now_jst    = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    ICONS = {
        "全体の雰囲気": "🌡️", "主なトピック": "📌", "感情の波": "🎢",
        "注目の発言": "💬", "今日の内輪ネタ": "🔑", "メンバーの人間関係": "🤝",
        "ユーザーの感情状態": "😊", "直近の話題": "🔥", "会話の特徴": "✨",
    }
    parts = _re2.split(r"\n##\s+", "\n" + summary)
    sections = []
    for part in parts:
        if not part.strip():
            continue
        split = part.strip().split("\n", 1)
        title = split[0].strip()
        body  = split[1].strip() if len(split) > 1 else ""
        if title and body:
            sections.append((title, body))
    fields = []
    for title, body in sections:
        icon = next((v for k, v in ICONS.items() if k in title), "📋")
        if len(body) > 1020:
            body = body[:1017] + "…"
        fields.append({"name": f"{icon} {title}", "value": body, "inline": False})
    fields.append({"name": "📊 集計",
                   "value": f"対象日: {retro_date} / メッセージ数: {msg_count:,}件",
                   "inline": False})
    title_str = f"📜 {retro_date} の過去日報（再掲）"
    embeds, current_fields, current_chars = [], [], 0
    for field in fields:
        fc = len(field["name"]) + len(field["value"])
        if (current_chars + fc > 5800 or len(current_fields) >= 25) and current_fields:
            embed = {"color": 0x8B4513, "fields": current_fields,
                     "title": title_str if not embeds else f"📜 {retro_date} の過去日報（再掲・続き）"}
            embeds.append(embed)
            current_fields, current_chars = [], 0
        current_fields.append(field)
        current_chars += fc
    if current_fields:
        embeds.append({
            "color":  0x8B4513,
            "title":  title_str if not embeds else f"📜 {retro_date} の過去日報（再掲・続き）",
            "fields": current_fields,
            "footer": {"text": f"空気くん遡及日報（再掲） • {now_jst.strftime('%H:%M')} JST"},
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
    return embeds


@client.tree.command(name="retroreport", description="過去の指定日の日報を作成（管理者・ブースター専用）")
@app_commands.describe(
    date="対象日付（例: 2026-03-01）",
    force="既存の日報を無視して強制的に作り直す（壊れた日報の再生成用）",
)
async def retroreport_cmd(interaction: discord.Interaction, date: str, force: bool = False):
    if not await check_home_guild(interaction):
        return
    is_booster = any(r.id == BOOSTER_ROLE_ID for r in interaction.user.roles)
    is_admin   = interaction.user.guild_permissions.administrator
    if not (is_booster or is_admin):
        await interaction.response.send_message("このコマンドは管理者またはブースター専用です。", ephemeral=True)
        return

    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date.strip()):
        await interaction.response.send_message(
            "日付は `YYYY-MM-DD` 形式で入力してください。\n例: `2026-03-01`",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=False)

    # 既存日報チェック → あれば再投稿して終了（force 指定時は無視して再生成）
    existing = await summaries_col.find_one({"retro_date": date.strip()})
    if existing and not force:
        embeds = _build_retro_embeds(existing)
        for i in range(0, len(embeds), 10):
            await interaction.channel.send(embeds=[
                discord.Embed.from_dict(e) for e in embeds[i:i+10]
            ])
        await interaction.followup.send(
            f"📜 **{date}** の日報はすでに作成済みでした。上に再掲しました！",
            ephemeral=True,
        )
        return

    # 既存なし → GitHub Actions をトリガー
    gh_token = os.environ.get("GITHUB_TOKEN")
    gh_repo  = os.environ.get("GITHUB_REPO")
    if not gh_token or not gh_repo:
        await interaction.followup.send(
            "⚠️ GitHub連携が設定されていません。管理者に `GITHUB_TOKEN` と `GITHUB_REPO` の設定を依頼してください。",
            ephemeral=True,
        )
        return

    import aiohttp
    url     = f"https://api.github.com/repos/{gh_repo}/actions/workflows/retro_report.yml/dispatches"
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"ref": "main", "inputs": {"target_date": date.strip()}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 204:
                    note = "（既存日報を上書きで再生成します）" if (existing and force) else ""
                    await interaction.followup.send(
                        f"📜 **{date}** の日報作成を開始しました！{note}\n数分後に日報チャンネルに投稿されます。"
                    )
                else:
                    body = await resp.text()
                    await interaction.followup.send(
                        f"❌ GitHub Actions のトリガーに失敗しました。\n`{resp.status}: {body[:200]}`",
                        ephemeral=True,
                    )
    except Exception as e:
        print(f"[ERROR] retroreport_cmd: {e}")
        await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=True)


@client.tree.command(name="focus", description="特定メンバー or キーワードに絞った要約を作成（管理者・ブースター専用）")
@app_commands.describe(
    member="対象メンバー（@メンションで指定）",
    keyword="対象キーワード（例: Among Us・SNR問題）",
)
async def focus_cmd(
    interaction: discord.Interaction,
    member: discord.Member = None,
    keyword: str = None,
):
    if not await check_home_guild(interaction):
        return
    is_booster = any(r.id == BOOSTER_ROLE_ID for r in interaction.user.roles)
    is_admin   = interaction.user.guild_permissions.administrator
    if not (is_booster or is_admin):
        await interaction.response.send_message("このコマンドは管理者またはブースター専用です。", ephemeral=True)
        return

    if not member and not keyword:
        await interaction.response.send_message(
            "`member` か `keyword` のどちらかを指定してください。", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)

    gh_token = os.environ.get("GITHUB_TOKEN")
    gh_repo  = os.environ.get("GITHUB_REPO")
    if not gh_token or not gh_repo:
        await interaction.followup.send(
            "⚠️ GitHub連携が設定されていません。", ephemeral=True
        )
        return

    # パラメータ構築
    focus_type   = "member" if member else "keyword"
    focus_target = str(member.id) if member else keyword
    focus_name   = member.display_name if member else keyword

    import aiohttp
    url     = f"https://api.github.com/repos/{gh_repo}/actions/workflows/focus_summary.yml/dispatches"
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "ref": "main",
        "inputs": {
            "focus_type":    focus_type,
            "focus_target":  focus_target,
            "focus_name":    focus_name,
            # /focus を実行したチャンネルにレポートを投稿させる
            "focus_channel": str(interaction.channel_id),
        },
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 204:
                    icon = "👤" if focus_type == "member" else "🔍"
                    await interaction.followup.send(
                        f"{icon} **{focus_name}** に絞った要約を作成中です！\n数分後にこのチャンネルに投稿されます。"
                    )
                else:
                    body = await resp.text()
                    await interaction.followup.send(
                        f"❌ 失敗しました。\n`{resp.status}: {body[:200]}`",
                        ephemeral=True,
                    )
    except Exception as e:
        print(f"[ERROR] focus_cmd: {e}")
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


@client.tree.command(name="addnick", description="【管理者専用】ニックネームを登録する")
@app_commands.describe(nickname="登録するニックネーム（例: たろー）", realname="正式名称（例: 山田太郎）")
@app_commands.default_permissions(administrator=True)
async def addnick_cmd(interaction: discord.Interaction, nickname: str, realname: str):
    if not await check_home_guild(interaction):
        return
    doc = await system_col.find_one({"_id": "nickname_map"}) or {"_id": "nickname_map", "map": {}}
    nick_map = doc.get("map", {})
    nick_map[nickname.strip()] = realname.strip()
    await system_col.update_one(
        {"_id": "nickname_map"},
        {"$set": {"map": nick_map}},
        upsert=True,
    )
    await interaction.response.send_message(
        f"✅ `{nickname}` → `{realname}` を登録したよ！",
        ephemeral=True,
    )


@client.tree.command(name="removenick", description="【管理者専用】ニックネームを削除する")
@app_commands.describe(nickname="削除するニックネーム")
@app_commands.default_permissions(administrator=True)
async def removenick_cmd(interaction: discord.Interaction, nickname: str):
    if not await check_home_guild(interaction):
        return
    doc = await system_col.find_one({"_id": "nickname_map"}) or {}
    nick_map = doc.get("map", {})
    if nickname.strip() in nick_map:
        del nick_map[nickname.strip()]
        await system_col.update_one(
            {"_id": "nickname_map"},
            {"$set": {"map": nick_map}},
            upsert=True,
        )
        await interaction.response.send_message(f"✅ `{nickname}` を削除したよ！", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ `{nickname}` は登録されていないよ。", ephemeral=True)


@client.tree.command(name="listnick", description="【管理者専用】登録済みニックネーム一覧を表示")
@app_commands.default_permissions(administrator=True)
async def listnick_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    nick_map = await get_nickname_map()
    if not nick_map:
        await interaction.response.send_message("まだニックネームは登録されていないよ。", ephemeral=True)
        return
    lines = [f"・`{k}` → `{v}`" for k, v in nick_map.items()]
    embed = discord.Embed(title="📝 登録済みニックネーム一覧", color=0x3498DB)
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="summarystatus", description="【管理者専用】最新の要約状態を確認")
@app_commands.default_permissions(administrator=True)
async def summarystatus_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        doc = await summaries_col.find_one(
            {"summary": {"$exists": True},
             "is_retro": {"$ne": True},
             "retro_date": {"$exists": False}},
            sort=[("created_at", -1)]
        )
        if not doc:
            await interaction.followup.send("⚠️ 要約レコードが見つかりません。バッチが未実行の可能性があります。", ephemeral=True)
            return
        summary       = doc.get("summary", "")[:300]
        created_at    = doc.get("created_at", "不明")
        message_count = doc.get("message_count", 0)
        embed = discord.Embed(title="📋 要約ステータス", color=0x00FF88)
        embed.add_field(name="作成日時",          value=created_at,           inline=True)
        embed.add_field(name="対象メッセージ数",   value=f"{message_count}件", inline=True)
        embed.add_field(name="要約（冒頭300字）",  value=summary + "…" if summary else "なし", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


@client.tree.command(name="listmodels", description="【管理者専用】利用可能なGeminiモデル一覧を表示")
@app_commands.default_permissions(administrator=True)
async def listmodels_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        lines = []
        for m in gemini_client.models.list():
            if hasattr(m, 'name'):
                lines.append(f"`{m.name}`")
        if not lines:
            await interaction.followup.send("利用可能なモデルが見つかりませんでした。", ephemeral=True)
            return
        chunk = "**利用可能なGeminiモデル一覧：**\n"
        for line in lines:
            if len(chunk) + len(line) + 1 > 1900:
                await interaction.followup.send(chunk, ephemeral=True)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await interaction.followup.send(chunk, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ モデル一覧の取得に失敗したよ: {e}", ephemeral=True)


# =============================================================================
# キルスイッチ（緊急遮断 / ブレークグラス）
#   オーナーが手動で発火する可逆・監査可能・OWNER専用の緊急遮断。
#   自動発火は一切なし（どのイベントハンドラからも呼ばない）。
# =============================================================================

# @everyone で per-bot に直せない「危険権限」
_PANIC_DANGEROUS_PERMS = (
    "administrator", "ban_members", "kick_members",
    "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "mention_everyone",
)


def _panic_dangerous_perm_names(perms: discord.Permissions) -> list[str]:
    """与えられた権限のうち危険なものの名前一覧。"""
    out = []
    for name in _PANIC_DANGEROUS_PERMS:
        if getattr(perms, name, False):
            out.append(name)
    return out


async def _panic_resolve_me(guild) -> "discord.Member | None":
    """空気くん自身の Member を解決する。
    fetch_guild 由来のギルドは guild.me が None になるため REST フォールバックする。
    """
    me = getattr(guild, "me", None)
    if me is not None:
        return me
    try:
        if client.user is not None:
            return await guild.fetch_member(client.user.id)
    except Exception as e:
        print(f"[PANIC] _panic_resolve_me fetch失敗: {e}")
    return None


async def _panic_audit(guild, text: str):
    """best-effort 監査ログ。print は必ず実行。ch 投稿失敗はキルを止めない。"""
    print(f"[PANIC] {text}")
    if not PANIC_LOG_CHANNEL_ID:
        return
    try:
        ch = None
        if guild is not None:
            ch = guild.get_channel(PANIC_LOG_CHANNEL_ID)
        if ch is None:
            ch = client.get_channel(PANIC_LOG_CHANNEL_ID)
        if ch is None:
            ch = await client.fetch_channel(PANIC_LOG_CHANNEL_ID)
        embed = discord.Embed(
            title="🚨 キルスイッチ監査ログ",
            description=text[:4000],
            color=0xE74C3C,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        await ch.send(embed=embed)
    except Exception as e:
        # 混乱時に最も失敗しやすいのが ch 投稿。絶対にキルを止めない。
        print(f"[PANIC] 監査ログ投稿失敗（無視して続行）: {e}")


async def _panic_neutralize_bot(guild, target, mode: str, actor: str) -> dict:
    """対象 bot を無力化する共有ヘルパ。例外を外に投げない（全体 try/except）。
    戻り値は構造化 dict。health server / bot を絶対に落とさない。
    """
    try:
        result = {
            "ok": False,
            "mode": mode,
            "target": {"id": getattr(target, "id", None),
                       "name": str(getattr(target, "name", "?"))},
            "snapshot_id": None,
            "actions": [],
            "unactionable": [],
            "warnings": [],
            "reversibility": "",
        }

        # --- ガード ---
        if mode not in ("strip", "kick", "ban"):
            result["error"] = f"未知の mode: {mode}"
            return result
        if not getattr(target, "bot", False):
            result["error"] = "v1 は bot のみ対象です（メンバー隔離は v2）。"
            return result

        me = await _panic_resolve_me(guild)
        if me is None:
            result["error"] = "空気くん自身のメンバー情報を取得できませんでした。"
            return result
        if target.id == me.id:
            result["error"] = "空気くん自身は対象にできません。"
            return result
        if OWNER_ID == 0 or target.id == OWNER_ID:
            result["error"] = "オーナーは対象にできません（または OWNER 未設定）。"
            return result

        my_top = me.top_role.position

        # --- 原状から entries / warnings を構築（破壊操作の前に確定） ---
        managed_entries = []      # 権限ゼロ化する managed ロール
        non_managed_roles = []    # bot から外す非 managed ロール
        membership_entries = []
        critical_blocks = []      # managed なのに編集不可（=階層が下） → 全体 abort

        for role in target.roles:
            if role.is_default():   # @everyone
                continue
            editable = my_top > role.position
            if role.managed:
                if not editable:
                    # managed（危険権限の在処）が編集不可 → critical
                    critical_blocks.append(role)
                else:
                    managed_entries.append({
                        "role_id": role.id,
                        "role_name": role.name,
                        "type": "managed",
                        "perms": role.permissions.value,
                    })
            else:
                if not editable:
                    # 非 managed が外せない（階層が下）。危険権限を持つなら managed と同じく
                    # critical 扱い（半端な無力化＝「効いたつもりで生きてる」を避ける）。
                    danger = _panic_dangerous_perm_names(role.permissions)
                    if danger:
                        critical_blocks.append(role)
                    else:
                        result["unactionable"].append(
                            f"{role.name}: 空気くんより上位（除去不可・危険権限なし）"
                        )
                else:
                    non_managed_roles.append(role)
                    membership_entries.append({
                        "role_id": role.id,
                        "role_name": role.name,
                        "type": "membership",
                    })

        # @everyone の危険権限は per-bot で直せない → warnings のみ（変更しない）
        try:
            ev = guild.default_role
            danger = _panic_dangerous_perm_names(ev.permissions)
            if danger:
                result["warnings"].append(
                    "@everyone が危険権限を保持（per-bot では直せません）: " + ", ".join(danger)
                )
        except Exception:
            pass

        # strip で「危険権限の在処」を編集/除去できないなら「何もせず」明確に拒否
        # （managed ロールの権限編集不可、または危険権限を持つ非managedロールが除去不可）。
        # 部分無力化＝「効いたつもりで Wick が生きてる」最悪パターンを避ける。
        if mode == "strip" and critical_blocks:
            names = ", ".join(f"`{r.name}`" for r in critical_blocks)
            result["error"] = (
                f"⛔ 階層前提を満たしていません。空気くんを {names} より上に移動してください。"
                "（危険権限の在処であるこれらのロールを編集/除去できないため、"
                "半端な無力化を避けて中止しました）"
            )
            await _panic_audit(
                guild,
                f"neutralize ABORT target={target} mode={mode} actor={actor} "
                f"理由=危険権限ロール編集/除去不可 {names}"
            )
            return result

        entries = managed_entries + membership_entries
        reversibility = {
            "strip": "strip=完全可逆（managed権限の復元 + ロール再付与）",
            "kick":  "kick=再招待は手動（bot は OAuth 必須・bot 不可）",
            "ban":   "ban=unban のみで可逆（再招待は手動）",
        }[mode]
        result["reversibility"] = reversibility

        # --- Mongo に snapshot doc を insert（破壊操作より前 = restore の真実の源） ---
        snapshot_doc = {
            "target_id": target.id,
            "target_name": str(target.name),
            "guild_id": guild.id,
            "mode": mode,
            "actor": actor,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "applied",
            "restored": False,
            "restored_at": None,
            "entries": entries,
            "warnings": list(result["warnings"]),
        }
        ins = await killswitch_col.insert_one(snapshot_doc)
        snapshot_id = str(ins.inserted_id)
        result["snapshot_id"] = snapshot_id

        # --- 破壊操作（per-action try/except で捕捉） ---
        reason = f"[PANIC] killswitch {mode} by {actor}"
        applied_entries = []
        made_changes = False  # 破壊操作が1つでも適用されたか（snapshot を復旧可能にするか判定）

        if mode == "strip":
            # 危険権限ロールのうち無力化に失敗したもの（→ ok=False で正直に報告）
            dangerous_failures = []
            # managed: 権限をゼロ化
            for ent in managed_entries:
                ent_dangerous = bool(_panic_dangerous_perm_names(discord.Permissions(ent["perms"])))
                role = guild.get_role(ent["role_id"])
                if role is None:
                    result["unactionable"].append(f"{ent['role_name']}: ロールが見つからない")
                    if ent_dangerous:
                        dangerous_failures.append(ent["role_name"])
                    continue
                try:
                    await role.edit(permissions=discord.Permissions.none(), reason=reason)
                    result["actions"].append(f"zeroed perms on {role.name}")
                    applied_entries.append(ent)
                except Exception as e:
                    result["unactionable"].append(f"{ent['role_name']}: 権限ゼロ化失敗 {e}")
                    if ent_dangerous:
                        dangerous_failures.append(ent["role_name"])
            # 非 managed: bulk で一括除去
            if non_managed_roles:
                try:
                    await target.remove_roles(*non_managed_roles, reason=reason)
                    for r in non_managed_roles:
                        result["actions"].append(f"removed {r.name}")
                    applied_entries.extend(membership_entries)
                except Exception:
                    # bulk 失敗時は個別に試行
                    for r in non_managed_roles:
                        try:
                            await target.remove_roles(r, reason=reason)
                            result["actions"].append(f"removed {r.name}")
                            applied_entries.append({
                                "role_id": r.id, "role_name": r.name, "type": "membership"
                            })
                        except Exception as e2:
                            result["unactionable"].append(f"{r.name}: 除去失敗 {e2}")
                            if _panic_dangerous_perm_names(r.permissions):
                                dangerous_failures.append(r.name)
            made_changes = bool(applied_entries)
            # 危険権限を1つでも無力化し損ねたら ok=False（「効いたつもりで生きてる」を防ぐ）
            if dangerous_failures:
                result["ok"] = False
                result["error"] = (
                    "⚠️ 危険権限ロールを完全には無力化できませんでした（対象がまだ生きている可能性）: "
                    + ", ".join(dangerous_failures)
                    + "。空気くんの階層位置を確認し、必要なら kick/ban を検討してください。"
                )
            else:
                result["ok"] = True
            result["warnings"].append(
                "strip はロール由来の権限のみ無効化します。チャンネル個別の権限上書き"
                "(channel overwrite) を持つ bot はそれを保持し得ます。完全除去は kick/ban を。"
            )

        elif mode == "kick":
            applied_entries = entries
            try:
                await target.kick(reason=reason)
                result["actions"].append(f"kicked {target.name}")
                result["ok"] = True
                made_changes = True
            except Exception as e:
                result["error"] = f"kick 失敗: {e}"

        elif mode == "ban":
            applied_entries = entries
            try:
                await guild.ban(target, reason=reason, delete_message_seconds=0)
                result["actions"].append(f"banned {target.name}")
                result["ok"] = True
                made_changes = True
            except Exception as e:
                result["error"] = f"ban 失敗: {e}"

        # --- snapshot doc を結果で update ---
        try:
            await killswitch_col.update_one(
                {"_id": ins.inserted_id},
                {"$set": {
                    # 1つでも適用できたら restore 可能にする（部分失敗で ok=False でも
                    # 適用分は target 復旧で戻せるように status は made_changes で判定）
                    "status": "applied" if made_changes else "failed",
                    "entries": applied_entries,
                    "actions": result["actions"],
                    "unactionable": result["unactionable"],
                }},
            )
        except Exception as e:
            print(f"[PANIC] snapshot update 失敗: {e}")

        # --- best-effort 監査ログ（失敗してもキルを止めない） ---
        await _panic_audit(
            guild,
            f"neutralize target={target.name}({target.id}) mode={mode} actor={actor} "
            f"ok={result['ok']} actions={result['actions']} snapshot={snapshot_id}"
        )
        return result

    except Exception as e:
        # どんな例外も外に投げない
        print(f"[PANIC] _panic_neutralize_bot 例外: {e}")
        traceback.print_exc()
        return {"ok": False, "mode": mode, "error": f"内部エラー: {e}"}


async def _panic_restore(guild, target_id: "int | None" = None,
                         snapshot_id: "str | None" = None) -> dict:
    """スナップショットから原状回復する共有ヘルパ。例外を外に投げない。"""
    try:
        from bson import ObjectId
        result = {"ok": False, "actions": [], "warnings": [], "snapshot_id": snapshot_id}

        doc = None
        if snapshot_id:
            try:
                doc = await killswitch_col.find_one({"_id": ObjectId(snapshot_id)})
            except Exception as e:
                result["error"] = f"snapshot_id が不正です: {e}"
                return result
        elif target_id is not None:
            doc = await killswitch_col.find_one(
                {"target_id": int(target_id), "status": "applied", "restored": False},
                sort=[("created_at", -1)],
            )
        else:
            result["error"] = "target または snapshot_id を指定してください。"
            return result

        if not doc:
            result["error"] = "復旧対象のスナップショットが見つかりません。"
            return result

        result["snapshot_id"] = str(doc["_id"])
        result["mode"] = doc.get("mode")
        result["target"] = {"id": doc.get("target_id"), "name": doc.get("target_name")}

        # 二重 restore 拒否
        if doc.get("restored"):
            result["error"] = "このスナップショットは既に復旧済みです。"
            return result

        mode = doc.get("mode")
        tid = int(doc.get("target_id"))
        reason = "[PANIC] killswitch restore"
        membership_roles = []

        for ent in doc.get("entries", []):
            if ent.get("type") == "managed":
                role = guild.get_role(ent["role_id"])
                if role is None:
                    result["warnings"].append(
                        f"{ent.get('role_name')}: ロールが見つからず権限復元できません"
                    )
                    continue
                try:
                    await role.edit(
                        permissions=discord.Permissions(ent["perms"]), reason=reason
                    )
                    result["actions"].append(f"restored perms on {role.name}")
                except Exception as e:
                    result["warnings"].append(f"{ent.get('role_name')}: 権限復元失敗 {e}")
            elif ent.get("type") == "membership":
                role = guild.get_role(ent["role_id"])
                if role is not None:
                    membership_roles.append(role)
                else:
                    result["warnings"].append(
                        f"{ent.get('role_name')}: ロールが見つからず再付与できません"
                    )

        if mode == "strip" and membership_roles:
            try:
                member = await guild.fetch_member(tid)
                await member.add_roles(*membership_roles, reason=reason)
                for r in membership_roles:
                    result["actions"].append(f"re-added {r.name}")
            except Exception as e:
                result["warnings"].append(f"ロール再付与失敗（bot が不在の可能性）: {e}")

        if mode == "kick":
            result["warnings"].append(
                "kick の復旧: bot の再招待は OAuth が必要で bot 側からは不可能です（手動で再招待してください）。"
            )
        elif mode == "ban":
            try:
                user = discord.Object(id=tid)
                await guild.unban(user, reason=reason)
                result["actions"].append("unbanned")
            except Exception as e:
                result["warnings"].append(f"unban 失敗: {e}")
            result["warnings"].append(
                "ban の復旧: unban のみ実施。再参加は対象 bot 側の再招待が必要です。"
            )

        await killswitch_col.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "restored": True,
                "restored_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }},
        )
        result["ok"] = True
        await _panic_audit(
            guild,
            f"restore snapshot={result['snapshot_id']} target={result['target']} "
            f"mode={mode} actions={result['actions']}"
        )
        return result

    except Exception as e:
        print(f"[PANIC] _panic_restore 例外: {e}")
        traceback.print_exc()
        return {"ok": False, "error": f"内部エラー: {e}"}


async def _panic_check(guild) -> dict:
    """ギルド内の各 bot について階層 readiness を報告する共有ヘルパ。"""
    try:
        result = {"ok": True, "ready": True, "bots": [], "warnings": []}
        me = await _panic_resolve_me(guild)
        if me is None:
            return {"ok": False, "error": "空気くん自身のメンバー情報を取得できません。"}
        my_top = me.top_role.position
        result["my_top_role"] = {"name": me.top_role.name, "position": my_top}

        # @everyone の危険権限
        try:
            danger = _panic_dangerous_perm_names(guild.default_role.permissions)
            if danger:
                result["warnings"].append(
                    "@everyone が危険権限を保持: " + ", ".join(danger)
                )
        except Exception:
            pass

        members = guild.members
        if not members:
            # gateway 未接続（fetch_guild 由来）だと members が空。最も診断が要る
            # 障害時にこそ偽の readiness:True を返さない。
            result["ready"] = False
            result["warnings"].append(
                "gateway 未接続の可能性: bot 一覧を取得できず readiness を判定できません"
                "（このエンドポイントが本当に必要な障害時は正確な判定が出せない点に注意）"
            )
        for m in members:
            if not m.bot:
                continue
            if m.id == me.id:
                continue
            bot_info = {
                "id": m.id, "name": str(m.name),
                "top_role": m.top_role.name,
                "above_me": my_top > m.top_role.position,
                "managed_editable": [], "managed_blocked": [],
            }
            for role in m.roles:
                if role.is_default():
                    continue
                if role.managed:
                    if my_top > role.position:
                        bot_info["managed_editable"].append(role.name)
                    else:
                        bot_info["managed_blocked"].append(role.name)
                        result["ready"] = False
            result["bots"].append(bot_info)
        return result
    except Exception as e:
        print(f"[PANIC] _panic_check 例外: {e}")
        return {"ok": False, "error": f"内部エラー: {e}"}


def _panic_owner_gate_ok(interaction: discord.Interaction) -> bool:
    """実セキュリティ: OWNER_ID 照合。OWNER_ID==0 なら全拒否。admin 権限では判定しない。"""
    return OWNER_ID != 0 and interaction.user.id == OWNER_ID


class PanicConfirmView(discord.ui.View):
    """/panic_bot の確認ボタン（ephemeral・60秒タイムアウト）。"""
    def __init__(self, target: discord.Member, mode: str):
        super().__init__(timeout=60)
        self.target = target
        self.mode = mode
        self._origin: "discord.Interaction | None" = None

    async def on_timeout(self):
        # 60秒放置でボタンを無効化（取り残し防止）。best-effort。
        for item in self.children:
            item.disabled = True
        if self._origin is not None:
            try:
                await self._origin.edit_original_response(view=self)
            except Exception:
                pass

    @discord.ui.button(label="実行する（緊急遮断）", style=discord.ButtonStyle.danger, emoji="🚨")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ボタン操作も OWNER のみ
        if not _panic_owner_gate_ok(interaction):
            await interaction.response.send_message("⛔ オーナー専用です。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        actor = f"slash:{interaction.user.id}"
        res = await _panic_neutralize_bot(interaction.guild, self.target, self.mode, actor)
        for item in self.children:
            item.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            pass
        await interaction.followup.send(_panic_format_result(res), ephemeral=True)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="キャンセルしました。", view=self)
        self.stop()


def _panic_format_result(res: dict) -> str:
    """neutralize / restore の dict を人間可読テキストに整形。"""
    lines = []
    if res.get("ok"):
        lines.append("✅ 完了しました。")
    else:
        lines.append("⚠️ 失敗または部分的失敗。")
    if res.get("error"):
        lines.append(f"理由: {res['error']}")
    if res.get("mode"):
        lines.append(f"mode: `{res['mode']}`")
    if res.get("snapshot_id"):
        lines.append(f"snapshot: `{res['snapshot_id']}`")
    if res.get("actions"):
        lines.append("実行: " + ", ".join(res["actions"]))
    if res.get("unactionable"):
        lines.append("未処理: " + " / ".join(res["unactionable"]))
    if res.get("warnings"):
        lines.append("⚠️ 警告: " + " / ".join(res["warnings"]))
    if res.get("reversibility"):
        lines.append("可逆性: " + res["reversibility"])
    return "\n".join(lines)[:1900]


@client.tree.command(name="panic_check", description="【オーナー専用】キルスイッチの階層 readiness を確認")
@app_commands.default_permissions(administrator=True)
async def panic_check_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    if not _panic_owner_gate_ok(interaction):
        await interaction.response.send_message("⛔ このコマンドはオーナー専用です。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    res = await _panic_check(interaction.guild)
    if not res.get("ok"):
        await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)
        return
    embed = discord.Embed(
        title="🛡️ キルスイッチ階層 readiness",
        color=0x2ECC71 if res.get("ready") else 0xE67E22,
    )
    mt = res.get("my_top_role", {})
    embed.add_field(
        name="空気くんの最上位ロール",
        value=f"{mt.get('name','?')} (pos {mt.get('position','?')})",
        inline=False,
    )
    for b in res.get("bots", [])[:20]:
        status = "✅ 上位" if b["above_me"] else "❌ 下位"
        val = f"top_role: {b['top_role']} / {status}"
        if b["managed_editable"]:
            val += "\n編集可 managed: " + ", ".join(b["managed_editable"])
        if b["managed_blocked"]:
            val += "\n⛔ 編集不可 managed: " + ", ".join(b["managed_blocked"])
        embed.add_field(name=f"{b['name']} ({b['id']})", value=val[:1024], inline=False)
    if not res.get("bots"):
        embed.description = "bot メンバーが見つかりません。"
    if res.get("warnings"):
        embed.add_field(name="⚠️ 警告", value="\n".join(res["warnings"])[:1024], inline=False)
    if not res.get("ready"):
        embed.set_footer(text="readiness に問題があります。⚠️警告と各 bot の表示を確認してください。")
    await interaction.followup.send(embed=embed, ephemeral=True)


@client.tree.command(name="panic_bot", description="【オーナー専用】暴走 bot を緊急無力化（確認あり）")
@app_commands.describe(bot="対象の bot", mode="strip=可逆 / kick / ban")
@app_commands.choices(mode=[
    app_commands.Choice(name="strip（権限ゼロ化・可逆）", value="strip"),
    app_commands.Choice(name="kick", value="kick"),
    app_commands.Choice(name="ban", value="ban"),
])
@app_commands.default_permissions(administrator=True)
async def panic_bot_cmd(interaction: discord.Interaction, bot: discord.Member,
                        mode: app_commands.Choice[str]):
    if not await check_home_guild(interaction):
        return
    if not _panic_owner_gate_ok(interaction):
        await interaction.response.send_message("⛔ このコマンドはオーナー専用です。", ephemeral=True)
        return
    if not bot.bot:
        await interaction.response.send_message(
            "⛔ v1 は bot のみ対象です（メンバー隔離は v2）。", ephemeral=True
        )
        return
    view = PanicConfirmView(bot, mode.value)
    await interaction.response.send_message(
        f"🚨 **緊急遮断の確認**\n対象: {bot.mention}（{bot.name} / {bot.id}）\n"
        f"mode: `{mode.value}`\n\n本当に実行しますか？（60秒以内）",
        view=view, ephemeral=True,
    )
    view._origin = interaction  # on_timeout でボタン無効化するため保持


@client.tree.command(name="panic_restore", description="【オーナー専用】キルスイッチで無力化した bot を復旧")
@app_commands.describe(target="復旧対象の bot（省略時は直近の未復旧スナップショット）")
@app_commands.default_permissions(administrator=True)
async def panic_restore_cmd(interaction: discord.Interaction,
                            target: "discord.Member | None" = None):
    if not await check_home_guild(interaction):
        return
    if not _panic_owner_gate_ok(interaction):
        await interaction.response.send_message("⛔ このコマンドはオーナー専用です。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    target_id = target.id if target is not None else None
    res = await _panic_restore(interaction.guild, target_id=target_id)
    await interaction.followup.send(_panic_format_result(res), ephemeral=True)


# --- 外部エンドポイント（aiohttp health server に相乗り） ---
# 簡易レート制限（in-memory・送信元IP単位の「失敗試行」回数）。
# ブルートフォース対策なので token 照合の【前】に判定し、【失敗だけ】を数える。
# 正しいトークンの正規発火はカウントしない → オーナーを締め出さない。
_panic_fail_hits: dict = {}
_PANIC_RATE_WINDOW = 60
_PANIC_RATE_MAX = 10
# X-Forwarded-For は攻撃者が偽装でき、IP をローテートすると dict が無限増殖し得る。
# レート制限は token(高エントロピー)+compare_digest を補助する best-effort 防御なので、
# キー数に硬上限を設けてメモリ DoS を防ぐ（上限超過時は古い追跡を捨てる）。
_PANIC_RATE_MAX_KEYS = 1024


def _panic_client_ip(request) -> str:
    # Render 等プロキシ背後では実IPは X-Forwarded-For の先頭ホップ。
    # 偽装可能だが、ここで proxy IP を使うと攻撃者の失敗洪水でオーナーまで
    # 巻き添えロックされる（緊急時に致命的）。偽装耐性より「オーナーを締め出さない」を優先。
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote or "?"


def _panic_sweep_fail_hits():
    """全キーをウィンドウで剪定し、空になったキーを削除（メモリ上限維持用）。"""
    cutoff = time.time() - _PANIC_RATE_WINDOW
    for k in list(_panic_fail_hits.keys()):
        kept = [t for t in _panic_fail_hits[k] if t >= cutoff]
        if kept:
            _panic_fail_hits[k] = kept
        else:
            _panic_fail_hits.pop(k, None)


def _panic_ip_blocked(ip: str) -> bool:
    """直近ウィンドウの失敗回数が上限以上か（判定のみ・カウントしない）。"""
    cutoff = time.time() - _PANIC_RATE_WINDOW
    hits = [t for t in _panic_fail_hits.get(ip, ()) if t >= cutoff]
    if hits:
        _panic_fail_hits[ip] = hits
    else:
        _panic_fail_hits.pop(ip, None)
    return len(hits) >= _PANIC_RATE_MAX


def _panic_record_fail(ip: str):
    """token 照合失敗を IP 単位で記録。キー数が硬上限を超えないよう剪定/退避する。"""
    if ip not in _panic_fail_hits and len(_panic_fail_hits) >= _PANIC_RATE_MAX_KEYS:
        _panic_sweep_fail_hits()
        if len(_panic_fail_hits) >= _PANIC_RATE_MAX_KEYS:
            # 剪定でも下がらなければ最古の最終失敗時刻のキーを1つ退避（FIFO 近似）
            oldest = min(_panic_fail_hits,
                         key=lambda k: _panic_fail_hits[k][-1] if _panic_fail_hits[k] else 0.0)
            _panic_fail_hits.pop(oldest, None)
    _panic_fail_hits.setdefault(ip, []).append(time.time())


async def _panic_web_handler(request):
    """外部 /panic エンドポイント。必ず web.Response を返す（health server を落とさない）。"""
    from aiohttp import web
    try:
        # 1. PANIC_TOKEN 空 → 無効（503）
        if not PANIC_TOKEN:
            return web.json_response({"error": "disabled"}, status=503)

        # 2. 送信元IP単位のブルートフォース判定（token 照合の【前】・失敗回数ベース）
        ip = _panic_client_ip(request)
        if _panic_ip_blocked(ip):
            print(f"[PANIC] 429 レート制限 from {ip}")
            return web.json_response({"error": "rate_limited"}, status=429)

        # 3. token 照合（定数時間比較）。失敗は IP 単位で記録
        provided = request.headers.get("X-Panic-Token", "")
        if not hmac.compare_digest(str(provided), str(PANIC_TOKEN)):
            _panic_record_fail(ip)
            print(f"[PANIC] 403 不正トークン from {ip}")
            return web.json_response({"error": "forbidden"}, status=403)

        # 4. body パース
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        action = body.get("action")
        if action not in ("neutralize", "restore", "check"):
            return web.json_response({"error": "unknown_action"}, status=400)

        # 5. ギルド取得は gateway キャッシュに依存しない
        if OWNER_ID == 0:
            return web.json_response({"error": "owner_not_configured"}, status=503)
        guild = client.get_guild(HOME_GUILD_ID)
        if guild is None:
            guild = await client.fetch_guild(HOME_GUILD_ID)

        actor = f"endpoint:{ip}(peer:{request.remote})"

        # 6. action ごとにコア共有ヘルパを呼ぶ
        if action == "check":
            result = await _panic_check(guild)
            return web.json_response(result)

        if action == "neutralize":
            bot_id = body.get("bot_id")
            if bot_id is None:
                return web.json_response({"error": "missing bot_id"}, status=400)
            mode = body.get("mode", "strip")
            try:
                target = await guild.fetch_member(int(bot_id))
            except Exception as e:
                return web.json_response({"error": f"member_fetch_failed: {e}"}, status=404)
            result = await _panic_neutralize_bot(guild, target, mode, actor)
            return web.json_response(result)

        if action == "restore":
            bot_id = body.get("bot_id")
            result = await _panic_restore(
                guild, target_id=int(bot_id) if bot_id is not None else None
            )
            return web.json_response(result)

        return web.json_response({"error": "unhandled"}, status=400)

    except Exception as e:
        # 想定外でも health server を落とさない
        print(f"[PANIC] _panic_web_handler 例外: {e}")
        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500)


# =============================================================================
# モデレーション・ガード：他管理者/他botの ban・kick を検知してレビュー（モデルC）
#   - ban を発動前に止めることは Discord 仕様上不可能 → 検知→通知→ワンクリックUndo。
#   - Wick の ban は既定で Undo を出さない（荒らし対策の正当banを誤解除しないため）。
#   - 空気くん自身の操作は self-filter で除外（通知ループ防止）。
#   - kick は巻き戻し不可（強制再参加は不可）→ 検知・通知＋招待リンク発行のみ。
# =============================================================================


async def _guard_find_audit_entry(guild, action, target_id, retries: int = 4, delay: float = 1.0):
    """監査ログから target_id に対する直近の action エントリを探す。
    on_member_ban は監査エントリ確定前に発火し得るため数回リトライする。
    新鮮さ(30秒)で stale エントリを弾く。見つからなければ None。
    View Audit Log 権限が無ければ静かに諦める。"""
    if guild is None:
        return None
    for _ in range(max(1, retries)):
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            async for entry in guild.audit_logs(action=action, limit=8):
                if getattr(entry.target, "id", None) != target_id:
                    continue
                age = (now - ensure_utc(entry.created_at)).total_seconds()
                if age <= 30:
                    return entry
                # このtargetへの最新一致が古い → 新規actionは未記録。リトライへ
                break
        except discord.Forbidden:
            print("[GUARD] View Audit Log 権限がありません（実行者を特定できません）")
            return None
        except Exception as e:
            print(f"[GUARD] audit_logs 取得失敗: {e}")
        await asyncio.sleep(delay)
    return None


def _guard_resolve_log_channel(guild):
    """レビュー投稿先 channel を解決。優先: MOD_GUARD_LOG → PANIC_LOG → NOTIFY。"""
    for cid in (MOD_GUARD_LOG_CHANNEL_ID, PANIC_LOG_CHANNEL_ID, NOTIFY_CHANNEL_ID):
        if not cid:
            continue
        ch = guild.get_channel(cid) if guild is not None else None
        if ch is None:
            ch = client.get_channel(cid)
        if ch is not None:
            return ch
    return None


async def _guard_store_event(kind, guild, target, executor, reason):
    """検知履歴を MongoDB に best-effort で保存。"""
    try:
        await guard_events_col.insert_one({
            "kind": kind,
            "guild_id": guild.id if guild else None,
            "target_id": getattr(target, "id", None),
            "target_name": str(target),
            "executor_id": getattr(executor, "id", None),
            "executor_name": str(executor) if executor else None,
            "reason": str(reason)[:500],
            "created_at": datetime.datetime.now(datetime.timezone.utc),
        })
    except Exception as e:
        print(f"[GUARD] event保存失敗: {e}")


async def _guard_mark_resolved(interaction: discord.Interaction, note: str):
    """レビューembedに対応結果を追記しボタンを消す。best-effort。"""
    try:
        msg = interaction.message
        embed = msg.embeds[0] if (msg and msg.embeds) else discord.Embed()
        embed.add_field(name="対応", value=note[:1024], inline=False)
        embed.color = 0x2ECC71
        await msg.edit(embed=embed, view=None)
    except Exception as e:
        print(f"[GUARD] mark_resolved失敗: {e}")
        try:
            await interaction.followup.send(note, ephemeral=False)
        except Exception:
            pass


class GuardUnbanButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"guard:unban:(?P<uid>\d+)",
):
    """ban レビューの「Undo（解除）」ボタン。custom_id に対象IDを埋め、再起動後も有効。"""
    def __init__(self, uid: int):
        self.uid = uid
        super().__init__(
            discord.ui.Button(
                label="Undo（banを解除）",
                style=discord.ButtonStyle.success,
                emoji="↩️",
                custom_id=f"guard:unban:{uid}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["uid"]))

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message(
                "⛔ banを解除するには ban_members 権限が必要です。", ephemeral=True)
            return
        await interaction.response.defer()
        guild = interaction.guild
        try:
            await guild.unban(
                discord.Object(id=self.uid),
                reason=f"mod-guard Undo by {interaction.user} ({interaction.user.id})",
            )
            note = f"↩️ {interaction.user.mention} がbanを解除しました。"
        except discord.NotFound:
            note = f"ℹ️ 既にban解除済みでした（{interaction.user.mention} が確認）。"
        except discord.Forbidden:
            await interaction.followup.send(
                "⛔ 空気くんに ban_members 権限が無く解除できません。", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ 解除に失敗: {e}", ephemeral=True)
            return
        await _guard_mark_resolved(interaction, note)


class GuardInviteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"guard:invite",
):
    """kick レビューの「招待リンク発行」ボタン。kick は巻き戻せないため再招待用。"""
    def __init__(self):
        super().__init__(
            discord.ui.Button(
                label="招待リンクを発行",
                style=discord.ButtonStyle.primary,
                emoji="✉️",
                custom_id="guard:invite",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                "⛔ kick_members 権限が必要です。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            target_ch = interaction.guild.system_channel or interaction.channel
            invite = await target_ch.create_invite(
                max_age=86400, max_uses=1, unique=True,
                reason=f"mod-guard re-invite by {interaction.user}",
            )
            await interaction.followup.send(
                "✉️ 24時間有効・1回使い切りの招待リンクです"
                f"（kickされた人にDM等で渡してください）:\n{invite.url}",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ 招待発行に失敗: {e}", ephemeral=True)


@client.event
async def on_member_ban(guild: discord.Guild, user):
    """他者の ban を検知 → レビュー投稿（Wick/信頼済み/自分は除外）。"""
    if not MOD_GUARD_ENABLED:
        return
    try:
        if guild.id != HOME_GUILD_ID:
            return
        entry = await _guard_find_audit_entry(guild, discord.AuditLogAction.ban, user.id)
        executor = entry.user if entry else None
        reason = (entry.reason if entry else None) or "（理由なし）"

        # self-filter：空気くん自身のbanは通知しない（ループ防止）
        if executor and client.user and executor.id == client.user.id:
            return

        ch = _guard_resolve_log_channel(guild)
        if ch is None:
            print("[GUARD] ログchannel未設定のため ban を通知できません")
            return

        is_wick = bool(WICK_BOT_ID and executor and executor.id == WICK_BOT_ID)
        is_trusted = bool(executor and executor.id in GUARD_TRUSTED_IDS)
        exec_label = f"{executor} (`{executor.id}`)" if executor else "不明（監査ログ取得不可）"

        embed = discord.Embed(
            title="🔨 BAN を検知",
            color=0xE74C3C,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="対象", value=f"{user} (`{user.id}`)", inline=False)
        embed.add_field(name="実行者", value=exec_label, inline=True)
        embed.add_field(name="理由", value=str(reason)[:1024], inline=False)

        view = None
        if is_wick:
            embed.color = 0x95A5A6
            embed.add_field(
                name="判定",
                value="🛡️ Wick による ban → 荒らし対策の正当banとみなし Undo は出しません"
                      "（必要なら手動で解除してください）。",
                inline=False)
        elif is_trusted:
            embed.color = 0x95A5A6
            embed.add_field(name="判定", value="✅ 信頼済み実行者のためログのみ。", inline=False)
        elif executor is None:
            # fail-safe：実行者を特定できない時は Undo を出さない。
            # 荒らし時はWickの大量banで監査ログ取得が最も失敗しやすく、ここで
            # ボタンを出すと荒らしを一括unban→空気くん自身がanti-nukeに焼かれる。
            embed.color = 0xF1C40F
            embed.add_field(
                name="判定",
                value="⚠️ 実行者を特定できませんでした（監査ログ未取得/権限不足/大量ban中）。"
                      "誤解除防止のため Undo ボタンは出しません。不当なら手動で解除してください。",
                inline=False)
        else:
            view = discord.ui.View(timeout=None)
            view.add_item(GuardUnbanButton(user.id))
            embed.set_footer(text="不当と思えば下のボタンで即解除できます（要 ban_members 権限）")

        await ch.send(embed=embed, view=view)
        await _guard_store_event("ban", guild, user, executor, reason)
    except Exception as e:
        print(f"[GUARD] on_member_ban 例外: {e}")
        traceback.print_exc()


@client.event
async def on_member_remove(member: discord.Member):
    """退出を検知。監査ログに kick エントリがあれば「kick」としてレビュー投稿。
    自主退出・ban（別途 on_member_ban で処理）・監査取得不可は無視する。"""
    if not MOD_GUARD_ENABLED:
        return
    try:
        guild = member.guild
        if guild.id != HOME_GUILD_ID:
            return
        # 自主退出が大多数なので kick リトライは抑えめ（kickの監査記録は概ね即時）
        entry = await _guard_find_audit_entry(
            guild, discord.AuditLogAction.kick, member.id, retries=2)
        if entry is None:
            return  # 自主退出 or 監査取得不可 → 何もしない
        executor = entry.user
        if executor and client.user and executor.id == client.user.id:
            return  # 自分のkick
        reason = entry.reason or "（理由なし）"
        is_wick = bool(WICK_BOT_ID and executor and executor.id == WICK_BOT_ID)
        is_trusted = bool(executor and executor.id in GUARD_TRUSTED_IDS)

        ch = _guard_resolve_log_channel(guild)
        if ch is None:
            return
        exec_label = f"{executor} (`{executor.id}`)" if executor else "不明"

        embed = discord.Embed(
            title="👢 KICK を検知",
            color=0xE67E22,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="対象", value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="実行者", value=exec_label, inline=True)
        embed.add_field(name="理由", value=str(reason)[:1024], inline=False)
        embed.add_field(
            name="注意",
            value="⚠️ kick は巻き戻せません（強制再参加は不可）。"
                  "必要なら招待リンクを発行して本人へ渡してください。",
            inline=False)

        view = None
        if is_wick or is_trusted:
            embed.color = 0x95A5A6
            embed.add_field(name="判定", value="ログのみ（信頼済み/Wick）。", inline=False)
        else:
            view = discord.ui.View(timeout=None)
            view.add_item(GuardInviteButton())

        await ch.send(embed=embed, view=view)
        await _guard_store_event("kick", guild, member, executor, reason)
    except Exception as e:
        print(f"[GUARD] on_member_remove 例外: {e}")
        traceback.print_exc()


@client.tree.command(name="guard_status", description="【管理者】ban/kickガードの稼働状況と権限を確認")
@app_commands.default_permissions(administrator=True)
async def guard_status_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("⛔ 管理者専用です。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    me = guild.me or await guild.fetch_member(client.user.id)
    p = me.guild_permissions
    ch = _guard_resolve_log_channel(guild)

    def yn(b): return "✅" if b else "❌"

    embed = discord.Embed(
        title="🛡️ ban/kick ガード状況",
        color=0x2ECC71 if MOD_GUARD_ENABLED else 0x95A5A6,
    )
    embed.add_field(name="有効", value=yn(MOD_GUARD_ENABLED), inline=True)
    embed.add_field(
        name="投稿先ch",
        value=(ch.mention if ch else "❌ 未設定（MOD_GUARD_LOG_CHANNEL_ID等を設定）"),
        inline=True)
    embed.add_field(
        name="空気くんの権限",
        value=(f"View Audit Log {yn(p.view_audit_log)} / "
               f"Ban {yn(p.ban_members)} / Kick {yn(p.kick_members)} / "
               f"招待作成 {yn(p.create_instant_invite)}"),
        inline=False)
    embed.add_field(
        name="Wick ID",
        value=(f"`{WICK_BOT_ID}`（banはログのみ）" if WICK_BOT_ID else "未設定"),
        inline=True)
    embed.add_field(
        name="信頼済み実行者",
        value=(", ".join(f"`{i}`" for i in GUARD_TRUSTED_IDS) if GUARD_TRUSTED_IDS else "なし"),
        inline=True)

    try:
        recent = await guard_events_col.find(
            {"guild_id": guild.id}).sort("created_at", -1).limit(5).to_list(length=5)
        if recent:
            lines = []
            for ev in recent:
                icon = "🔨" if ev["kind"] == "ban" else "👢"
                ts = ensure_utc(ev["created_at"]).astimezone(
                    datetime.timezone(datetime.timedelta(hours=9))).strftime("%m/%d %H:%M")
                lines.append(f"{icon} {ts} 対象`{ev.get('target_name','?')}` ← "
                             f"実行`{ev.get('executor_name') or '不明'}`")
            embed.add_field(name="直近の検知", value="\n".join(lines)[:1024], inline=False)
    except Exception as e:
        embed.add_field(name="直近の検知", value=f"取得失敗: {e}", inline=False)

    if not p.view_audit_log:
        embed.set_footer(text="⚠️ View Audit Log 権限が無いと実行者を特定できません。")
    await interaction.followup.send(embed=embed, ephemeral=True)


# =============================================================================
# 統合モデレーション・パネル（v2）：分散したパネルを空気くん1つに集約
#   - 右クリック→アプリ で BAN / Kick / タイムアウト / 警告（モーダルで理由入力）
#   - /modpanel で常設ボタン（ch ロック切替・スローモード）を設置
#   - 操作は空気くん自身が実行（admin保持＝Wickより上位なので階層的に確実）
# =============================================================================

_MODGUARD_JST = datetime.timezone(datetime.timedelta(hours=9))


def _parse_duration(s: str):
    """'10m' / '1h' / '2d' / '30s' を秒に。最大28日(Discordのtimeout上限)。不正なら None。"""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(s).lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    secs = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if secs <= 0:
        return None
    return min(secs, 28 * 86400)


async def _mod_can_target(interaction: discord.Interaction, member: discord.Member):
    """対象操作の安全チェック。問題があればエラー文字列、無ければ None。"""
    guild = interaction.guild
    if guild is None:
        return "サーバー内でのみ使用できます。"
    if member.id == guild.owner_id:
        return "サーバーオーナーは対象にできません。"
    if client.user and member.id == client.user.id:
        return "空気くん自身は対象にできません。"
    me = guild.me
    if me and member.id != me.id and member.top_role >= me.top_role:
        return "対象が空気くんと同等以上のロールのため操作できません。"
    return None


async def _mod_log(guild, embed: discord.Embed):
    """操作ログをガードと同じchへ best-effort 投稿。"""
    try:
        ch = _guard_resolve_log_channel(guild)
        if ch:
            await ch.send(embed=embed)
    except Exception as e:
        print(f"[MODPANEL] ログ投稿失敗: {e}")


class BanModal(discord.ui.Modal, title="BAN 確認"):
    reason = discord.ui.TextInput(label="理由", required=False, max_length=400,
                                  style=discord.TextStyle.paragraph)
    del_days = discord.ui.TextInput(label="メッセージ削除日数(0-7)", required=False,
                                    max_length=1, default="0")

    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            dd = max(0, min(7, int(str(self.del_days) or "0")))
        except Exception:
            dd = 0
        reason = str(self.reason) or "（理由なし）"
        try:
            await interaction.guild.ban(
                self.member, reason=f"{reason} | by {interaction.user}",
                delete_message_seconds=dd * 86400)
            await interaction.followup.send(f"🔨 {self.member} をBANしました。", ephemeral=True)
            await _mod_log(interaction.guild, discord.Embed(
                title="🔨 BAN（パネル操作）", color=0xE74C3C,
                description=f"対象: {self.member} (`{self.member.id}`)\n"
                            f"実行: {interaction.user.mention}\n理由: {reason}"))
        except discord.Forbidden:
            await interaction.followup.send("⛔ 権限不足でBANできません（階層を確認）。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 失敗: {e}", ephemeral=True)


class KickModal(discord.ui.Modal, title="Kick 確認"):
    reason = discord.ui.TextInput(label="理由", required=False, max_length=400,
                                  style=discord.TextStyle.paragraph)

    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reason = str(self.reason) or "（理由なし）"
        try:
            await interaction.guild.kick(self.member, reason=f"{reason} | by {interaction.user}")
            await interaction.followup.send(f"👢 {self.member} をKickしました。", ephemeral=True)
            await _mod_log(interaction.guild, discord.Embed(
                title="👢 Kick（パネル操作）", color=0xE67E22,
                description=f"対象: {self.member} (`{self.member.id}`)\n"
                            f"実行: {interaction.user.mention}\n理由: {reason}"))
        except discord.Forbidden:
            await interaction.followup.send("⛔ 権限不足でKickできません（階層を確認）。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 失敗: {e}", ephemeral=True)


class TimeoutModal(discord.ui.Modal, title="タイムアウト（ミュート）"):
    duration = discord.ui.TextInput(label="期間 例: 10m / 1h / 1d（最大28d）",
                                    required=True, max_length=8)
    reason = discord.ui.TextInput(label="理由", required=False, max_length=400,
                                  style=discord.TextStyle.paragraph)

    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        secs = _parse_duration(str(self.duration))
        if secs is None:
            await interaction.followup.send(
                "⛔ 期間の形式が不正です（例: 10m, 1h, 2d）。", ephemeral=True)
            return
        reason = str(self.reason) or "（理由なし）"
        try:
            await self.member.timeout(datetime.timedelta(seconds=secs),
                                      reason=f"{reason} | by {interaction.user}")
            await interaction.followup.send(
                f"🔇 {self.member} を {self.duration} タイムアウトしました。", ephemeral=True)
            await _mod_log(interaction.guild, discord.Embed(
                title="🔇 タイムアウト（パネル操作）", color=0x9B59B6,
                description=f"対象: {self.member} (`{self.member.id}`)\n期間: {self.duration}\n"
                            f"実行: {interaction.user.mention}\n理由: {reason}"))
        except discord.Forbidden:
            await interaction.followup.send("⛔ 権限不足でタイムアウトできません（階層を確認）。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 失敗: {e}", ephemeral=True)


class WarnModal(discord.ui.Modal, title="警告"):
    reason = discord.ui.TextInput(label="警告理由", required=True, max_length=400,
                                  style=discord.TextStyle.paragraph)

    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reason = str(self.reason)
        warn_doc = {
            "reason": reason[:400], "by_id": interaction.user.id,
            "by_name": str(interaction.user),
            "at": datetime.datetime.now(datetime.timezone.utc),
        }
        try:
            doc = await users_col.find_one_and_update(
                {"_id": str(self.member.id)}, {"$push": {"mod_warnings": warn_doc}},
                upsert=True, return_document=True)
            count = len(doc.get("mod_warnings", [])) if doc else 1
        except Exception as e:
            await interaction.followup.send(f"❌ 警告の保存に失敗: {e}", ephemeral=True)
            return
        try:
            await self.member.send(
                f"⚠️ **{interaction.guild.name}** で警告を受けました（通算{count}回目）。\n理由: {reason}")
            dm_note = ""
        except Exception:
            dm_note = "（DM送信不可）"
        await _mod_log(interaction.guild, discord.Embed(
            title="⚠️ 警告（パネル操作）", color=0xF1C40F,
            description=f"対象: {self.member} (`{self.member.id}`)\n通算: **{count}回**\n"
                        f"実行: {interaction.user.mention}\n理由: {reason}"))
        await interaction.followup.send(
            f"⚠️ 警告しました（通算{count}回）{dm_note}", ephemeral=True)


@client.tree.context_menu(name="BAN")
@app_commands.default_permissions(ban_members=True)
async def ctx_ban(interaction: discord.Interaction, member: discord.Member):
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("⛔ ban_members 権限が必要です。", ephemeral=True)
        return
    err = await _mod_can_target(interaction, member)
    if err:
        await interaction.response.send_message(f"⛔ {err}", ephemeral=True)
        return
    await interaction.response.send_modal(BanModal(member))


@client.tree.context_menu(name="Kick")
@app_commands.default_permissions(kick_members=True)
async def ctx_kick(interaction: discord.Interaction, member: discord.Member):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message("⛔ kick_members 権限が必要です。", ephemeral=True)
        return
    err = await _mod_can_target(interaction, member)
    if err:
        await interaction.response.send_message(f"⛔ {err}", ephemeral=True)
        return
    await interaction.response.send_modal(KickModal(member))


@client.tree.context_menu(name="タイムアウト")
@app_commands.default_permissions(moderate_members=True)
async def ctx_timeout(interaction: discord.Interaction, member: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("⛔ moderate_members 権限が必要です。", ephemeral=True)
        return
    err = await _mod_can_target(interaction, member)
    if err:
        await interaction.response.send_message(f"⛔ {err}", ephemeral=True)
        return
    await interaction.response.send_modal(TimeoutModal(member))


@client.tree.context_menu(name="警告")
@app_commands.default_permissions(kick_members=True)
async def ctx_warn(interaction: discord.Interaction, member: discord.Member):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message("⛔ kick_members 権限が必要です。", ephemeral=True)
        return
    if client.user and member.id == client.user.id:
        await interaction.response.send_message("⛔ 空気くん自身は対象にできません。", ephemeral=True)
        return
    await interaction.response.send_modal(WarnModal(member))


class ModPanelView(discord.ui.View):
    """/modpanel で設置する常設パネル。操作対象は設置されたチャンネル。"""
    def __init__(self):
        super().__init__(timeout=None)

    async def _need(self, interaction: discord.Interaction, perm: str) -> bool:
        if not getattr(interaction.user.guild_permissions, perm, False):
            await interaction.response.send_message(f"⛔ `{perm}` 権限が必要です。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔒 ロック切替", style=discord.ButtonStyle.danger,
                       custom_id="modpanel:lock")
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._need(interaction, "manage_channels"):
            return
        ch = interaction.channel
        everyone = interaction.guild.default_role
        ow = ch.overwrites_for(everyone)
        locked_now = ow.send_messages is False
        ow.send_messages = None if locked_now else False
        try:
            await ch.set_permissions(everyone, overwrite=ow,
                                     reason=f"modpanel lock by {interaction.user}")
            await interaction.response.send_message(
                f"{'🔓 解除' if locked_now else '🔒 ロック'}しました（#{ch.name}）。", ephemeral=False)
        except Exception as e:
            await interaction.response.send_message(f"❌ 失敗: {e}", ephemeral=True)

    @discord.ui.select(custom_id="modpanel:slowmode", placeholder="🐌 スローモードを設定…",
                       options=[
                           discord.SelectOption(label="オフ", value="0"),
                           discord.SelectOption(label="5秒", value="5"),
                           discord.SelectOption(label="15秒", value="15"),
                           discord.SelectOption(label="60秒", value="60"),
                           discord.SelectOption(label="5分", value="300"),
                       ])
    async def slowmode(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await self._need(interaction, "manage_channels"):
            return
        sec = int(select.values[0])
        try:
            await interaction.channel.edit(
                slowmode_delay=sec, reason=f"modpanel slowmode by {interaction.user}")
            await interaction.response.send_message(
                f"🐌 スローモードを **{sec}秒**{'（オフ）' if sec == 0 else ''}に設定（#{interaction.channel.name}）。",
                ephemeral=False)
        except Exception as e:
            await interaction.response.send_message(f"❌ 失敗: {e}", ephemeral=True)


@client.tree.command(name="modpanel", description="【管理者】モデレーション操作パネルを設置")
@app_commands.default_permissions(manage_guild=True)
async def modpanel_cmd(interaction: discord.Interaction):
    if not await check_home_guild(interaction):
        return
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("⛔ manage_channels 権限が必要です。", ephemeral=True)
        return
    embed = discord.Embed(
        title="🛠️ モデレーション・パネル",
        description="**このチャンネル**への操作です。\n"
                    "🔒 ロック切替 ／ 🐌 スローモード\n\n"
                    "ユーザー個別操作（BAN/Kick/タイムアウト/警告）は、"
                    "**ユーザーを右クリック → アプリ** から実行できます。",
        color=0x5865F2)
    await interaction.channel.send(embed=embed, view=ModPanelView())
    await interaction.response.send_message("✅ パネルを設置しました。", ephemeral=True)


@client.tree.command(name="warnings", description="【管理者】ユーザーの警告履歴を表示")
@app_commands.describe(user="対象ユーザー")
@app_commands.default_permissions(kick_members=True)
async def warnings_cmd(interaction: discord.Interaction, user: discord.Member):
    if not await check_home_guild(interaction):
        return
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message("⛔ kick_members 権限が必要です。", ephemeral=True)
        return
    doc = await users_col.find_one({"_id": str(user.id)})
    warns = (doc or {}).get("mod_warnings", [])
    if not warns:
        await interaction.response.send_message(f"{user} に警告履歴はありません。", ephemeral=True)
        return
    lines = []
    for i, w in enumerate(warns[-15:], 1):
        ts = (ensure_utc(w["at"]).astimezone(_MODGUARD_JST).strftime("%Y/%m/%d")
              if w.get("at") else "?")
        lines.append(f"{i}. [{ts}] {w.get('reason','?')} — by {w.get('by_name','?')}")
    embed = discord.Embed(
        title=f"⚠️ {user} の警告履歴（通算{len(warns)}件・直近15件）",
        description="\n".join(lines)[:4000], color=0xF1C40F)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =============================================================================
# UI Views
# =============================================================================

async def _cleanup_consent_prompt(interaction: discord.Interaction):
    """個別の同意プロンプトは解決後に削除。ただし:
    - ピン留めされた常設パネル(/notify_migrate)は消さない。
    - 共有パネルや「他人宛ての個別プロンプト」を消さない（押した本人が宛先=メンション
      対象のときだけ削除）。他人のプロンプトを消すと、その人のpendingが未解決のまま
      24h後に自動剥奪される事故になるため。"""
    try:
        msg = interaction.message
        if not msg or msg.pinned:
            return
        if interaction.user in msg.mentions:
            await msg.delete()
    except Exception:
        pass


class NotifyConsentView(discord.ui.View):
    """通知ロールの同意/解除パネル（永続）。ボタンは常に押した本人(interaction.user)に作用する。
    新規付与時の個別プロンプトと、既存保持者向けの一斉予告（/notify_migrate）で共用。"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ 受け取る", style=discord.ButtonStyle.success,
                       custom_id="notifyconsent:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await users_col.update_one(
                {"_id": str(interaction.user.id)},
                {"$set": {"notify_consented": True},
                 "$unset": {"notify_consent_pending_since": "", "notify_consent_msg": ""}},
                upsert=True,
            )
            # ロールが無ければ付与（パネル経由のオプトインも兼ねる）
            guild = interaction.guild
            role  = guild.get_role(NOTIFY_ROLE_ID) if guild else None
            member = guild.get_member(interaction.user.id) if guild else None
            if role and member and role not in member.roles:
                try:
                    await member.add_roles(role, reason="通知ロール: 本人が受け取りに同意")
                except Exception as ae:
                    print(f"[WARN] consent accept add_roles: {ae}")
            await interaction.response.send_message(
                "✅ 通知を受け取る設定にしました。宣伝準備ができたらお知らせします。", ephemeral=True)
            await _cleanup_consent_prompt(interaction)
        except Exception as e:
            print(f"[ERROR] NotifyConsentView.accept: {e}")
            try:
                await interaction.response.send_message("❌ 失敗しました。時間をおいて再度お試しください。", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="❌ 通知を外す", style=discord.ButtonStyle.secondary,
                       custom_id="notifyconsent:decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            guild  = interaction.guild
            role   = guild.get_role(NOTIFY_ROLE_ID) if guild else None
            member = guild.get_member(interaction.user.id) if guild else None
            # ロール除去に失敗したら状態を変えず（consented維持・pending維持）失敗を伝える。
            # 「外しました」と言ったのにロールが残り鳴り続ける、を防ぐ。
            if role and member and role in member.roles:
                try:
                    await member.remove_roles(role, reason="通知ロール: 本人が解除")
                except Exception as re:
                    print(f"[WARN] consent decline remove_roles: {re}")
                    await interaction.response.send_message(
                        "❌ 解除に失敗しました（権限/ロール階層の可能性）。管理者にご連絡ください。",
                        ephemeral=True)
                    return
            await users_col.update_one(
                {"_id": str(interaction.user.id)},
                {"$set": {"notify_consented": False},
                 "$unset": {"notify_consent_pending_since": "", "notify_consent_msg": ""}},
                upsert=True,
            )
            await interaction.response.send_message(
                "🔕 通知を外しました。受け取りたくなったらいつでもこのパネルから戻せます。", ephemeral=True)
            await _cleanup_consent_prompt(interaction)
        except Exception as e:
            print(f"[ERROR] NotifyConsentView.decline: {e}")
            try:
                await interaction.response.send_message("❌ 失敗しました。時間をおいて再度お試しください。", ephemeral=True)
            except Exception:
                pass


class PersonalityView(discord.ui.View):
    def __init__(self, invoker: discord.Member | discord.User | None = None):
        super().__init__(timeout=60)
        self.message: discord.Message | None = None
        self.invoker = invoker
        for key, data in PERSONALITIES.items():
            btn = discord.ui.Button(
                label=data["label"],
                style=discord.ButtonStyle.primary,
                custom_id=f"personality_{key}",
            )
            btn.callback = self._make_callback(key)
            self.add_item(btn)

    def _make_callback(self, key: str):
        async def callback(interaction: discord.Interaction):
            await set_server_personality(key)
            personality = PERSONALITIES[key]
            changer     = interaction.user.display_name
            # Botのニックネームを人格名に変更
            try:
                nickname = personality.get("nickname", "空気くん")
                await interaction.guild.me.edit(nick=nickname)
            except Exception as ne:
                print(f"[WARN] nickname change: {ne}")
            embed = discord.Embed(
                title="🎭 性格が変わりました！",
                description=(
                    f"**{changer}** さんが変更しました\n\n"
                    f"新しい性格: **{personality['label']}**\n"
                    f"名前: **{personality.get('nickname', '空気くん')}**\n"
                    f"サーバー全員に反映されました。"
                ),
                color=personality["color"],
            )
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
        return callback

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class MyMaidView(discord.ui.View):
    """/mymaid 用: 自分専用のメイド人格(persona_override)を選ぶ。サーバー共通には影響しない。"""
    def __init__(self, invoker: discord.Member | discord.User | None = None):
        super().__init__(timeout=60)
        self.invoker = invoker
        for key, data in PERSONALITIES.items():
            btn = discord.ui.Button(
                label=data["label"],
                style=discord.ButtonStyle.secondary,
                custom_id=f"mymaid_{key}",
            )
            btn.callback = self._make_callback(key)
            self.add_item(btn)
        reset = discord.ui.Button(
            label="🔄 サーバー共通に戻す",
            style=discord.ButtonStyle.danger,
            custom_id="mymaid_reset",
        )
        reset.callback = self._make_callback(None)
        self.add_item(reset)

    def _make_callback(self, key: str | None):
        async def callback(interaction: discord.Interaction):
            # 個人設定なので本人のみ操作可
            if self.invoker and interaction.user.id != self.invoker.id:
                await interaction.response.send_message(
                    "これは他の人の設定パネルだよ。自分で /mymaid を使ってね。", ephemeral=True
                )
                return
            uid = str(interaction.user.id)
            if key is None:
                await users_col.update_one({"_id": uid}, {"$unset": {"persona_override": ""}}, upsert=True)
                desc = "専属人格を解除して、サーバー共通の人格に戻したよ。"
            else:
                await users_col.update_one(
                    {"_id": uid},
                    {"$set": {"persona_override": key, "name": interaction.user.name}},
                    upsert=True,
                )
                desc = (
                    f"あなた専用のメイドを **{PERSONALITIES[key]['label']}** にしたよ！\n"
                    "あなたへの返信だけこの人格になる（サーバー全体は変わらない）。"
                )
            for item in self.children:
                item.disabled = True
            embed = discord.Embed(title="🎀 専属メイド人格を更新", description=desc, color=0xFF69B4)
            await interaction.response.edit_message(embed=embed, view=self)
        return callback

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class LuckyTitleView(discord.ui.View):
    def __init__(self, title: str, member: discord.Member):
        super().__init__(timeout=60)
        self.title   = title
        self.member  = member
        self.message: discord.Message | None = None

    @discord.ui.button(label="ニックネームに反映する", style=discord.ButtonStyle.primary, emoji="✨")
    async def apply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("おいｗなにしようとしとんねん！", ephemeral=True)
            return
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            await interaction.response.send_message("エラーが発生したよ。", ephemeral=True)
            return
        await users_col.update_one(
            {"_id": str(interaction.user.id)},
            {"$set": {"title": self.title, "name": interaction.user.name}},
            upsert=True,
        )
        success = await apply_nickname(member, self.title)
        await interaction.response.send_message(
            f"✨ ニックネームを **「{self.title}」{interaction.user.name}** に変更したにゃ！" if success
            else "権限の関係でニックネームを変更できなかったよ…ごめんにゃ。",
            ephemeral=True,
        )
        button.disabled = True
        button.label    = "反映済み"
        await interaction.message.edit(view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

# =============================================================================
# 通知タスク
# =============================================================================

# 招待ボーナス設定 { 招待人数: XP }
INVITE_BONUSES = {1: 200, 3: 500, 5: 1000, 10: 2000}

@client.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """通知ロールが新規付与された人に同意プロンプトを出す（案A: 24h未確認で自動剥奪）。"""
    try:
        before_ids = {r.id for r in before.roles}
        after_ids  = {r.id for r in after.roles}
        if not (NOTIFY_ROLE_ID in after_ids and NOTIFY_ROLE_ID not in before_ids):
            return  # 通知ロールの「新規付与」以外は無視

        udoc = await users_col.find_one(
            {"_id": str(after.id)},
            {"notify_consented": 1, "notify_consent_pending_since": 1},
        )
        if udoc:
            if udoc.get("notify_consented") is True:
                return  # 既に同意済み（再付与）
            if udoc.get("notify_consent_pending_since"):
                return  # 既にプロンプト送信済み（二重送信防止）

        ch = client.get_channel(NOTIFY_PING_CHANNEL_ID)
        if not ch:
            print("[WARN] on_member_update: 通知chが見つからない")
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        msg = await ch.send(
            f"{after.mention} 通知ロールが付きました。\n"
            f"🔔 宣伝が打てるようになると、このロールを **全体で1時間に1回まで** メンションします"
            f"（深夜0-7時は鳴りません）。受け取りますか？\n"
            f"※ **24時間** [✅受け取る] を押さないと、自動でロールを外します（あとで付け直せます）。",
            view=NotifyConsentView(),
            allowed_mentions=discord.AllowedMentions(users=[after], roles=False, everyone=False),
        )
        await users_col.update_one(
            {"_id": str(after.id)},
            {"$set": {"notify_consent_pending_since": now,
                      "notify_consent_msg": f"{ch.id}:{msg.id}"}},
            upsert=True,
        )
        print(f"[notify] 新規付与→同意プロンプト送信: {after.id}")
    except Exception as e:
        print(f"[ERROR] on_member_update(consent): {e}")


@client.event
async def on_member_join(member: discord.Member):
    """招待ボーナス処理"""
    try:
        invites_after = await member.guild.invites()
        inviter_id    = None
        used_code     = None
        for inv in invites_after:
            prev_uses = _invite_snapshot.get(inv.code, 0)
            if (inv.uses or 0) > prev_uses:
                inviter_id = str(inv.inviter.id) if inv.inviter else None
                used_code  = inv.code
                _invite_snapshot[inv.code] = inv.uses or 0
                break
        # スナップショット更新
        for inv in invites_after:
            _invite_snapshot[inv.code] = inv.uses or 0

        if not inviter_id:
            return

        # 招待者の累計招待数を更新
        inviter_doc = await users_col.find_one_and_update(
            {"_id": inviter_id},
            {"$inc": {"invite_count": 1}},
            upsert=True, return_document=True,
        )
        invite_count = inviter_doc.get("invite_count", 1)

        # マイルストーンボーナス
        bonus = INVITE_BONUSES.get(invite_count, 0)
        if bonus:
            await users_col.update_one(
                {"_id": inviter_id},
                {"$inc": {"xp": bonus}},
            )
            general_ch = member.guild.get_channel(GENERAL_CHANNEL_ID)
            inviter_member = member.guild.get_member(int(inviter_id))
            if general_ch and inviter_member:
                await general_ch.send(
                    f"🎉 {inviter_member.mention} さんが **{invite_count}人目** の招待を達成！"
                    f" **+{bonus:,} XP** ボーナス獲得！"
                )
            print(f"[invite] {inviter_id} が {invite_count}人招待 +{bonus}XP")
    except Exception as e:
        print(f"[ERROR] on_member_join: {e}")


async def weekly_ranking_task():
    """毎週日曜JST正午にランキングを表チャンネルに投稿"""
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
            # 日曜(weekday=6) 正午(12:00)に投稿
            if now_jst.weekday() == 6 and now_jst.hour == 12 and now_jst.minute < 10:
                await post_weekly_ranking()
        except Exception as e:
            print(f"[ERROR] weekly_ranking_task: {e}")
        await asyncio.sleep(600)  # 10分ごとにチェック


async def post_weekly_ranking():
    guild = next(iter(client.guilds), None)
    if not guild:
        return
    ch = guild.get_channel(GENERAL_CHANNEL_ID)
    if not ch:
        return

    # 上位20名取得
    top_users = await users_col.find().sort("xp", -1).limit(20).to_list(length=20)
    if not top_users:
        return

    now_jst  = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    medals   = ["🥇", "🥈", "🥉"]
    lines    = []
    for i, doc in enumerate(top_users):
        rank_name, _, _, _ = get_rank_info(doc.get("xp", 0))
        medal = medals[i] if i < 3 else f"**{i+1}.**"
        name  = doc.get("name", "不明")
        xp    = doc.get("xp", 0)
        lines.append(f"{medal} {name}　`{rank_name}`　**{xp:,} XP**")

    embed = discord.Embed(
        title=f"📊 週次XPランキング",
        description="\n".join(lines),
        color=0xFFD700,
    )
    embed.set_footer(text=f"{now_jst.strftime('%Y/%m/%d')} 集計 • /rank で自分のステータスを確認！")
    await ch.send(embed=embed)
    print(f"[ranking] 週次ランキング投稿完了")


_last_idle_post: datetime.datetime | None = None  # 前回の時間ベース自発投稿（UTC）


async def _generate_idle_opener() -> tuple[str, dict] | None:
    """場が静かなときの自発的な一言を、現在のサーバー人格の口調で生成する。"""
    personality_key = await get_server_personality()
    personality = PERSONALITIES.get(personality_key, PERSONALITIES[DEFAULT_PERSONALITY])
    try:
        raw_summary   = await get_latest_summary()
        smart_summary = build_smart_summary(raw_summary) if raw_summary else ""
    except Exception as e:
        print(f"[WARN] idle opener summary失敗: {e}")
        smart_summary = ""
    directive = (
        "（これは誰かへの返信ではありません。いまチャンネルが少し静かなので、あなたから場に投げる"
        "『最初の一言』を作ってください。下の『サーバーの最新状況』にある実際の話題・流れに触れて、"
        "会話が再開するきっかけになる軽い一言を。特定個人を責めたり質問攻めにしたりしない。"
        "前置きや「メイド:」等のラベル無しで本文のみ・1〜2文）"
    )
    base = personality["booster_prompt"].format(
        name="みなさん",
        history="（直近のメイドとの個別会話履歴はなし。場全体への語りかけです）",
        content=directive,
    )
    if smart_summary:
        prompt = "【サーバーの最新状況（ここにある話題に触れよ）】\n" + smart_summary + "\n\n---\n" + base
    else:
        prompt = base
    text = await _run_ai_booster(prompt)
    if not text or "（" in text:
        return None
    return text, personality


async def idle_chatter_task():
    """活動時間帯にチャンネルが静かになったら、メイドが自分から軽く話しかける。
    安全装置: 直近に人間の発言があるチャンネルでのみ投稿する（過疎チャンネルでは黙る）。"""
    global _last_idle_post
    await client.wait_until_ready()
    target_id = IDLE_CHAT_CHANNEL_ID or GENERAL_CHANNEL_ID
    while not client.is_closed():
        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            now_jst = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
            in_hours = IDLE_HOURS_START <= now_jst.hour < IDLE_HOURS_END
            recently_posted = (
                _last_idle_post is not None and
                (now_utc - _last_idle_post).total_seconds() < IDLE_MIN_GAP_HRS * 3600
            )
            if in_hours and not recently_posted:
                ch = client.get_channel(target_id)
                if ch:
                    last_human      = None
                    last_msg_is_bot = False
                    first = True
                    async for m in ch.history(limit=20):
                        if first:
                            last_msg_is_bot = (m.author.id == client.user.id)
                            first = False
                        if not m.author.bot:
                            last_human = m
                            break
                    # 最新がメイド自身でない＆直近に人間発言がある場合のみ検討
                    if last_human is not None and not last_msg_is_bot:
                        gap = (now_utc - ensure_utc(last_human.created_at)).total_seconds()
                        # 「静か(QUIET_MIN以上)」かつ「過疎でない(RECENT_HRS以内)」という谷間だけ狙う
                        if IDLE_QUIET_MIN * 60 <= gap <= IDLE_RECENT_HRS * 3600 and random.random() < IDLE_POST_CHANCE:
                            result = await _generate_idle_opener()
                            if result:
                                text, personality = result
                                await ch.send(f"{personality['icon']} {text}")
                                _last_idle_post = now_utc
                                print(f"[idle] 自発投稿: {text[:40]}")
        except Exception as e:
            print(f"[ERROR] idle_chatter_task: {e}")
        await asyncio.sleep(IDLE_CHECK_INTERVAL)


def _in_quiet_hours_jst() -> bool:
    """静音帯(JST NOTIFY_QUIET_START〜END)内なら True。"""
    h = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).hour
    return NOTIFY_QUIET_START_JST <= h < NOTIFY_QUIET_END_JST


async def _sweep_consent_timeouts(now: datetime.datetime):
    """新規付与の同意待ちが NOTIFY_CONSENT_TIMEOUT を超過したらロール自動剥奪。

    重要: 状態(notify_consented:False / pendingクリア)を確定するのは、ロールが
    実際に外れたことを確認できた場合のみ。ギルド/ロール未解決や remove_roles 失敗時は
    pending を残し、次周期で再試行する（DBとロールのdesync＝永久に剥奪されない事故の回避）。
    """
    try:
        cutoff = now - datetime.timedelta(seconds=NOTIFY_CONSENT_TIMEOUT)
        guild  = client.get_guild(HOME_GUILD_ID)
        role   = guild.get_role(NOTIFY_ROLE_ID) if guild else None
        if not guild or not role:
            print("[WARN] consent sweep: guild/role未解決のためスキップ（次周期で再試行）")
            return
        async for u in users_col.find({"notify_consent_pending_since": {"$lt": cutoff}}):
            uid    = u["_id"]
            member = guild.get_member(int(uid))
            if member is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason="通知ロール: 同意24h無応答のため自動解除")
                except Exception as re:
                    print(f"[WARN] consent sweep remove_roles({uid}): {re} — pending維持で再試行")
                    continue  # 剥奪失敗 → 状態を確定せず次周期で再試行
            # ここに到達 = メンバー不在(退出済み) or ロール除去成功 or 元々ロール無し。
            # プロンプトを掃除
            ref = u.get("notify_consent_msg")
            if ref and ":" in ref:
                try:
                    cid, mid = (int(x) for x in ref.split(":", 1))
                    pch = client.get_channel(cid)
                    if pch:
                        pmsg = await pch.fetch_message(mid)
                        await pmsg.delete()
                except Exception:
                    pass
            await users_col.update_one(
                {"_id": uid},
                {"$set": {"notify_consented": False},
                 "$unset": {"notify_consent_pending_since": "", "notify_consent_msg": ""}},
            )
            print(f"[notify] 同意24h無応答 → ロール解除: {uid}")
    except Exception as e:
        print(f"[ERROR] consent sweep: {e}")


async def notification_task():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            # 新規付与の同意タイムアウト処理（毎周期）
            await _sweep_consent_timeouts(now)

            ch = client.get_channel(NOTIFY_PING_CHANNEL_ID)
            if ch:
                ready = []
                async for doc in system_col.find({"notified": False}):
                    bid = doc["_id"]
                    if bid not in BOT_CONFIG or not doc.get("last_bump_at"):
                        continue
                    if (now - ensure_utc(doc["last_bump_at"])).total_seconds() >= BOT_CONFIG[bid]["cd"]:
                        ready.append(bid)

                # 静音帯ならpingしない（notifiedはFalseのまま持ち越し→明けてから1本）
                state = await system_col.find_one({"_id": "notify_state"})
                # /notify_migrate 未実行の間は一切pingしない（既存40人への不意打ち防止）
                migrated = bool(state and state.get("migrated"))
                if ready and not migrated:
                    print(f"[notify] 宣伝準備OK({len(ready)}件)だが未解禁: /notify_migrate を実行すると通知が始まります")
                if ready and migrated and not _in_quiet_hours_jst():
                    # グローバル上限: 全体で1時間に1回まで
                    last_ping = state.get("last_ping_at") if state else None
                    allowed   = (last_ping is None or
                                 (now - ensure_utc(last_ping)).total_seconds() >= NOTIFY_GLOBAL_COOLDOWN)
                    if allowed:
                        role    = ch.guild.get_role(NOTIFY_ROLE_ID) if ch.guild else None
                        mention = (role.mention + "\n") if role else ""
                        await ch.send(
                            f"{mention}🔔 **宣伝準備完了！**\n" +
                            "\n".join(f"✅ **{BOT_CONFIG[b]['name']}**" for b in ready),
                            allowed_mentions=discord.AllowedMentions(
                                everyone=False, users=False,
                                roles=[role] if role else False,
                            ),
                        )
                        await system_col.update_one(
                            {"_id": "notify_state"}, {"$set": {"last_ping_at": now}}, upsert=True,
                        )
                        for b in ready:
                            await system_col.update_one({"_id": b}, {"$set": {"notified": True}})
                    # 上限内なら送らず、notifiedはFalseのまま次周期に持ち越す
        except discord.errors.HTTPException as e:
            if "429" in str(e):
                print(f"[WARN] Notify 429: レート制限中、60秒待機")
                await asyncio.sleep(60)  # 429時は60秒余分に待つ
            else:
                print(f"[ERROR] Notify: {e}")
        except Exception as e:
            print(f"[ERROR] Notify: {e}")
        await asyncio.sleep(600)

# =============================================================================
# エントリーポイント
# =============================================================================

async def _main():
    # Webサーバーを最初に起動（Renderのポートチェックを通すため）
    await start_web_server()
    print("[web] Webサーバー起動完了")

    retry = 0
    # client は起動時に一度だけ生成済み（グローバル）。
    # 再生成すると @client.tree.command で登録したコマンドが消えるため、
    # 429時はclose→再接続のみ行う。
    while True:
        try:
            await client.start(TOKEN)
        except discord.errors.HTTPException as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                wait = min(300 * (2 ** retry), 3600)  # 最大1時間
                print(f"[WARN] Discord 429 rate limited. {wait}秒後に再試行... (retry={retry})")
                try:
                    if not client.is_closed():
                        await client.close()
                except Exception:
                    pass
                await asyncio.sleep(wait)
                retry += 1
            else:
                print(f"[ERROR] Discord HTTPException: {e}")
                raise
        except Exception as e:
            print(f"[ERROR] client.start failed: {e}")
            raise
        finally:
            try:
                if not client.is_closed():
                    await client.close()
            except Exception:
                pass

if __name__ == "__main__":
    asyncio.run(_main())
