"""
focus_summary.py

特定メンバー or キーワードに絞った要約を作成し、
人物の場合はMongoDBのプロフィールに自動反映する。

環境変数:
  FOCUS_TYPE:   "member" or "keyword"
  FOCUS_TARGET: メンバーID or キーワード文字列
  FOCUS_NAME:   表示名（メンバー表示名 or キーワード）
"""

import os
import json
import re
import time
from datetime import datetime, timezone, timedelta

import requests
from pymongo import MongoClient
from google import genai
from google.genai import types

from embed_util import embed_query, cosine   # Tier3: keyword 意味検索（summaries embedding 規約）

FOCUS_TYPE    = os.environ["FOCUS_TYPE"]    # "member" or "keyword"
FOCUS_TARGET  = os.environ["FOCUS_TARGET"]  # user_id or keyword
FOCUS_NAME    = os.environ["FOCUS_NAME"]    # 表示名

DISCORD_BOT_TOKEN  = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_GUILD_ID   = int(os.environ["DISCORD_GUILD_ID"])
CHANNEL_IDS_RAW    = os.environ.get("DISCORD_CHANNEL_IDS", "")
CHANNEL_IDS        = [int(c.strip()) for c in CHANNEL_IDS_RAW.split(",") if c.strip()]
EXCLUDE_IDS_RAW    = os.environ.get("EXCLUDE_CHANNEL_IDS", "")
EXCLUDE_IDS        = set(int(c.strip()) for c in EXCLUDE_IDS_RAW.split(",") if c.strip())
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
MONGODB_URI        = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
SUMMARY_CHANNEL_ID = os.environ["SUMMARY_CHANNEL_ID"]
# /focus を実行したチャンネルへ投稿する。未指定（手動dispatch等）の場合は
# 従来どおり SUMMARY_CHANNEL_ID にフォールバックする。
FOCUS_CHANNEL_ID   = os.environ.get("FOCUS_CHANNEL_ID", "").strip() or SUMMARY_CHANNEL_ID

MODEL          = "models/gemma-4-31b-it"
MODEL_FALLBACK = "models/gemma-4-26b-a4b-it"
DB_NAME        = "discord_bot_db"
JST            = timezone(timedelta(hours=9))
FETCH_DAYS     = 7    # 直近何日分の生ログを取得するか
SUMMARY_LOOKBACK_DAYS_MEMBER  = 365    # member: 人物は1年遡る（密度と負荷のバランス）
SUMMARY_LOOKBACK_DAYS_KEYWORD = 730    # keyword: トピックは深い歴史を持つので2年遡る
MAX_LOG_CHARS         = 10000  # 生ログ(直近7日)は「最近の断面」の補助なので絞る（長期偏重を優先）
MAX_SUMMARY_CHARS     = 24000  # 長期アーカイブが主役。月ごと按分でこの予算を1〜2年に配分する
MAX_MEMBER_CONTEXT_CHARS = 4000  # 既知情報(profile/claims/memories)をプロンプトに入れる最大文字数(Tier2)
# Tier3 意味検索（keyword専用）。doc重心 vs 短い検索語 の cosine は圧縮・密集しがちなので
# 閾値は低めに置き、毎回 top-N の sim を必ずログする＝実測で調整する（推測で固定しない）。
KEYWORD_SEM_THRESHOLD = float(os.environ.get("KEYWORD_SEM_THRESHOLD", "0.45"))
KEYWORD_SEM_TOPK      = int(os.environ.get("KEYWORD_SEM_TOPK", "20"))   # 意味検索で追加採用する最大doc数
EMBED_BLOCK_CHARS     = 1000   # 意味検索で当たった doc から載せる本文の上限（needle行抽出と違い全文系なので絞る）

# 自サーバーのチャット分析（内部バッチ）なので safety ブロックで空レスポンスにならないよう緩和。
# 構築失敗時は None（=SDKデフォルト）にフォールバックしてモジュール読込を壊さない。
try:
    SAFETY_OFF = [
        types.SafetySetting(category=c, threshold="BLOCK_NONE")
        for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                  "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
    ]
except Exception as _e:
    print(f"[WARN] safety_settings 構築失敗（デフォルト使用）: {_e}")
    SAFETY_OFF = None

# =============================================================================
# プロンプト
# =============================================================================

MEMBER_FOCUS_PROMPT = """\
以下はDiscordサーバーの「長期の要約アーカイブ（過去数ヶ月）」と「直近{days}日分の生チャットログ」です。
両方を突き合わせ、長期の傾向と直近の動きの両面から、
「{name}」というメンバーの発言・言及に絞って分析し、
この人物についての詳細な観察レポートを書いてください。
冒頭に「これまでに蓄積した既知情報」が提示される場合があります。それは過去の分析の蓄積であり、
今回のログ・要約で裏付け・更新・新情報の追加を行い、矛盾があれば新しい証拠を優先してください。
古い情報の単なる引き写しは避け、変化や新事実を重視すること。
前置き・導入文は不要です。各セクションの見出しから即座に書き始めてください。

「数ヶ月の変遷・時系列」では、長期アーカイブの各行頭にある [YYYY-MM-DD] の日付を必ず引用し、
過去数ヶ月から現在への「変化・継続・一過性の出来事」を時系列で具体的に書くこと。
直近{days}日だけで分かることの繰り返しは禁止。1ヶ月以上前の話題・エピソード・関係性の移り変わりを優先して拾うこと。

## 発言の口調・語彙
## 性格・行動パターン
## サーバー内での立場・役割
## よく絡むメンバーと関係性
## 関心・よく話すトピック
## 印象的な発言・エピソード
## 数ヶ月の変遷・時系列
## 総合評価
"""

