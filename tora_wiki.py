"""Tora MOD Wiki (https://ranranrufan-oss.github.io/Tora-MOD-wiki/) の窓口。

Wiki から来る役職の提案・バグ報告を Discord に投稿し、役職一覧・お知らせ・受付停止・
バグ一覧を保存して Wiki に返す。元は別に動いていた Node 版の tora bot (index.js) を
そのまま移したもの。API の形・応答・Discord の投稿の見た目は元と同じにしてある。

元との違い:
- 保存先は JSON ファイルでなく MongoDB (Render の無料枠はディスクが再起動で消える)。
  tora_wiki コレクションに _id="roles" / "settings"、tora_bugs コレクションにバグ 1 件 1 doc。
- ボタンの custom_id は他の機能とぶつからないよう "tora:" を前に付けた。
- POST /api/admin/import (管理者だけ) で、旧 bot の公開 API から役職・お知らせ・バグ一覧を写せる。

main.py からは setup() / register_routes() / persistent_views() の 3 つだけを呼ぶ。

環境変数 (チャンネルは 0 のままだと送信が 503 bot_offline になる):
  TORA_CHANNEL_INBOX_ID   役職提案
  TORA_CHANNEL_REVIEW_ID  役職検討
  TORA_CHANNEL_ADOPT_ID   役職採用
  TORA_CHANNEL_BUG_ID     バグ報告
  TORA_ADMIN_KEY          Wiki の管理者キー。空なら管理用の API は 503 admin_disabled
  TORA_STAFF_ROLE_IDS     ボタンを押せるロール (カンマ区切り)。空なら「メッセージの管理」権限の人だけ
  TORA_ALLOWED_ORIGINS    API を呼べるサイト (カンマ区切り)
  TORA_IMPORT_FROM        /api/admin/import の写し元 (旧 bot の URL)
"""
import asyncio
import datetime
import hashlib
import hmac
import io
import json
import os
import re
import time
import uuid

import discord

# ===== 設定 =====
CHANNEL_INBOX_ID  = int(os.environ.get("TORA_CHANNEL_INBOX_ID") or 0)
CHANNEL_REVIEW_ID = int(os.environ.get("TORA_CHANNEL_REVIEW_ID") or 0)
CHANNEL_ADOPT_ID  = int(os.environ.get("TORA_CHANNEL_ADOPT_ID") or 0)
CHANNEL_BUG_ID    = int(os.environ.get("TORA_CHANNEL_BUG_ID") or 0)
ADMIN_KEY         = os.environ.get("TORA_ADMIN_KEY") or ""
STAFF_ROLE_IDS    = {int(s) for s in (os.environ.get("TORA_STAFF_ROLE_IDS") or "").split(",") if s.strip().isdigit()}
ALLOWED_ORIGINS   = [s.strip().rstrip("/") for s in
                     (os.environ.get("TORA_ALLOWED_ORIGINS") or "https://ranranrufan-oss.github.io").split(",")
                     if s.strip()]
IMPORT_FROM       = (os.environ.get("TORA_IMPORT_FROM") or "https://413n7r1lrp.apps.bot-hosting.cloud/").rstrip("/") + "/"

MAX_JSON_BODY = 100_000      # 普通の API
MAX_ROLES_BODY = 2_000_000   # 役職一覧の保存
MAX_BUG_BODY = 9_000_000     # バグ報告 (ログ込み)
MAX_LOG_BYTES = 7_000_000    # 添付するログの上限 (超えたら末尾を残す)

if not ADMIN_KEY:
    print("[tora] TORA_ADMIN_KEY が未設定 (Wiki の管理者機能は使えません)")

_client: discord.Client | None = None
_meta_col = None     # tora_wiki: _id="roles" {value: [...] | None} / _id="settings" {announcement, paused}
_bugs_col = None     # tora_bugs: {_id, message_id, desc, date, created_at}
_trim = None         # 大きなログを扱った後にヒープを OS へ返す関数 (main.py の _malloc_trim)
_home_guild_id = 0

_roles = None
_settings = {"announcement": "", "paused": False}
_loaded = False
_load_lock = asyncio.Lock()
_bugs_lock = asyncio.Lock()


