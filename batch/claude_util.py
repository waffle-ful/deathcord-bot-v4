"""バッチ用 Claude クライアント（requests 直REST。anthropic SDK 非依存）。

■ 役割
重いバッチ（要約 / 性格分析 / memory enrich / retro / focus）の呼び出しループの「先頭」に
Claude（既定 claude-haiku-5-5）を1回だけ差し込む。失敗・拒否・空・予算到達なら None を返し、
呼び出し側はそのまま従来の HEAVY_MODEL_CHAIN（Gemini 連鎖）へ落ちる。
呼び出し側はプロバイダの詳細を知らなくてよい:

    text = call_claude(prompt, max_tokens, schema=SOME_SCHEMA)
    if text:
        return text
    for model, label in HEAVY_MODEL_CHAIN: ...   # 従来どおり

■ 不活性条件（1リクエストも飛ばない）
  - env ANTHROPIC_API_KEY 未設定（workflow で secret 未登録＝空文字も同じ）
  - env CLAUDE_DISABLED=1
  - Mongo に繋がらない（予算が読めない＝安全側に倒して使わない）
  - 当月(UTC)の累計が CLAUDE_BUDGET_SOFT_USD 以上
  - 401/402/403/「credit balance」/404 を一度でも踏んだ（このプロセスの残りは使わない）

■ 月次予算は Render（main.py）と共有
Anthropic の org は1つ＝ソフト上限 $80 も1つ。main.py と同じ Mongo
discord_bot_db.claude_usage の _id="YYYY-MM"(UTC) に $inc する（batch 分は batch_calls にも計上）。
初回呼び出し前に1回読み、以後はプロセス内ミラーへ自分の分を足しつつ、長時間ジョブ中に
Render 側が使った分も拾えるよう BUDGET_REFRESH_SEC ごとに読み直す。

■ 送ってはいけないもの（main.py と同じ）
temperature / top_p / top_k（Haiku 5.5 は既定値以外を 400 で弾く）、thinking（adaptive が既定）、
assistant の prefill。呼び出し側の temperature 引数は Gemini 専用として黙って無視する。

■ structured outputs
JSON をパースするスクリプトは schema を渡す → output_config.format=json_schema で送る。
400 が schema/format に言及したら schema 無しで1回だけ再試行し、以後このプロセスでは schema を送らない。
schema 指定時は返ってきた本文を json.loads して検証し、壊れていれば（max_tokens で途中切れ等）None を返す
＝Claude が「成功」扱いで壊れた JSON を返し、Gemini フォールバックが走らない事故を防ぐ。

★Batches API は使わない（ユーザー判断で対象外）。
"""

import os
import json
import time
from datetime import datetime, timezone

import requests

# ---- 接続設定 ----------------------------------------------------------------
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY") or ""
ANTHROPIC_API_BASE = (os.environ.get("ANTHROPIC_API_BASE") or "https://api.anthropic.com").rstrip("/")
ANTHROPIC_VERSION  = "2023-06-01"
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL") or "claude-haiku-5-5"
CLAUDE_DISABLED    = (os.environ.get("CLAUDE_DISABLED", "").strip().lower()
                      in ("1", "true", "yes", "on"))
# バッチは速度不問なので medium（main.py の CLAUDE_EFFORT_BG と同じ既定）
CLAUDE_EFFORT_BATCH = os.environ.get("CLAUDE_EFFORT_BATCH") or "medium"
# 非ストリーミングで最大 16000 トークン出させるので長めに取る
CLAUDE_TIMEOUT_SEC = float(os.environ.get("CLAUDE_TIMEOUT_SEC") or 300)
# max_tokens は adaptive thinking の思考トークンも含む。Gemini 用の output_tokens() 下限(4000)より
# 大きめの下限を取る（課金は実際に出た分だけなので枠を広げても費用は増えない）。
CLAUDE_MIN_OUTPUT_TOKENS = 8000

# ★入力の長さ上限（文字数）。超えるプロンプトは Claude に送らず Gemini に回す。
#   analyze_nonbooster は1人あたり日報10日分（最大120件≒20万字）を送るため、そのまま流すと
#   1回 20万トークン超＝高単価帯（入力$0.50/MTok）×20人/日で月$70前後になり、共有予算$80を
#   batch だけで食い潰して会話側が Gemini に落ちる。日本語は概ね1字≒1〜1.3トークンなので
#   6万字≒8万トークン＝100k の安い帯に収まる。超える処理は無料枠の Gemini で十分。
CLAUDE_MAX_PROMPT_CHARS = int(os.environ.get("CLAUDE_MAX_PROMPT_CHARS") or 60000)

MONGODB_URI = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")   # consent_util と同じ解決順
DB_NAME     = "discord_bot_db"
USAGE_COL   = "claude_usage"