KEYWORD_FOCUS_PROMPT = """\
以下はDiscordサーバーの「長期の要約アーカイブ（過去数ヶ月）」と「直近{days}日分の生チャットログ」です。
両方を突き合わせ、長期の経緯と直近の状況の両面から、
「{keyword}」に関する発言・話題に絞って分析し、
このトピックについての詳細なレポートを書いてください。
前置き・導入文は不要です。各セクションの見出しから即座に書き始めてください。

## このトピックの概要
## 主な議論の流れ
## 関わったメンバーと立場
## 盛り上がった瞬間・転換点
## 結論・現在の状況
## 関連する他のトピック
"""

PROFILE_UPDATE_PROMPT = """\
以下は「{name}」についての観察レポートです。
このレポートから、Discordメイドボットが会話に使う
プロフィール情報をJSON形式で抽出してください。
前置き・説明文・```は不要です。JSONのみ出力してください。
情報が不明な項目はnullにしてください。

出力形式:
{{
  "tone": "口調の特徴（例: テンション高め・敬語なし・短文多め）",
  "vocabulary": "よく使う語彙・口癖",
  "personality": "性格の概要",
  "background": "サーバー内の立場・役割",
  "relations": "よく絡むメンバーや関係性",
  "interests_vibe": "関心・よく話すトピック・雰囲気"
}}

【観察レポート】
{report}
"""

SECTION_ICONS_MEMBER = {
    "発言の口調": "🗣️", "性格": "🧠", "立場": "👑",
    "よく絡む": "🤝", "関心": "💡", "印象的": "⭐",
    "変遷": "🕰️", "総合": "📋",
}
SECTION_ICONS_KEYWORD = {
    "概要": "📌", "議論の流れ": "🔄", "関わった": "👥",
    "盛り上がった": "🔥", "結論": "✅", "関連": "🔗",
}

# analyze_personality.py が profile.bigfive に書く5因子（TIPI-J / 小塩ら2012）。
# focus は読むだけ＝bigfive の所有者は analyze_personality のみ（書き戻さず較正値を汚さない）。
# バッチ間で import しない方針なので表示順を固定するためここに複製する。
BIGFIVE_FACTORS_JA = {
    "openness":          "開放性",
    "conscientiousness": "誠実性",
    "extraversion":      "外向性",
    "agreeableness":     "協調性",
    "neuroticism":       "情緒不安定さ",
}
CONF_JA = {"low": "低", "mid": "中", "high": "高"}


# =============================================================================
# ログ取得
# =============================================================================

BASE_URL     = "https://discord.com/api/v10"
REST_HEADERS = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
    "Content-Type":  "application/json",
}

# Discord API 叩きすぎ防止のガード（fetch_discord_logs.py と同方針）。
# /focus は手動で何度でも叩けて本番と同一トークンなので、繁忙期の青天井ページングで
# レート制限/BAN を踏むと本番Botまで巻き添えになる。上限は実トラフィックの十分上に置き、
# 通常運用では発火せず暴走時のみ打ち切る。
MAX_TOTAL_API_CALLS   = int(os.environ.get("MAX_TOTAL_API_CALLS", "400"))
MAX_PAGES_PER_CHANNEL = int(os.environ.get("MAX_PAGES_PER_CHANNEL", "50"))
_api_call_count = 0


def _api_get(path: str, params: dict = None):
    global _api_call_count
    # C: グローバル上限 — 超えたら打ち切り（fetch_logs の per-channel ループの break に繋がる）
    if _api_call_count >= MAX_TOTAL_API_CALLS:
        print(f"[WARN] Total API call limit reached ({MAX_TOTAL_API_CALLS}), aborting fetch early")
        return None
    _api_call_count += 1

    url = BASE_URL + path
    for attempt in range(5):
        resp = requests.get(url, headers=REST_HEADERS, params=params, timeout=30)

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
            print(f"[fetch] Rate limited. Waiting {retry_after:.1f}s...")
            time.sleep(retry_after + 0.5)
            continue
        if resp.status_code in (403, 404):
            return None
        print(f"[fetch] API error {resp.status_code}: {path}")
        time.sleep(1)
    return None


def _datetime_to_snowflake(dt: datetime) -> int:
    discord_epoch = 1420070400000
    return (int(dt.timestamp() * 1000) - discord_epoch) << 22


def is_excluded(channel: dict) -> bool:
    if channel.get("nsfw", False):
        return True
    return int(channel["id"]) in EXCLUDE_IDS


def fetch_logs(days: int) -> list[dict]:
    after_dt     = datetime.now(timezone.utc) - timedelta(days=days)
    after_sf     = _datetime_to_snowflake(after_dt)
    TEXT_TYPES   = {0, 5}
    THREAD_TYPES = {11, 12}

    all_channels = _api_get(f"/guilds/{DISCORD_GUILD_ID}/channels") or []

    if CHANNEL_IDS:
        id_set = set(CHANNEL_IDS)
        base = [ch for ch in all_channels if int(ch["id"]) in id_set and ch["type"] in TEXT_TYPES]
    else:
        base = [ch for ch in all_channels if ch["type"] in TEXT_TYPES]

    targets    = []
    parent_ids = set()
    for ch in base:
        if is_excluded(ch):
            continue
        targets.append(ch)
        parent_ids.add(int(ch["id"]))

    thread_resp = _api_get(f"/guilds/{DISCORD_GUILD_ID}/threads/active") or {}
    for t in thread_resp.get("threads", []):
        if int(t.get("parent_id", 0)) in parent_ids and not is_excluded(t) and t["type"] in THREAD_TYPES:
            targets.append(t)

    msgs = []
    for ch in targets:
        ch_id   = int(ch["id"])
        ch_name = ch.get("name", str(ch_id))
        last_id = after_sf
        # B: ページング上限 — 1ch あたり最大 MAX_PAGES_PER_CHANNEL ページで打ち切り。
        #    繁忙期に len(batch)<100 まで止まらない青天井ページングを防ぐ。
        for _ in range(MAX_PAGES_PER_CHANNEL):
            params = {"limit": 100, "after": str(last_id)}
            batch  = _api_get(f"/channels/{ch_id}/messages", params=params)
            if not batch:
                break
            batch = list(reversed(batch))
            if not batch:
                break
            for msg in batch:
                author = msg.get("author", {})
                if author.get("bot") or msg.get("webhook_id"):
                    continue
                msgs.append({
                    "channel":   ch_name,
                    "author":    author.get("global_name") or author.get("username", ""),
                    "author_id": author.get("id", ""),
                    "timestamp": msg.get("timestamp", ""),
                    "content":   msg.get("content", ""),
                })
            last_id = int(batch[-1]["id"])
            if len(batch) < 100:
                break
            time.sleep(0.3)
        time.sleep(0.2)

    msgs.sort(key=lambda m: m["timestamp"])
    print(f"[fetch] {len(msgs)} messages")
    return msgs