def setup(client: discord.Client, db, home_guild_id: int = 0, trim=None) -> None:
    global _client, _meta_col, _bugs_col, _trim, _home_guild_id
    _client = client
    _meta_col = db["tora_wiki"]
    _bugs_col = db["tora_bugs"]
    _trim = trim
    _home_guild_id = home_guild_id


async def _ensure_loaded() -> None:
    global _loaded, _roles, _settings
    if _loaded:
        return
    async with _load_lock:
        if _loaded:
            return
        r = await _meta_col.find_one({"_id": "roles"})
        s = await _meta_col.find_one({"_id": "settings"})
        v = r.get("value") if r else None
        _roles = v if isinstance(v, list) else None
        if s:
            _settings = {"announcement": str(s.get("announcement") or ""), "paused": s.get("paused") is True}
        _loaded = True


def _trim_heap() -> None:
    if _trim:
        try:
            _trim()
        except Exception:
            pass


def _today_ja() -> str:
    d = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return f"{d.year}/{d.month}/{d.day}"


# ===== 文字列の整形 =====
def _s(v, max_len: int = 0) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        v = "true" if v else "false"
    s = re.sub(r"\r\n?", "\n", str(v)).strip()
    return s[:max_len] if max_len and len(s) > max_len else s


def _clip(s: str, max_len: int, mark: str = "…") -> str:
    if len(s) <= max_len:
        return s
    return s[:max(0, max_len - len(mark))] + mark


# ===== 管理者の確認 =====
def _is_admin(request) -> bool:
    if not ADMIN_KEY:
        return False
    given = request.headers.get("x-admin-key") or ""
    if not given:
        return False
    a = hashlib.sha256(given.encode("utf-8")).digest()
    b = hashlib.sha256(ADMIN_KEY.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)


# ===== 送信の回数制限 (IP ごと・10 分に 5 件) =====
_RATE_WINDOW = 10 * 60
_RATE_MAX = 5
_RATE_MAX_KEYS = 2048   # X-Forwarded-For は偽装できるので、追跡する IP の数に上限を設ける
_rate_log: dict = {}


def _client_ip(request) -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote or "unknown"


def _rate_limited(request) -> bool:
    ip = _client_ip(request)
    now = time.time()
    hits = [t for t in _rate_log.get(ip, ()) if now - t < _RATE_WINDOW]
    if len(hits) >= _RATE_MAX:
        _rate_log[ip] = hits
        return True
    if ip not in _rate_log and len(_rate_log) >= _RATE_MAX_KEYS:
        for k in list(_rate_log):
            if not any(now - t < _RATE_WINDOW for t in _rate_log[k]):
                del _rate_log[k]
        if len(_rate_log) >= _RATE_MAX_KEYS:
            oldest = min(_rate_log, key=lambda k: _rate_log[k][-1] if _rate_log[k] else 0.0)
            _rate_log.pop(oldest, None)
    hits.append(now)
    _rate_log[ip] = hits
    return False


# ===== 役職データの検証 =====
CAMPS = ["インポスター", "マッドメイト", "クルーメイト", "第三陣営", "コンビネーション", "属性", "幽霊役職"]
MODES = ["両方", "ホストMOD", "クライアントMOD"]
SETTING_TYPES = ["number", "boolean", "text"]


def _clean_setting(s):
    if not isinstance(s, dict):
        return None
    name = _s(s.get("name"), 100)
    if not name:
        return None
    return {
        "name": name,
        "type": s.get("type") if s.get("type") in SETTING_TYPES else "text",
        "min": _s(s.get("min"), 30),
        "max": _s(s.get("max"), 30),
        "step": _s(s.get("step"), 30),
        "unit": _s(s.get("unit"), 30),
        "c1": _s(s.get("c1"), 50),
        "c2": _s(s.get("c2"), 50),
        "text": _s(s.get("text"), 300),
        "detail": _s(s.get("detail"), 400),
    }