# ---- 月次予算ガード ----------------------------------------------------------
# ★main.py と同じ値に保つこと（main.py「Claude クライアント」節の CLAUDE_TIER_THRESHOLD /
#   CLAUDE_PRICE_LE_100K / CLAUDE_PRICE_GT_100K）。ズレると Render と batch で同じ予算を違う単価で数える。
# 単価は USD / 100万トークン。段階は「そのリクエストの入力合計（キャッシュ読み/書き込み）」が 100k 超か。
CLAUDE_TIER_THRESHOLD = 100_000
CLAUDE_PRICE_LE_100K = {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125}
CLAUDE_PRICE_GT_100K = {"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write": 0.625}
CLAUDE_BUDGET_SOFT_USD = float(os.environ.get("CLAUDE_BUDGET_SOFT_USD") or 80)
BUDGET_REFRESH_SEC = 600   # 長時間ジョブ中に Render 側の消費を拾うための再読込間隔

# プロセス内の状態（GitHub Actions の1ジョブ＝1プロセス＝1 run）
_state: dict = {
    "disabled": None,    # 使えない理由（None なら使える）
    "col":      None,    # claude_usage コレクション
    "month":    None,    # 読み込んだ月 "YYYY-MM"
    "usd":      0.0,     # 当月累計（Mongo 値＋このプロセスの加算）
    "read_at":  0.0,     # 最後に Mongo から読んだ time.monotonic()
    "schema_ok": True,   # structured outputs を送るか（schema 400 で False）
    "announced": False,  # 有効化ログを1回だけ出す
}


# =============================================================================
# JSON スキーマ（structured outputs 用）
#   ・全 object に additionalProperties:false と required を付ける
#   ・minLength/maxLength・minimum/maximum・minItems/maxItems・再帰は使わない（非対応）
#   ・null 可は anyOf で表す（プロンプトが「不明なら null」と指示している項目）
# 各スキーマは対応するプロンプトの出力形式と、パース後に実際に読むフィールドから作っている。
# =============================================================================

_NSTR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NSTR_LIST = {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props,
            "required": list(props.keys()), "additionalProperties": False}


# analyze_personality.py TONE_PROMPT → safe_merge が tone/communication_style/vocabulary を読む
PERSONALITY_TONE_SCHEMA = _obj({
    "tone": _NSTR, "communication_style": _NSTR, "vocabulary": _NSTR,
})

# analyze_personality.py CONTEXT_PROMPT（「言及が少なければ全項目null」）
PERSONALITY_CONTEXT_SCHEMA = _obj({
    "background": _NSTR, "relations": _NSTR, "interests_vibe": _NSTR,
})