def filter_logs(msgs: list[dict], needles: list[str] = None) -> str:
    """フォーカス対象に絞ってログをテキスト化。

    member の場合、needles に nickname_map のエイリアスを含めると
    「たろー」のような愛称での発言・言及も拾える。
    """
    filtered = []

    if FOCUS_TYPE == "member":
        target_id = FOCUS_TARGET
        nlist     = [n.lower() for n in (needles or [FOCUS_NAME]) if n]
        for m in msgs:
            author_l  = m["author"].lower()
            content_l = m["content"].lower()
            # 発言者が対象 or メッセージ内に名前/エイリアス/IDが含まれる
            is_author  = m["author_id"] == target_id or any(n in author_l for n in nlist)
            is_mention = any(n in content_l for n in nlist) or f"<@{target_id}>" in m["content"]
            if is_author or is_mention:
                filtered.append(m)
    else:
        kw = FOCUS_TARGET.lower()
        for m in msgs:
            if kw in m["content"].lower() or kw in m["channel"].lower():
                filtered.append(m)

    lines = []
    for m in filtered:
        ts = m["timestamp"][:16].replace("T", " ")
        lines.append(f"[{ts}] #{m['channel']} {m['author']}: {m['content']}")
    return "\n".join(lines)


# =============================================================================
# 長期アーカイブ（summaries）からの絞り込み
# =============================================================================

def _strip_title_decoration(name: str) -> set[str]:
    """「二つ名」本名 / 【二つ名】本名 等の装飾を剥がして素の名前候補を返す。
    例: 「わっふる教団司祭」チャコ → {'わっふる教団司祭', 'チャコ'}。
    /as の title が Discord 表示名に「title」付きで反映されるため、summaries/生ログ上の
    素の呼称（チャコ）と表示名（装飾付き）がズレてヒットしない問題に対応。"""
    out = set()
    if not name:
        return out
    stripped = re.sub(r"[「」『』【】《》〈〉\[\]()（）\"'`･・|]", " ", name)
    tokens   = [t for t in stripped.split() if len(t) >= 2]
    out.update(tokens)
    joined = "".join(tokens)
    if len(joined) >= 2:
        out.add(joined)
    return out


def build_member_needles(system_col, users_col) -> list[str]:
    """フォーカス対象を summaries/生ログで拾うための検索語を集める。
    FOCUS_NAME（装飾付き表示名）・素名・users.name/title・nickname_map の相互展開を統合。"""
    variants = {FOCUS_NAME}
    variants |= _strip_title_decoration(FOCUS_NAME)

    # users ドキュメントの記録名・二つ名も候補に
    try:
        udoc = users_col.find_one({"_id": FOCUS_TARGET}, {"name": 1, "title": 1}) or {}
        for key in ("name", "title"):
            if udoc.get(key):
                variants.add(udoc[key])
                variants |= _strip_title_decoration(udoc[key])
    except Exception as e:
        print(f"[WARN] users doc 読込失敗: {e}")

    # nickname_map: variants のいずれかに一致する alias/canon を相互展開（2パスで推移的に）
    try:
        ndoc = system_col.find_one({"_id": "nickname_map"}) or {}
        nmap = ndoc.get("map", {})
        for _ in range(2):
            for alias, canon in nmap.items():
                if alias in variants or canon in variants:
                    variants.add(alias)
                    variants.add(canon)
    except Exception as e:
        print(f"[WARN] nickname_map 読込失敗: {e}")

    # 1文字は誤爆するので2文字以上のみ採用
    return [v for v in variants if v and len(v) >= 2]


def _lookback_days() -> int:
    """focusタイプ別の遡及日数。member=1年・keyword=2年。"""
    return SUMMARY_LOOKBACK_DAYS_MEMBER if FOCUS_TYPE == "member" else SUMMARY_LOOKBACK_DAYS_KEYWORD