def _clean_role(r):
    if not isinstance(r, dict):
        return None
    name = _s(r.get("name"), 100)
    if not name:
        return None
    lines = r.get("descLines") if isinstance(r.get("descLines"), list) else []
    sets = r.get("settingsList") if isinstance(r.get("settingsList"), list) else []
    return {
        "id": _s(r.get("id"), 60) or str(uuid.uuid4()),
        "name": name,
        "camp": r.get("camp") if r.get("camp") in CAMPS else "クルーメイト",
        "mode": r.get("mode") if r.get("mode") in MODES else "両方",
        "descLines": [x for x in (_s(l, 500) for l in lines) if x][:50],
        "settingsList": [x for x in (_clean_setting(t) for t in sets) if x][:50],
    }


# ===== 提案・バグの embed を組む =====
CAMP_COLORS = {
    "インポスター": 16731469, "マッドメイト": 16731469, "クルーメイト": 3718648,
    "第三陣営": 10265775, "コンビネーション": 16777215, "幽霊役職": 16777215, "属性": 2278750,
}
BUG_COLOR = 15631988
FOOTER = "Tora MOD Official Wiki"


def _setting_line(s) -> str:
    if s["type"] == "boolean":
        return f"・**{s['name']}**: 2択 [ {s['c1'] or 'ON'} / {s['c2'] or 'OFF'} ]"
    if s["type"] == "text":
        return f"・**{s['name']}**: {s['text'] or '-'}"
    d = []
    u = s["unit"]
    if s["min"] and s["max"]:
        d.append(f"{s['min']}〜{s['max']}{u}")
    elif s["max"]:
        d.append(f"最大: {s['max']}{u}")
    elif s["min"]:
        d.append(f"最小: {s['min']}{u}")
    if s["step"]:
        d.append(f"間隔: {s['step']}{u}")
    if not s["min"] and not s["max"] and not s["step"] and u:
        d.append(f"単位: {u}")
    return f"・**{s['name']}**: 数値" + (f" ({', '.join(d)})" if d else "")


def _fit_embed(e: dict) -> bool:
    """embed を Discord の上限 (title 256 / description 4096 / field 名 256・値 1024 / 25 個 / 合計 6000) に収める。
    切った物があれば True。"""
    cut = False

    def c(s, m):
        nonlocal cut
        out = _clip(s, m)
        if out != s:
            cut = True
        return out

    e["title"] = c(e.get("title") or "(無題)", 256)
    e["description"] = c(e["description"], 4096) if e.get("description") else ""
    e["fields"] = [{"name": c(f.get("name") or "-", 256), "value": c(f.get("value") or "-", 1024),
                    "inline": bool(f.get("inline"))} for f in (e.get("fields") or [])[:25]]

    def total():
        return (len(e["title"]) + len(e["description"]) + len(e.get("footer") or "") +
                sum(len(f["name"]) + len(f["value"]) for f in e["fields"]))

    # 本文 (description) を優先して残し、まず後ろの欄から削る
    over = total() - 5900
    for f in reversed(e["fields"]):
        if over <= 0:
            break
        keep = max(20, len(f["value"]) - over)
        if keep < len(f["value"]):
            cut = True
            f["value"] = _clip(f["value"], keep)
            over = total() - 5900
    if over > 0 and e["description"]:
        cut = True
        e["description"] = _clip(e["description"], max(1, len(e["description"]) - over))
    return cut


def _to_embed(e: dict, color: int) -> discord.Embed:
    b = discord.Embed(title=e["title"], color=color, timestamp=discord.utils.utcnow())
    if e["description"]:
        b.description = e["description"]
    for f in e["fields"]:
        b.add_field(name=f["name"], value=f["value"], inline=f["inline"])
    if e.get("footer"):
        b.set_footer(text=e["footer"])
    return b


def _full_text(e: dict) -> str:
    return "\n".join([e.get("title") or "", "", e.get("description") or "", "",
                      *[f"## {f['name']}\n{f['value']}" for f in e.get("fields") or []]])