# analyze_personality.py BIGFIVE_PROMPT → score_bigfive が items["1".."10"].score/evidence を読む。
# score の 1〜7 範囲は minimum/maximum 非対応のためスキーマでは縛らない（score_bigfive が範囲外を捨てる）。
_TIPI_ITEM = _obj({
    "score":    {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    "evidence": _NSTR,
})
PERSONALITY_BIGFIVE_SCHEMA = _obj({
    "items": _obj({str(i): _TIPI_ITEM for i in range(1, 11)}),
})

# analyze_nonbooster.py ANALYZE_PROMPT → merge_simple_profile が読む6項目（全項目null可）
NONBOOSTER_SCHEMA = _obj({
    "tone_tags":        _NSTR_LIST,
    "vibe":             _NSTR,
    "frequent_members": _NSTR_LIST,
    "personality":      _NSTR,
    "background":       _NSTR,
    "relations":        _NSTR,
})

# enrich_memories.py EXTRACT_PROMPT と focus_summary.py save_memories_from_focus の共通形。
# claims は文字列のみ（focus 側が _dedup_key(c) / c[:200] と文字列前提で扱う）。
MEMORIES_SCHEMA = _obj({
    "claims":   {"type": "array", "items": {"type": "string"}},
    "memories": {"type": "array", "items": _obj({
        "content":  {"type": "string"},
        "category": {"type": "string", "enum": ["趣味", "出来事", "感情", "計画"]},
    })},
})

# focus_summary.py PROFILE_UPDATE_PROMPT → save_profile が profile.<key> へ $set（null は捨てる）
FOCUS_PROFILE_SCHEMA = _obj({
    "tone": _NSTR, "vocabulary": _NSTR, "personality": _NSTR,
    "background": _NSTR, "relations": _NSTR, "interests_vibe": _NSTR,
})


# =============================================================================
# 予算
# =============================================================================

def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def claude_cost_usd(usage: dict | None) -> float:
    """Anthropic の生 usage から1リクエストの費用(USD)。main.py _claude_cost_usd と同じ計算。"""
    if not usage:
        return 0.0
    inp = int(usage.get("input_tokens") or 0)
    cr  = int(usage.get("cache_read_input_tokens") or 0)
    cw  = int(usage.get("cache_creation_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    p = CLAUDE_PRICE_GT_100K if (inp + cr + cw) > CLAUDE_TIER_THRESHOLD else CLAUDE_PRICE_LE_100K
    return (inp * p["input"] + out * p["output"]
            + cr * p["cache_read"] + cw * p["cache_write"]) / 1_000_000


def _disable(reason: str) -> None:
    if _state["disabled"] is None:
        _state["disabled"] = reason
        print(f"[claude] 無効化（{reason}）→ このプロセスの残りは Gemini 連鎖のみ")


def _get_col():
    if _state["col"] is None:
        if not MONGODB_URI:
            raise RuntimeError("MONGODB_URI / MONGO_URL が未設定")
        from pymongo import MongoClient   # import は使うときだけ（Claude 不活性時は不要）
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=15000)
        _state["col"] = client[DB_NAME][USAGE_COL]
    return _state["col"]


def _budget_ok() -> bool:
    """当月累計がソフト上限未満か。Mongo が読めなければ False（fail closed）。"""
    month = _month_key()
    need_read = (_state["month"] != month
                 or time.monotonic() - _state["read_at"] >= BUDGET_REFRESH_SEC)
    if need_read:
        try:
            doc = _get_col().find_one({"_id": month}) or {}
        except Exception as e:
            print(f"[WARN] claude 予算の読込失敗 → Claude を使わない（安全側）: {type(e).__name__}: {e}")
            _disable("予算が読めない（Mongo 不通）")
            return False
        mongo_usd = float(doc.get("usd") or 0.0)
        # 月が同じなら自分の加算分（まだ反映途中かもしれない）と比べて大きい方＝安全側
        _state["usd"] = mongo_usd if _state["month"] != month else max(_state["usd"], mongo_usd)
        _state["month"], _state["read_at"] = month, time.monotonic()
    if _state["usd"] >= CLAUDE_BUDGET_SOFT_USD:
        print(f"[WARN] claude 月次ソフト上限到達: {month} ${_state['usd']:.2f} >= "
              f"${CLAUDE_BUDGET_SOFT_USD:.2f}")
        _disable(f"月次予算到達 {month} ${_state['usd']:.2f}/${CLAUDE_BUDGET_SOFT_USD:.2f}")
        return False
    if not _state["announced"]:
        _state["announced"] = True
        print(f"[claude] 有効: model={CLAUDE_MODEL} effort={CLAUDE_EFFORT_BATCH} "
              f"今月({month}) ${_state['usd']:.2f}/${CLAUDE_BUDGET_SOFT_USD:.2f}")
    return True


def _record_usage(usage: dict | None) -> float:
    """1リクエスト分をミラーへ加算し、Mongo へ $inc（main.py と同じフィールド＋batch_calls）。"""
    cost = claude_cost_usd(usage)
    _state["usd"] += cost
    u = usage or {}
    inc = {
        "usd":         cost,
        "calls":       1,
        "batch_calls": 1,   # batch 側の呼び出し数（Render 分との内訳を見るため）
        # input_tokens はキャッシュ読み書きも含めた入力合計で記録する（main.py と同じ）
        "input_tokens": int(u.get("input_tokens") or 0)
                        + int(u.get("cache_read_input_tokens") or 0)
                        + int(u.get("cache_creation_input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
    }
    try:
        _get_col().update_one(
            {"_id": _state["month"] or _month_key()},
            {"$inc": inc, "$set": {"updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except Exception as e:
        # 記録失敗でも成果物は捨てない。ミラーには加算済みなのでこのプロセス内のガードは効く
        print(f"[WARN] claude 使用量の記録失敗: {type(e).__name__}: {e}")
    return cost


# =============================================================================
# 呼び出し
# =============================================================================

def claude_available() -> bool:
    """キー・停止フラグ・プロセス内無効化だけを見る（Mongo には触らない）。"""
    return bool(ANTHROPIC_API_KEY) and not CLAUDE_DISABLED and _state["disabled"] is None


def _error_str(status: int, raw: str) -> tuple[str, str, str]:
    """エラー応答を (etype, message, "<status> <etype>: <message>") に。"""
    etype, msg = "http_error", (raw or "")[:400]
    try:
        err = ((json.loads(raw) or {}).get("error") or {})
        if isinstance(err, dict):
            etype = err.get("type") or etype
            msg   = err.get("message") or msg
    except Exception:
        pass
    return etype, msg, f"{status} {etype}: {msg}".strip()


def _is_fatal(status: int, etype: str, msg: str) -> bool:
    """待っても直らない失敗（キー無効・権限・残高/請求・モデル名不正）。"""
    return (status in (401, 402, 403, 404)
            or etype in ("authentication_error", "permission_error", "billing_error", "not_found_error")
            or "credit balance" in (msg or "").lower())


def _is_schema_error(status: int, msg: str) -> bool:
    m = (msg or "").lower()
    return status == 400 and any(k in m for k in ("schema", "format", "output_config"))


def _post(payload: dict) -> requests.Response:
    # キーはヘッダのみ。URL/ログ/例外文には絶対に出さない
    headers = {"x-api-key": ANTHROPIC_API_KEY,
               "anthropic-version": ANTHROPIC_VERSION,
               "content-type": "application/json"}
    return requests.post(f"{ANTHROPIC_API_BASE}/v1/messages", json=payload,
                         headers=headers, timeout=CLAUDE_TIMEOUT_SEC)


def call_claude(prompt: str, max_tokens: int, schema: dict | None = None,
                system: str | None = None, label: str = "") -> str | None:
    """Claude を1回だけ叩いて本文を返す。使えない/失敗/拒否/空なら None（＝Gemini 連鎖へ）。

    429/529/5xx・通信エラーも待たずに None（Claude で粘らず Gemini へ横滑りする方針）。"""
    if not claude_available():
        return None
    if not _budget_ok():
        return None
    _plen = len(prompt or "") + len(system or "")
    if _plen > CLAUDE_MAX_PROMPT_CHARS:
        print(f"[claude{(':' + label) if label else ''}] 入力 {_plen}字 > 上限 "
              f"{CLAUDE_MAX_PROMPT_CHARS}字 → Claude を使わず Gemini へ（高単価帯回避）")
        return None

    tag = f"[claude{(':' + label) if label else ''}]"
    use_schema = schema is not None and _state["schema_ok"]
    payload = {
        "model":         CLAUDE_MODEL,
        "max_tokens":    max(int(max_tokens or 0), CLAUDE_MIN_OUTPUT_TOKENS),
        "messages":      [{"role": "user", "content": prompt}],
        "output_config": {"effort": CLAUDE_EFFORT_BATCH},
    }
    if system:
        payload["system"] = system

    for _ in range(2):   # 2周目は「schema 400 → schema 無しで再試行」専用
        body = dict(payload)
        if use_schema:
            body["output_config"] = {**payload["output_config"],
                                     "format": {"type": "json_schema", "schema": schema}}
        try:
            resp = _post(body)
        except Exception as e:
            print(f"[WARN] {tag} 通信エラー → Gemini へ: {type(e).__name__}: {e}")
            return None

        if resp.status_code >= 400:
            etype, msg, err = _error_str(resp.status_code, resp.text)
            if use_schema and _is_schema_error(resp.status_code, msg):
                print(f"[WARN] {tag} structured outputs が拒否された → schema 無しで再試行"
                      f"（以後このプロセスでは schema を送らない）: {err}")
                _state["schema_ok"] = False
                use_schema = False
                continue
            if _is_fatal(resp.status_code, etype, msg):
                print(f"[WARN] {tag} キー/残高/モデルエラー: {err}")
                _disable(f"致命的エラー {resp.status_code} {etype}")
            else:
                print(f"[WARN] {tag} {err} → Gemini へ")
            return None

        try:
            data = resp.json() or {}
        except Exception as e:
            print(f"[WARN] {tag} 応答JSON不正 → Gemini へ: {e}")
            return None
        break
    else:
        return None   # 到達しない（continue は schema 400 の1回だけ）

    # 課金は成功応答に乗った usage で確定する（拒否/本文ゼロでもトークンは消費済みなので計上）
    cost = _record_usage(data.get("usage"))

    blocks = data.get("content") or []
    text = "".join((b.get("text") or "") for b in blocks
                   if isinstance(b, dict) and b.get("type") == "text").strip()
    stop = data.get("stop_reason")
    if stop == "refusal":
        print(f"[WARN] {tag} refusal → Gemini へ: stop_details={data.get('stop_details')}")
        return None
    if not text:
        print(f"[WARN] {tag} 本文ゼロ（stop_reason={stop}）→ Gemini へ")
        return None
    if schema is not None:
        # JSON を期待する呼び出しでは壊れた JSON を「成功」扱いにしない（max_tokens 途中切れ等）。
        # schema 無し再試行の後も同じ（その場合 ```json 囲みは呼び出し側の既存掃除に任せて剥がしてから見る）
        try:
            json.loads(text.replace("```json", "").replace("```", "").strip())
        except Exception:
            print(f"[WARN] {tag} JSON が壊れている（stop_reason={stop}）→ Gemini へ: {text[:80]}")
            return None
    print(f"{tag} 成功 ({len(text)}文字, ${cost:.4f}, 今月累計 ${_state['usd']:.2f})")
    return text