def _select_across_months(matched: list, budget: int) -> list:
    """matched=[(date, block)]（新しい順）を、月ごとに予算を按分して選ぶ。
    直近偏重の「新しい順で予算切り」をやめ、各月を必ず代表させる。
    余った予算は全体の新しい順で埋める。返りは (date, block) のリスト（順不同）。
    """
    if not matched:
        return []
    buckets = {}                       # "YYYY-MM" -> [(date, block)]（月内も新しい順）
    for date, block in matched:
        buckets.setdefault(date[:7], []).append((date, block))
    months = sorted(buckets)           # 古→新
    share  = max(1, budget // len(months))

    selected, chosen, total = [], set(), 0
    # 第1パス: 各月に share まで（月内は新しい順）→ 全月を代表させる
    for m in months:
        used = 0
        for item in buckets[m]:
            block = item[1]
            if used + len(block) > share:
                continue
            selected.append(item); chosen.add(id(block))
            used += len(block); total += len(block)
    # 第2パス: 余り予算を全体の新しい順で埋める
    if total < budget:
        for item in matched:           # 新しい順
            block = item[1]
            if id(block) in chosen or total + len(block) > budget:
                continue
            selected.append(item); chosen.add(id(block))
            total += len(block)
    return selected


def fetch_relevant_summaries(summaries_col, needles: list[str], query_embedding=None) -> str:
    """summaries アーカイブ（永久保持）から関連ブロックを抽出してテキスト化する。

    ハイブリッド recall（keyword の Tier3）:
      ① needle（文字列一致）= 確実な行レベル抽出。recall の【床】。member/keyword 共通。
      ② 意味検索（query_embedding 指定時=keyword専用）= needle に掛からないが意味的に近い
         doc を doc重心 embedding の cosine で救済。①で当たった doc は除外（重複回避）。
    両者を月按分（_select_across_months）に合流させ、時系列の分散を保ったまま予算配分する。
    needle ブロックを先に matched へ積む＝月バケツ内で精密な needle が曖昧な意味検索より優先される。
    """
    needles_l = [n.lower() for n in needles if n]
    if not needles_l:
        return ""

    # created_at は全書き手が .isoformat()（UTC）文字列なので ISO 辞書順比較で日付フィルタ可。
    # 窓を 30 日チャンクに分割し各チャンクを個別クエリ＝生成密度に関係なく窓全体へ必ず到達する。
    # （単一クエリ＋newest-N limit だと密度が高い期間で古い側が打ち切られ、窓が縮む＝旧バグ）
    lookback   = _lookback_days()
    now        = datetime.now(timezone.utc)
    CHUNK_DAYS = 30
    PER_CHUNK  = 700                       # 1チャンク(≒1ヶ月)あたりの走査上限（高密度でも1ヶ月分カバー）
    n_chunks   = (lookback + CHUNK_DAYS - 1) // CHUNK_DAYS
    window_start = (now - timedelta(days=lookback)).strftime("%Y-%m-%d")

    # embedding は 3072次元で重い。意味検索する keyword モードのときだけ射影する
    # （member は意味検索しないので全 doc のベクトル転送は純粋な無駄）。
    proj = {"summary": 1, "created_at": 1, "retro_date": 1}
    if query_embedding is not None:
        proj["embedding"] = 1

    matched, scanned_total = [], 0
    # emb_seen は embedding を持つ doc の【総数】(needle該当も含む)＝バックフィル進捗の真の指標。
    # sem_cands は needle 非該当のみ（意味検索の救済対象）。両者を分けて数える。
    sem_cands, emb_seen = [], 0
    for i in range(n_chunks):
        chunk_end   = now - timedelta(days=i * CHUNK_DAYS)
        chunk_start = now - timedelta(days=min((i + 1) * CHUNK_DAYS, lookback))
        try:
            docs = list(summaries_col.find(
                {"summary": {"$exists": True},
                 "created_at": {"$gte": chunk_start.isoformat(), "$lt": chunk_end.isoformat()}},
                proj,
                sort=[("created_at", -1)],
            ).limit(PER_CHUNK))
        except Exception as e:
            print(f"[WARN] summaries 取得失敗(chunk{i}): {e}")
            continue
        scanned_total += len(docs)
        for d in docs:
            summ = d.get("summary", "")
            if not summ:
                continue
            date = d.get("retro_date") or str(d.get("created_at", ""))[:10]
            hits = [ln.strip() for ln in summ.split("\n")
                    if ln.strip() and any(n in ln.lower() for n in needles_l)]
            emb = d.get("embedding") if query_embedding is not None else None
            if emb:
                emb_seen += 1
            if hits:
                matched.append((date, f"[{date}]\n" + "\n".join(hits)))
            elif emb:
                # needle 非該当 doc のみ意味検索の候補に（needle 該当は①で精密に拾えている）
                sem_cands.append((cosine(query_embedding, emb), date, summ))

    # 意味検索: 閾値以上の上位 K doc を needle ブロックの【後】に積む（月バケツ内で needle 優先）
    sem_selected = 0
    if query_embedding is not None:
        sem_cands.sort(key=lambda x: x[0], reverse=True)
        # 床を割らない: 閾値で誰も通らなくても needle 結果はそのまま返る
        for sim, date, summ in sem_cands:
            if sim < KEYWORD_SEM_THRESHOLD or sem_selected >= KEYWORD_SEM_TOPK:
                break
            block = (f"[{date}]\n（意味検索 sim={sim:.2f}・キーワード非含有だが関連）\n"
                     f"{summ.strip()[:EMBED_BLOCK_CHARS]}")
            matched.append((date, block))
            sem_selected += 1
        # 閾値調整のための実測ログ（推測で閾値を決めない・top10の sim と日付を必ず出す）
        top = sem_cands[:10]
        top_str = " ".join(f"{s:.2f}({dt})" for s, dt, _ in top) if top else "なし"
        print(f"[focus][sem] embedding付きdoc(全)={emb_seen}件 意味検索候補(needle非該当)={len(sem_cands)}件 "
              f"候補top10sim={top_str}")
        print(f"[focus][sem] 閾値={KEYWORD_SEM_THRESHOLD} 採用={sem_selected}/{KEYWORD_SEM_TOPK} "
              f"（embedding付きdoc(全)=0なら未バックフィル／sim全部低なら重心がぼやけ過ぎ＝閾値下げ or 無効化検討）")

    # 月ごと按分で数ヶ月を満遍なく代表させる（直近偏重の予算切りを回避）
    selected = _select_across_months(matched, MAX_SUMMARY_CHARS)
    selected.sort(key=lambda x: x[0])   # 時系列順（古→新）でプロンプトへ＝変遷が読みやすい

    # 診断: 窓全体を走査したか（走査件数＋窓開始日）／マッチ期間／実採用期間
    print(f"[focus][debug] 走査={scanned_total}件 窓開始={window_start}（ここまでクエリ済＝これ以前のマッチが無ければ当時別名/不在）")
    if matched:
        md = [d for d, _ in matched]
        sd = [d for d, _ in selected]
        n_months = len({d[:7] for d in sd})
        print(f"[focus][debug] 全マッチ(needle+意味)={len(matched)}件 期間={min(md)}〜{max(md)} "
              f"／按分採用={len(selected)}件 採用期間={min(sd)}〜{max(sd)} ({n_months}ヶ月分)")
    try:
        oldest = summaries_col.find_one(
            {"summary": {"$exists": True}},
            {"created_at": 1, "retro_date": 1},
            sort=[("created_at", 1)],
        )
        if oldest:
            od = oldest.get("retro_date") or str(oldest.get("created_at", ""))[:10]
            print(f"[focus][debug] summariesコレクション最古ドキュメント={od}（アーカイブの実深度）")
    except Exception:
        pass

    total = sum(len(b) for _, b in selected)
    print(f"[focus] 関連サマリ: {len(selected)}ブロック ({total}文字)")
    return "\n\n".join(b for _, b in selected)


# =============================================================================
# 既知情報の読み返し（Tier2・member専用）
# =============================================================================

# profile / simple_profile のフィールド表示名（両形に対応）
_PROFILE_LABELS = {
    "tone":                "口調",
    "communication_style": "コミュ特徴",
    "vocabulary":          "語彙・口癖",
    "personality":         "性格",
    "background":          "立場・背景",
    "relations":           "関係性",
    "interests_vibe":      "関心・雰囲気",
    "birthday":            "誕生日",
    "vibe":                "雰囲気",          # simple_profile
    "tone_tags":           "口調タグ",        # simple_profile（list）
}


def load_member_context(users_col) -> str:
    """対象メンバーについて過去に蓄積した profile/claims/memories を読み返す（積み上げ式）。

    embedding は巨大なのでプロンプトには載せない（content/category/date のみ）。
    profile は $set 上書きなので戻しても劣化しないが、claims/memories は append-store の
    ため、戻した内容が言い換えで再抽出されると near-dup が溜まる。dedup 側を正規化して防ぐ
    （save_memories_from_focus の _dedup_key を参照）。
    """
    doc = users_col.find_one({"_id": FOCUS_TARGET}) or {}
    if not doc:
        return ""

    prof  = doc.get("profile") or {}
    sprof = doc.get("simple_profile") or {}
    sections = []

    # プロフィール（booster profile 優先、無い項目は simple_profile で補う）
    prof_lines = []
    for k, label in _PROFILE_LABELS.items():
        v = prof.get(k) or sprof.get(k)
        if not v:
            continue
        if isinstance(v, list):
            v = "・".join(str(x) for x in v if x)
        if v:
            prof_lines.append(f"- {label}: {v}")
    for k, label in [("hobbies", "趣味"), ("memo", "メモ")]:
        v = prof.get(k)
        if v:
            joined = "・".join(str(x) for x in v if x)
            if joined:
                prof_lines.append(f"- {label}: {joined}")
    if prof_lines:
        sections.append("◆既存プロフィール\n" + "\n".join(prof_lines))

    # claims（append+cap20、末尾が新しい）→ 新しい順に最大15件
    claims = [c.get("content", "") for c in (doc.get("claims") or []) if c.get("content")]
    if claims:
        cl = "\n".join(f"- {c}" for c in reversed(claims[-15:]))
        sections.append("◆過去の主張(claims)\n" + cl)

    # memories（先頭が新しい MRU、embedding は載せない）→ 最大20件
    ml = []
    for m in (doc.get("memories") or [])[:20]:
        c = m.get("content", "")
        if not c:
            continue
        tag = "/".join(x for x in (m.get("category", ""), m.get("date", "")) if x)
        ml.append(f"- [{tag}] {c}" if tag else f"- {c}")
    if ml:
        sections.append("◆過去の記憶(memories)\n" + "\n".join(ml))

    return "\n\n".join(sections)


# =============================================================================
# Big Five 連携（member専用・analyze_personality.py の profile.bigfive を読むだけ）
# =============================================================================

def load_bigfive(users_col) -> dict | None:
    """対象メンバーの profile.bigfive を返す。性格診断オプトアウト者は None。

    bigfive は analyze_personality.py が TIPI-J で較正して書く唯一の所有者なので、
    focus は読むだけで書き戻さない（較正値を vibe で汚さないため）。
    personality_optout の人は分析自体を拒否しているので focus でも開示しない
    （古い bigfive が残っていても出さない）。
    """
    doc = users_col.find_one(
        {"_id": FOCUS_TARGET},
        {"profile.bigfive": 1, "personality_optout": 1},
    ) or {}
    if doc.get("personality_optout") is True:
        print("[focus] Big Five: 対象が personality_optout=True のため非表示")
        return None
    bf = (doc.get("profile") or {}).get("bigfive")
    if isinstance(bf, dict) and any(isinstance(bf.get(k), dict) for k in BIGFIVE_FACTORS_JA):
        return bf
    # 無音だと「バグか未生成か」が切り分けられないので理由を出す（analyze_personality が
    # profile.bigfive を書く唯一の所有者。focus が profile を作っても bigfive は埋まらない）。
    print("[focus] Big Five: profile.bigfive 未生成（analyze_personality がこのユーザー未分析）。"
          "daily_tasks.yml を実行すれば次回 /focus から表示される")
    return None


def render_bigfive_for_prompt(bigfive: dict) -> str:
    """LLMプロンプト用：較正済み参考基準としての Big Five テキスト。
    band/confidence/evidence のみ（score は内部較正用なので出さない）。"""
    lines = []
    for key, label in BIGFIVE_FACTORS_JA.items():
        f = bigfive.get(key)
        if not isinstance(f, dict) or not f.get("band"):
            continue
        conf = CONF_JA.get(f.get("confidence"), f.get("confidence") or "?")
        line = f"- {label}: {f['band']}（確信:{conf}）"
        ev = f.get("evidence")
        if ev and str(ev) not in ("null", "None"):
            line += f" ／根拠例: {str(ev)[:80]}"
        lines.append(line)
    if not lines:
        return ""
    dp = bigfive.get("data_points")
    foot = f"\n（{dp}発言からの推定）" if dp else ""
    return "\n".join(lines) + foot


def render_bigfive_for_embed(bigfive: dict) -> dict | None:
    """Discord埋め込み用フィールド：LLMの気分に左右されず較正値を確実に表示する。
    bands+confidence のみ＋確定診断ではない旨の注記。"""
    rows = []
    for key, label in BIGFIVE_FACTORS_JA.items():
        f = bigfive.get(key)
        if not isinstance(f, dict) or not f.get("band"):
            continue
        conf = CONF_JA.get(f.get("confidence"), "?")
        rows.append(f"**{label}** {f['band']}（確信:{conf}）")
    if not rows:
        return None
    val = "\n".join(rows) + "\n※検証済み尺度TIPI-Jによるチャットからの推定。確定診断ではありません。"
    return {"name": "🧬 性格傾向（Big Five / TIPI-J推定）", "value": val[:1020], "inline": False}


# =============================================================================
# AI処理
# =============================================================================

def _extract_retry_wait(err: str) -> float:
    m = re.search(r"retry in ([\d.]+)s", err)
    return float(m.group(1)) + 2.0 if m else 60.0


def call_ai(client_ai: genai.Client, prompt: str, max_tokens: int = 2000,
            temperature: float = 0.3) -> str | None:
    for model, label in [(MODEL, "main"), (MODEL_FALLBACK, "fallback")]:
        for attempt in range(5):
            try:
                resp = client_ai.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                        safety_settings=SAFETY_OFF,
                    ),
                )
                text = getattr(resp, "text", None)
                if text and text.strip():
                    return text.strip()
                # 空の理由を診断（finish_reason / safety / prompt_feedback）
                try:
                    cand = (getattr(resp, "candidates", None) or [None])[0]
                    fr   = getattr(cand, "finish_reason", None)
                    sr   = getattr(cand, "safety_ratings", None)
                    pf   = getattr(resp, "prompt_feedback", None)
                    print(f"[WARN] {label}: 空レスポンス attempt{attempt+1} finish={fr} pf={pf} safety={sr}")
                except Exception:
                    print(f"[WARN] {label}: 空レスポンス attempt{attempt+1}")
                time.sleep(5)
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    wait = _extract_retry_wait(err)
                    print(f"[WARN] 429 ({label}) attempt{attempt+1}, {wait:.1f}s待機...")
                    time.sleep(wait)
                elif any(s in err for s in ("503", "UNAVAILABLE", "500", "INTERNAL")):
                    # 500 INTERNAL / 503 UNAVAILABLE は一時障害が多い → backoff してリトライ
                    wait = (attempt + 1) * 20
                    print(f"[WARN] 500/503 ({label}) attempt{attempt+1}, {wait}s待機...")
                    time.sleep(wait)
                else:
                    print(f"[ERROR] {label}: {e}")
                    break
    return None