def _build_suggestion(d: dict, admin: bool) -> dict:
    typ = "port" if d.get("type") == "port" else "role"
    camp = d.get("camp") if d.get("camp") in CAMPS else "クルーメイト"
    mode = d.get("mode") if d.get("mode") in MODES else "両方"
    desc = _s(d.get("desc"), 20000)
    fields = [
        {"name": "種別", "value": "🔄 移植案" if typ == "port" else "💡 役職提案", "inline": True},
        {"name": "対応モード", "value": mode, "inline": True},
        {"name": "陣営", "value": camp, "inline": True},
    ]
    missing = []

    def need(v, label):
        if not v:
            missing.append(label)
        return v

    if typ == "port":
        fields.append({"name": "移植元", "value": need(_s(d.get("origin"), 200), "移植元") or "未記入"})

    head = "【移植案】" if typ == "port" else "【新役職提案】"
    if camp == "コンビネーション":
        combi = d.get("combi") if isinstance(d.get("combi"), list) else []
        rs = []
        for i in (1, 2):
            x = combi[i - 1] if len(combi) >= i and isinstance(combi[i - 1], dict) else {}
            c = x.get("camp") if x.get("camp") in ("インポスター", "マッドメイト", "クルーメイト", "第三陣営") else "クルーメイト"
            rs.append({
                "camp": c,
                "name": need(_s(x.get("name"), 100), f"役職{i}の名前"),
                "nameEn": need(_s(x.get("nameEn"), 100), f"役職{i}の英名"),
                "win": need(_s(x.get("win"), 2000), f"役職{i}の勝利条件") if c == "第三陣営" else "",
            })
        title = f"{head}{rs[0]['name'] or '役職1'} × {rs[1]['name'] or '役職2'}"
        for i, x in enumerate(rs, 1):
            fields.append({"name": f"役職{i} [{x['camp']}]",
                           "value": f"{x['name'] or '未記入'}" + (f" ({x['nameEn']})" if x["nameEn"] else ""),
                           "inline": True})
            if x["camp"] == "第三陣営":
                fields.append({"name": f"役職{i}の勝利条件", "value": x["win"] or "未記入"})
    else:
        name = need(_s(d.get("name"), 100), "役職名")
        name_en = need(_s(d.get("nameEn"), 100), "英名")
        title = f"{head}{name or '(名前なし)'}" + (f" ({name_en})" if name_en else "")
        fields.append({"name": "英語名 (英名)", "value": name_en or "未記入", "inline": True})
        if camp == "第三陣営":
            fields.append({"name": "勝利条件", "value": need(_s(d.get("win"), 2000), "勝利条件") or "未記入"})
    need(desc, "能力・説明")

    sets = d.get("settings") if isinstance(d.get("settings"), list) else []
    settings_list = [x for x in (_clean_setting(t) for t in sets) if x][:40]
    if settings_list:
        fields.append({"name": "⚙️ ゲーム内設定項目", "value": "\n".join(_setting_line(t) for t in settings_list)})

    return {
        "missing": [] if admin else missing,
        "color": CAMP_COLORS.get(camp, 3718648),
        "embed": {
            "title": title,
            "description": f"**能力・詳細説明**\n{desc}" if desc else "**能力・詳細説明**\n（未入力 - 管理者送信）",
            "fields": fields,
            "footer": FOOTER + (" (管理者送信)" if admin else ""),
        },
    }


def _build_bug(d: dict, admin: bool) -> dict:
    match = _s(d.get("match"), 100)
    turn = _s(d.get("turn"), 100)
    desc = _s(d.get("desc"), 20000)
    log_name = re.sub(r'[\\/:*?"<>|\n]', "_", _s(d.get("logName"), 200)) or "log.txt"
    log = d.get("log") if isinstance(d.get("log"), str) else ""
    log_bytes = log.encode("utf-8")
    trimmed = False
    if len(log_bytes) > MAX_LOG_BYTES:
        # エラーは末尾に出るので後ろを残す
        log = log[-(MAX_LOG_BYTES // 3):]
        log_bytes = log.encode("utf-8")
        trimmed = True

    missing = []
    if not admin:
        if not match:
            missing.append("何試合目か")
        if not turn:
            missing.append("何ターン目か")
        if not desc:
            missing.append("バグ内容")
        if not log:
            missing.append("ログファイル")

    fields = [
        {"name": "何試合目か", "value": f"{match or '未記入'} 試合目", "inline": True},
        {"name": "何ターン目か", "value": f"{turn or '未記入'} ターン目", "inline": True},
        {"name": "添付ファイル名",
         "value": (f"{log_name} ({round(len(log_bytes) / 1024)} KB)" + (" ※末尾のみ" if trimmed else "")) if log else "未添付"},
    ]
    if log:
        tail = log.rstrip()[-900:].replace("```", "ˋˋˋ")
        fields.append({"name": "📋 ログ末尾（抜粋）", "value": "```text\n" + tail + "\n```"})

    return {
        "missing": missing,
        "log_bytes": log_bytes if log else b"",
        "log_name": log_name,
        "embed": {
            "title": "🐛【バグ報告】不具合が報告されました",
            "description": f"**バグ内容・発生状況**\n{desc or '未記入'}",
            "fields": fields,
            "footer": FOOTER + (" (管理者送信)" if admin else ""),
        },
    }


async def _fetch_text_channel(cid: int):
    if not cid or _client is None or not _client.is_ready():
        return None
    try:
        ch = _client.get_channel(cid) or await _client.fetch_channel(cid)
    except Exception as e:
        print(f"[tora] チャンネル {cid} を取得できません: {e}")
        return None
    return ch if hasattr(ch, "send") else None


_NO_MENTIONS = discord.AllowedMentions.none()


def _text_file(text: str, name: str) -> discord.File:
    return discord.File(io.BytesIO(text.encode("utf-8")), filename=name)


# ===== Web サーバー =====
class _HttpError(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


async def _read_json(request, limit: int):
    if request.content_length is not None and request.content_length > limit:
        raise _HttpError(413)
    buf = bytearray()
    async for chunk in request.content.iter_any():
        buf += chunk
        if len(buf) > limit:
            raise _HttpError(413)
    try:
        return json.loads(buf.decode("utf-8"))
    except Exception:
        raise _HttpError(400)


def _cors(request, resp):
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin and origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = request.headers["Origin"]
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, x-admin-key"
    return resp


async def _dispatch(request):
    from aiohttp import web
    if request.method == "OPTIONS":
        return _cors(request, web.Response(status=204))
    try:
        resp = await _handle(request)
    except _HttpError as e:
        resp = _json({"error": "too_large" if e.status == 413 else "bad_request"}, status=e.status)
    except Exception as e:
        print(f"[tora] API エラー: {type(e).__name__}: {e}")
        resp = _json({"error": "server_error"}, status=500)
    return _cors(request, resp)


def _json(obj, status: int = 200):
    from aiohttp import web
    # 日本語をエスケープしない (役職一覧が約 6 倍に膨らむ)
    return web.json_response(obj, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def _handle(request):
    J = _json
    m = request.method
    p = request.path.rstrip("/") or "/"
    admin = _is_admin(request)

    def deny():
        if not ADMIN_KEY:
            return J({"error": "admin_disabled"}, status=503)
        if not admin:
            return J({"error": "unauthorized"}, status=401)
        return None

    if p == "/api/status":
        return J({"status": "online", "discord": bool(_client and _client.is_ready())})

    await _ensure_loaded()
    global _roles, _settings

    # 管理者キーの確認
    if m == "POST" and p == "/api/admin/verify":
        return deny() or J({"ok": True})

    # 役職一覧
    if m == "GET" and p == "/api/roles":
        return J(_roles)
    if m == "PUT" and p == "/api/roles":
        if (r := deny()):
            return r
        data = await _read_json(request, MAX_ROLES_BODY)
        if not isinstance(data, list) or len(data) > 1000:
            return J({"error": "bad_roles"}, status=400)
        cleaned = [x for x in (_clean_role(r) for r in data) if x]
        seen = set()
        for r in cleaned:
            if r["id"] in seen:
                r["id"] = str(uuid.uuid4())
            seen.add(r["id"])
        await _meta_col.update_one({"_id": "roles"}, {"$set": {"value": cleaned}}, upsert=True)
        _roles = cleaned
        return J({"ok": True, "roles": _roles})

    # お知らせと受付停止
    if m == "GET" and p == "/api/settings":
        return J(_settings)
    if m == "PUT" and p == "/api/settings":
        if (r := deny()):
            return r
        data = await _read_json(request, MAX_JSON_BODY)
        if not isinstance(data, dict):
            return J({"error": "bad_settings"}, status=400)
        nxt = dict(_settings)
        if "announcement" in data:
            nxt["announcement"] = _s(data["announcement"], 500)
        if "paused" in data:
            nxt["paused"] = data["paused"] is True
        await _meta_col.update_one({"_id": "settings"}, {"$set": nxt}, upsert=True)
        _settings = nxt
        return J(_settings)

    # 役職の提案
    if m == "POST" and p == "/api/suggest":
        if _settings["paused"] and not admin:
            return J({"error": "paused"}, status=403)
        data = await _read_json(request, MAX_JSON_BODY)
        built = _build_suggestion(data if isinstance(data, dict) else {}, admin)
        if built["missing"]:
            return J({"error": "missing", "fields": built["missing"]}, status=400)
        ch = await _fetch_text_channel(CHANNEL_INBOX_ID)
        if not ch:
            return J({"error": "bot_offline"}, status=503)
        if not admin and _rate_limited(request):
            return J({"error": "rate_limited"}, status=429)
        full = _full_text(built["embed"])
        cut = _fit_embed(built["embed"])
        files = [_text_file(full, "提案の全文.txt")] if cut else []
        await ch.send(embed=_to_embed(built["embed"], built["color"]), view=SuggestionView(),
                      files=files, allowed_mentions=_NO_MENTIONS)
        return J({"ok": True})

    # バグ報告
    if m == "POST" and p == "/api/bug":
        if _settings["paused"] and not admin:
            return J({"error": "paused"}, status=403)
        try:
            data = await _read_json(request, MAX_BUG_BODY)
            built = _build_bug(data if isinstance(data, dict) else {}, admin)
            data = None
            if built["missing"]:
                return J({"error": "missing", "fields": built["missing"]}, status=400)
            ch = await _fetch_text_channel(CHANNEL_BUG_ID)
            if not ch:
                return J({"error": "bot_offline"}, status=503)
            if not admin and _rate_limited(request):
                return J({"error": "rate_limited"}, status=429)
            full = _full_text(built["embed"])
            cut = _fit_embed(built["embed"])
            files = []
            if built["log_bytes"]:
                files.append(discord.File(io.BytesIO(built["log_bytes"]), filename=built["log_name"]))
            if cut:
                files.append(_text_file(full, "報告の全文.txt"))
            await ch.send(embed=_to_embed(built["embed"], BUG_COLOR), view=BugView(),
                          files=files, allowed_mentions=_NO_MENTIONS)
            return J({"ok": True})
        finally:
            data = built = files = None
            _trim_heap()

    # バグ一覧
    if m == "GET" and p == "/api/bugs":
        out = [{"id": b["_id"], "desc": b.get("desc", ""), "date": b.get("date", "")}
               async for b in _bugs_col.find({}, {"desc": 1, "date": 1}).sort("created_at", 1)]
        return J(out)

    # 管理者によるバグの直接追加
    if m == "POST" and p == "/api/bugs":
        if (r := deny()):
            return r
        data = await _read_json(request, MAX_JSON_BODY)
        desc = _s(data.get("desc") if isinstance(data, dict) else None, 4000)
        if not desc:
            return J({"error": "missing", "fields": ["バグ内容"]}, status=400)
        await _bugs_col.insert_one(_bug_doc(None, desc))
        return J({"success": True})

    # 管理者によるバグの直接削除 (Discord 側の報告も消す)
    if m == "DELETE" and p.startswith("/api/bugs/"):
        if (r := deny()):
            return r
        bug_id = p[len("/api/bugs/"):]
        target = await _bugs_col.find_one_and_delete({"_id": bug_id})
        if not target:
            return J({"error": "not_found"}, status=404)
        mid = str(target.get("message_id") or "")
        if mid.isdigit():
            ch = await _fetch_text_channel(CHANNEL_BUG_ID)
            if ch:
                asyncio.create_task(_delete_quietly(ch.get_partial_message(int(mid))))
        return J({"success": True})

    # 旧 bot から役職・お知らせ・バグ一覧を写す (切り替えの時に 1 回)
    if m == "POST" and p == "/api/admin/import":
        if (r := deny()):
            return r
        return J(await _import_from_old())

    return J({"error": "not_found"}, status=404)


def _bug_doc(message_id, desc: str) -> dict:
    return {"_id": str(uuid.uuid4()), "message_id": message_id, "desc": desc, "date": _today_ja(),
            "created_at": datetime.datetime.now(datetime.timezone.utc)}


async def _delete_quietly(msg) -> None:
    try:
        await msg.delete()
    except Exception:
        pass


async def _import_from_old() -> dict:
    """旧 bot の公開 API (GET) から写して、こちらの保存を置き換える。
    旧 bot の Discord の投稿はこちらからは消せないので、写したバグは message_id 無しになる。"""
    import aiohttp
    global _roles, _settings
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async def get(path):
            async with s.get(IMPORT_FROM + path) as r:
                if r.status != 200:
                    raise RuntimeError(f"{path}: HTTP {r.status}")
                return await r.json(content_type=None)
        roles, settings, bugs = await get("api/roles"), await get("api/settings"), await get("api/bugs")

    cleaned = [x for x in (_clean_role(r) for r in roles) if x] if isinstance(roles, list) else None
    sett = {"announcement": _s((settings or {}).get("announcement"), 500),
            "paused": (settings or {}).get("paused") is True} if isinstance(settings, dict) else dict(_settings)
    docs = []
    base = datetime.datetime.now(datetime.timezone.utc)
    for i, b in enumerate(bugs if isinstance(bugs, list) else []):
        if not isinstance(b, dict) or not _s(b.get("desc")):
            continue
        docs.append({"_id": _s(b.get("id"), 60) or str(uuid.uuid4()), "message_id": None,
                     "desc": _s(b.get("desc"), 4000), "date": _s(b.get("date"), 30) or _today_ja(),
                     "created_at": base + datetime.timedelta(milliseconds=i)})

    await _meta_col.update_one({"_id": "roles"}, {"$set": {"value": cleaned}}, upsert=True)
    await _meta_col.update_one({"_id": "settings"}, {"$set": sett}, upsert=True)
    async with _bugs_lock:
        await _bugs_col.delete_many({})
        if docs:
            await _bugs_col.insert_many(docs)
    _roles, _settings = cleaned, sett
    print(f"[tora] 旧 bot から写した: 役職 {len(cleaned or [])} / バグ {len(docs)}")
    return {"ok": True, "roles": len(cleaned or []), "bugs": len(docs), "settings": sett}


def register_routes(app) -> None:
    app.router.add_route("*", "/api", _dispatch)
    app.router.add_route("*", "/api/{tail:.*}", _dispatch)


# ===== Discord のボタン =====
def _can_manage(interaction: discord.Interaction) -> bool:
    if STAFF_ROLE_IDS:
        roles = getattr(interaction.user, "roles", None) or []
        if any(r.id in STAFF_ROLE_IDS for r in roles):
            return True
    return bool(interaction.permissions and interaction.permissions.manage_messages)


def _bug_desc_from_embed(embed) -> str:
    """古い (webhook で投稿された) 報告にも対応して、バグ内容を取り出す。"""
    if not embed:
        return ""
    if embed.description:
        return re.sub(r"^\*\*バグ内容・発生状況\*\*\n", "", embed.description).strip()
    fields = embed.fields or []
    f = next((x for x in fields if x.name == "バグ内容・発生状況"), None) or \
        next((x for x in fields if "内容" in x.name and "ログ" not in x.name), None)
    return f.value if f else ""


_busy: set = set()


async def _reply(interaction: discord.Interaction, content: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except Exception as e:
        print(f"[tora] 返信に失敗: {e}")


async def _on_button(interaction: discord.Interaction, action: str) -> None:
    message = interaction.message
    if message is None:
        return
    if _home_guild_id and interaction.guild_id != _home_guild_id:
        await _reply(interaction, "⛔ このサーバーでは使えません。")
        return
    if not _can_manage(interaction):
        await _reply(interaction, "⛔ このボタンは運営だけが使えます。")
        return
    if message.id in _busy:
        await _reply(interaction, "⏳ ほかの人が処理中です。")
        return
    _busy.add(message.id)
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = message.embeds[0] if message.embeds else None

        if action == "delete":
            await message.delete()
            await _reply(interaction, "🗑️ 提案を削除しました。")
            return

        if action in ("review", "adopt"):
            to_review = action == "review"
            ch = await _fetch_text_channel(CHANNEL_REVIEW_ID if to_review else CHANNEL_ADOPT_ID)
            if not ch:
                await _reply(interaction, "⚠️ 移動先のチャンネルが見つからないため、移動をやめました (提案は残っています)。")
                return
            label = "📌 **【検討中】**" if to_review else "🎉 **【採用決定！】**"
            files = [await a.to_file() for a in message.attachments]
            kwargs = {"view": SuggestionView(review=False)} if to_review else {}
            await ch.send(content=f"{label} ({interaction.user.name})", embeds=[embed] if embed else [],
                          files=files, allowed_mentions=_NO_MENTIONS, **kwargs)
            await message.delete()
            await _reply(interaction, "🟡 検討チャンネルに移動しました！" if to_review else "🟢 採用チャンネルに移動しました！")
            return

        if action == "bug_read":
            async with _bugs_lock:
                if await _bugs_col.find_one({"message_id": str(message.id)}, {"_id": 1}):
                    await _reply(interaction, "すでにWikiのバグ一覧に追加されています！")
                    return
                desc = _bug_desc_from_embed(embed)
                if not desc or desc == "未記入":
                    await _reply(interaction, "⚠️ バグの内容が見つからないため、一覧に追加しませんでした。")
                    return
                await _bugs_col.insert_one(_bug_doc(str(message.id), _clip(desc, 4000)))
            await _reply(interaction, "👀 既読にしました。Wikiのバグ一覧に追加しました。")
            try:
                await message.reply(f"👀 既読 ({interaction.user.name}) ・Wikiのバグ一覧に追加済み",
                                    allowed_mentions=_NO_MENTIONS)
            except Exception:
                pass
            return

        if action in ("bug_resolve", "bug_delete"):
            await _bugs_col.delete_many({"message_id": str(message.id)})
            await message.delete()
            await _reply(interaction, "✅ バグ解決！ チャットとWiki一覧から削除しました。" if action == "bug_resolve"
                         else "🗑️ バグ報告を削除しました。")
            return
    except Exception as e:
        print(f"[tora] ボタン処理エラー: {type(e).__name__}: {e}")
        await _reply(interaction, "⚠️ 処理に失敗しました。もう一度試してください。")
    finally:
        _busy.discard(message.id)


class SuggestionView(discord.ui.View):
    """提案のボタン (採用 / 検討 / 削除)。検討チャンネルへ送る物は review=False で検討を外す。"""

    def __init__(self, review: bool = True):
        super().__init__(timeout=None)
        if not review:
            self.remove_item(self.review)

    @discord.ui.button(label="採用", emoji="🟢", style=discord.ButtonStyle.success, custom_id="tora:adopt")
    async def adopt(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _on_button(interaction, "adopt")

    @discord.ui.button(label="検討", emoji="🟡", style=discord.ButtonStyle.secondary, custom_id="tora:review")
    async def review(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _on_button(interaction, "review")

    @discord.ui.button(label="削除", emoji="🔴", style=discord.ButtonStyle.danger, custom_id="tora:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _on_button(interaction, "delete")


class BugView(discord.ui.View):
    """バグ報告のボタン (既読 / 解決 / 削除)。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="既読", emoji="👀", style=discord.ButtonStyle.primary, custom_id="tora:bug_read")
    async def bug_read(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _on_button(interaction, "bug_read")

    @discord.ui.button(label="解決", emoji="✅", style=discord.ButtonStyle.success, custom_id="tora:bug_resolve")
    async def bug_resolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _on_button(interaction, "bug_resolve")

    @discord.ui.button(label="削除", emoji="🔴", style=discord.ButtonStyle.danger, custom_id="tora:bug_delete")
    async def bug_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _on_button(interaction, "bug_delete")


def persistent_views() -> list:
    """再起動の後も前に投稿したボタンを押せるように setup_hook で add_view する物。"""
    return [SuggestionView(), BugView()]