def generate_report(client_ai: genai.Client, log_text: str, summary_text: str = "",
                    member_context: str = "", bigfive_text: str = "") -> str | None:
    if FOCUS_TYPE == "member":
        prompt = MEMBER_FOCUS_PROMPT.format(name=FOCUS_NAME, days=FETCH_DAYS)
    else:
        prompt = KEYWORD_FOCUS_PROMPT.format(keyword=FOCUS_NAME, days=FETCH_DAYS)

    # 並び順: 較正基準 → 既知情報 → 直近生ログ → 長期アーカイブ(最後=高アテンション位置)。
    # 長期データを末尾に置き、時系列見出しで活用を強制する。
    parts = []
    # Big Five は「上書き対象の古い分析」ではなく検証済みの“測定値”なので、
    # member_context（=検証・更新せよ）とは別 part で較正基準として提示する。
    # （独立 part にすることで member_context の [:4000] トランケにも巻き込まれない）
    if bigfive_text:
        parts.append(
            "【性格傾向の較正済み参考基準（検証済み心理尺度 TIPI-J / Big Five によるチャット推定。"
            "安易に上書きせず、今回の観察をこれと整合させ、一致点・相違点があれば言及すること）】\n"
            f"{bigfive_text}"
        )
    if member_context:
        parts.append(
            f"【これまでに蓄積した既知情報（過去の分析結果。今回の証拠で検証・更新せよ）】\n"
            f"{member_context[:MAX_MEMBER_CONTEXT_CHARS]}"
        )
    if log_text:
        parts.append(
            f"【直近{FETCH_DAYS}日の生チャットログ（{FOCUS_NAME}関連のみ抽出・あくまで最近の断面）】\n"
            f"{log_text[:MAX_LOG_CHARS]}"
        )
    if summary_text:
        parts.append(
            f"【長期の要約アーカイブ（過去{_lookback_days()}日・各行頭に[日付]・{FOCUS_NAME}関連を抽出）\n"
            f"※「数ヶ月の変遷・時系列」セクションはこのアーカイブを主たる根拠にすること】\n"
            f"{summary_text[:MAX_SUMMARY_CHARS]}"
        )

    full_prompt = prompt + "\n\n" + "\n\n".join(parts)
    # 8セクション（時系列見出し追加）で出力が伸びたため上限を引き上げ、総合評価が途切れないように
    return call_ai(client_ai, full_prompt, max_tokens=5000)


def extract_profile(client_ai: genai.Client, report: str) -> dict | None:
    """人物フォーカスの場合のみプロフィールをJSON抽出"""
    prompt = PROFILE_UPDATE_PROMPT.format(name=FOCUS_NAME, report=report[:4000])
    raw    = call_ai(client_ai, prompt, max_tokens=2000)
    if not raw:
        return None
    try:
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        result  = json.loads(cleaned)
        return result if any(v for v in result.values() if v) else None
    except Exception as e:
        print(f"[WARN] profile extract failed: {e}")
        return None


# =============================================================================
# MongoDB保存
# =============================================================================

def save_profile(users_col, profile: dict):
    """人物フォーカスの場合、対象メンバーのプロフィールに反映"""
    updates = {f"profile.{k}": v for k, v in profile.items() if v}
    if not updates:
        return
    users_col.update_one(
        {"_id": FOCUS_TARGET},
        {"$set": updates},
        upsert=False,
    )
    print(f"[profile] {FOCUS_NAME}のプロフィールを更新: {list(updates.keys())}")


def _dedup_key(s: str) -> str:
    """near-dup 照合用の正規化キー。Tier2 で既知情報を戻すと言い換え再抽出が起きるため、
    大小文字・空白・末尾句読点を畳んで軽い言い換え重複を弾く（完全な意味dedupはTier3領域）。"""
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", "", s)
    return s.strip("。.!?！？、,，・")


def save_memories_from_focus(client_ai: genai.Client, users_col, report: str):
    """focusレポートからmemories・claimsを抽出してユーザーに保存"""
    prompt = f"""以下は「{FOCUS_NAME}」についての観察レポートです。
このレポートから、後で思い出す価値のある情報を抽出してください。
必ずJSON形式のみで返せ。前置き・コードブロック不要。

出力形式:
{{
  "claims": ["この人が主張・意見として述べたことを一文ずつ（最大5件）"],
  "memories": [
    {{"content": "記憶すべき出来事・状況・感情を一文で", "category": "趣味/出来事/感情/計画"}}
  ]
}}

【観察レポート】
{report[:4000]}"""

    # call_ai 経由で MODEL→MODEL_FALLBACK のリトライ/フォールバックに乗せる
    # （旧実装は models/gemma-3-27b-it 直書きで 404 NOT_FOUND になり常に失敗していた）
    raw = call_ai(client_ai, prompt, max_tokens=2000, temperature=0.1)
    if not raw:
        print("[WARN] focus memories extract: 空/失敗（スキップ）")
        return
    try:
        raw  = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
    except Exception as e:
        print(f"[WARN] focus memories extract parse: {e}")
        return

    doc = users_col.find_one({"_id": FOCUS_TARGET}) or {}
    now = datetime.now(timezone.utc)

    # claims更新（正規化キーで near-dup も弾く・intra-batch も seen で防ぐ）
    new_claims = []
    seen_claims = {_dedup_key(c.get("content","")) for c in doc.get("claims", [])}
    for c in data.get("claims", []):
        key = _dedup_key(c)
        if c and key and key not in seen_claims:
            seen_claims.add(key)
            new_claims.append({
                "content": c[:200],
                "date":    now.isoformat(),
                "source":  "focus",
            })
    all_claims = (doc.get("claims", []) + new_claims)[-20:]

    # memories更新（Embedding付与・正規化キーで near-dup を弾く）
    new_memories = []
    seen_mems = {_dedup_key(m.get("content","")) for m in doc.get("memories", [])}
    for m in data.get("memories", []):
        text = m.get("content","")
        key  = _dedup_key(text)
        if not text or not key or key in seen_mems:
            continue
        seen_mems.add(key)
        vec = []
        try:
            er = client_ai.models.embed_content(
                model="models/gemini-embedding-001",
                contents=text[:500],
            )
            if hasattr(er, "embeddings") and er.embeddings:
                vec = list(er.embeddings[0].values)
        except Exception:
            pass
        new_memories.append({
            "content":   text[:300],
            "date":      now.strftime("%Y-%m"),
            "category":  m.get("category",""),
            "embedding": vec,
            "source":    "focus",
        })
        time.sleep(0.5)

    # 新しい記憶を先頭に置き、新しい順に40件保持（main.py $position:0 と向きを統一）
    all_memories = (new_memories + doc.get("memories", []))[:40]

    if new_claims or new_memories:
        users_col.update_one(
            {"_id": FOCUS_TARGET},
            {"$set": {"claims": all_claims, "memories": all_memories}},
            upsert=False,
        )
        print(f"[focus] {FOCUS_NAME}: claims+{len(new_claims)}件, memories+{len(new_memories)}件")


# =============================================================================
# Discord投稿
# =============================================================================

def _split_for_field(text: str, limit: int = 1020) -> list[str]:
    """Discordの1フィールド=1024字制限に収まるよう、本文を行境界で複数チャンクに分割。
    切り捨てず全文を表示するため。1行自体が limit 超なら強制分割。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                      # 1行が長すぎる場合は強制分割
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


def post_report(report: str, bigfive: dict | None = None):
    now_jst    = datetime.now(JST)
    icon       = "👤" if FOCUS_TYPE == "member" else "🔍"
    title_str  = f"{icon} {FOCUS_NAME} のフォーカス要約"
    ICONS      = SECTION_ICONS_MEMBER if FOCUS_TYPE == "member" else SECTION_ICONS_KEYWORD

    parts    = re.split(r"\n##\s+", "\n" + report)
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
    # Big Five 較正値を冒頭フィールドに（LLM出力に依存せず確実に表示）
    if bigfive:
        bf_field = render_bigfive_for_embed(bigfive)
        if bf_field:
            fields.append(bf_field)
    for title, body in sections:
        icon_c = next((v for k, v in ICONS.items() if k in title), "📋")
        # 1024字超のセクションは切り捨てず、続きフィールドに分割して全文表示
        chunks = _split_for_field(body, 1020)
        for i, chunk in enumerate(chunks):
            name = f"{icon_c} {title}" if i == 0 else f"{icon_c} {title}（続き{i+1}）"
            fields.append({"name": name[:256], "value": chunk, "inline": False})

    embeds, current_fields, current_chars = [], [], 0
    for field in fields:
        fc = len(field["name"]) + len(field["value"])
        if (current_chars + fc > 5800 or len(current_fields) >= 25) and current_fields:
            embed = {"color": 0x9B59B6, "fields": current_fields,
                     "title": title_str if not embeds else f"{icon} {FOCUS_NAME} のフォーカス要約（続き）"}
            embeds.append(embed)
            current_fields, current_chars = [], 0
        current_fields.append(field)
        current_chars += fc

    if current_fields:
        embeds.append({
            "color":  0x9B59B6,
            "title":  title_str if not embeds else f"{icon} {FOCUS_NAME} のフォーカス要約（続き）",
            "fields": current_fields,
            "footer": {"text": f"空気くんフォーカス要約 • {now_jst.strftime('%H:%M')} JST"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    url     = f"https://discord.com/api/v10/channels/{FOCUS_CHANNEL_ID}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
    for i in range(0, len(embeds), 10):
        resp = requests.post(url, headers=headers,
                             json={"embeds": embeds[i:i+10]}, timeout=10)
        if resp.status_code in (200, 201):
            print(f"[post] 投稿成功")
        else:
            print(f"[ERROR] 投稿失敗: {resp.status_code} {resp.text}")


# =============================================================================
# メイン
# =============================================================================

def main():
    print(f"[focus] type={FOCUS_TYPE}, target={FOCUS_TARGET}, name={FOCUS_NAME}")

    # MongoDB は両モードで使う（要約アーカイブ・nickname_map）ので先に接続
    mongo         = MongoClient(MONGODB_URI)
    db            = mongo[DB_NAME]
    users_col     = db["users"]
    summaries_col = db["summaries"]
    system_col    = db["system"]

    # フォーカス対象の検索語（member はエイリアスまで展開）
    if FOCUS_TYPE == "member":
        needles = build_member_needles(system_col, users_col)
        print(f"[focus] 名前バリアント: {needles}")
    else:
        needles = list({FOCUS_TARGET, FOCUS_NAME})

    # 生ログ取得（直近 FETCH_DAYS 日）
    print(f"[focus] 直近{FETCH_DAYS}日分の生ログを取得中...")
    msgs     = fetch_logs(FETCH_DAYS)
    log_text = filter_logs(msgs, needles) if msgs else ""
    print(f"[focus] 対象生ログ: {len(log_text)}文字")

    # Gemini クライアントは要約検索(keyword の意味検索 embedding)でも使うので先に作る
    client_ai = genai.Client(api_key=GEMINI_API_KEY)

    # keyword の Tier3: 検索語を embedding（RETRIEVAL_QUERY）。失敗時は None＝needle のみに自然降格
    query_embedding = None
    if FOCUS_TYPE == "keyword":
        try:
            query_embedding = embed_query(client_ai, FOCUS_TARGET)
            print(f"[focus] keyword 意味検索ON: query embedding dim="
                  f"{len(query_embedding) if query_embedding else 0}")
        except Exception as e:
            print(f"[focus] query embedding 失敗（needleのみで続行）: {e}")

    # 長期アーカイブ（summaries）から関連行を抽出（keyword は needle+意味検索のハイブリッド）
    print(f"[focus] 過去{_lookback_days()}日の要約アーカイブを検索中...")
    summary_text = fetch_relevant_summaries(summaries_col, needles, query_embedding)

    if not log_text and not summary_text:
        print(f"[focus] 「{FOCUS_NAME}」に関する情報が見つかりませんでした。")
        return

    # 既知情報の読み返し（Tier2・member専用：過去のprofile/claims/memoriesを積み上げ）
    member_context = load_member_context(users_col) if FOCUS_TYPE == "member" else ""
    if member_context:
        print(f"[focus] 既知情報を読み込み: {len(member_context)}文字")

    # Big Five 連携（member専用：analyze_personality の TIPI-J 推定を較正基準として読む）
    bigfive      = load_bigfive(users_col) if FOCUS_TYPE == "member" else None
    bigfive_text = render_bigfive_for_prompt(bigfive) if bigfive else ""
    if bigfive_text:
        present = [BIGFIVE_FACTORS_JA[k] for k in BIGFIVE_FACTORS_JA
                   if isinstance(bigfive.get(k), dict)]
        print(f"[focus] Big Five 連携: {present}")

    # レポート生成（較正基準 + 既知情報 + 長期要約 + 直近生ログ）
    print("[focus] レポート生成中...")
    report = generate_report(client_ai, log_text, summary_text, member_context, bigfive_text)
    if not report:
        print("[focus] レポート生成失敗")
        return
    print(f"[focus] レポート生成完了 ({len(report)}文字)")

    # 人物フォーカスの場合はプロフィール・memories補完
    if FOCUS_TYPE == "member":
        print("[focus] プロフィール・記憶抽出中...")
        time.sleep(3)
        profile = extract_profile(client_ai, report)
        if profile:
            save_profile(users_col, profile)
        else:
            print("[focus] プロフィール抽出失敗（スキップ）")
        time.sleep(3)
        save_memories_from_focus(client_ai, users_col, report)

    # Discord投稿（member は Big Five 較正値を冒頭フィールドに付与）
    post_report(report, bigfive=bigfive)
    print("[focus] 完了！")


if __name__ == "__main__":
    main()
