"""Discord REST への Embed 投稿の共通ヘルパ（focus / 日報 / 遡及日報 で共用）。

■ なぜ集中管理するか
2026-08-04、focus_summary.py で以下を立て続けに踏んだ。日報(post_summary.py)と
遡及日報(retro_summarize.py)も同型の実装だったため、同じ地雷を抱えたままだった。

① **6000字上限は「1メッセージ内の全embed合計」**。embedを複数まとめて1回のPOSTに
   束ねると 400 `code: 50035 MAX_EMBED_SIZE_EXCEEDED`。→ 1メッセージ1embedで送る。
   （エラーが `errors.embeds._errors` = 配列レベルなら合計超過、`embeds.0._errors` なら個別）

② **仕様上限(6000字/25フィールド)の内側でも、規模が大きいと 400 ではなく
   `500 {"code": 0}` が返る**。実測: 5483字/8フィールド は 500 を5連続、直後に同一
   チャンネルへ送った 3076字/4フィールド は一発成功。さらに同じ本文を平文で送ると
   全通したので、文字内容（絵文字・エンコード）ではなく embed の規模が原因。
   500は理由を返さないので、こちらで割って切り分けるしかない（post_embeds が自動化）。

③ **投稿失敗で成果物を捨てていた**。レポート1本の生成にLLM呼び出し＋429待ちで数分
   かかっており、1回の失敗で全部を失うのは損失が大きい。5xx/429/通信エラーは粘り、
   ペイロード不正(その他4xx)は即諦め、最後は平文に落としてでも本文を届ける。

■ 使い方
    from discord_post import pack_fields_into_embeds, post_embeds
    embeds = pack_fields_into_embeds(fields, color=0x5865F2,
                                     title_for=lambda i, n: "タイトル" if i == 0 else "タイトル（続き）",
                                     footer_for=lambda i, n: f"フッタ • {i+1}/{n}")
    post_embeds(url, headers, embeds, label="日報")
"""

import time
from typing import Callable

import requests

# Discordの公称上限は 6000字 / 25フィールド だが、②のとおりその内側でも500が返る。
# 実績OK=3076字/4フィールド、実績NG=5483字/8フィールド。実績のある側に寄せて詰める。
EMBED_CHAR_BUDGET = 3000
EMBED_MAX_FIELDS  = 4

# 1フィールドの値の上限（Discord仕様は1024。余裕を見て1020で運用）
FIELD_VALUE_LIMIT = 1020

# 平文フォールバック時の1メッセージ上限（Discord仕様は2000）
PLAIN_TEXT_LIMIT = 1900

# 引用した発言に @everyone やメンションが混ざっていても絶対に飛ばさない。
# embedは元々メンションが発火しないが、平文フォールバックは発火するので必須。
NO_MENTIONS = {"parse": []}


def split_for_field(text: str, limit: int = FIELD_VALUE_LIMIT) -> list[str]:
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


def embed_chars(embed: dict) -> tuple[int, int]:
    """Discordが6000字上限の対象に数える文字数と、フィールド数を返す（診断用）。"""
    n  = len(embed.get("title", "")) + len(embed.get("description", ""))
    n += len(embed.get("footer", {}).get("text", ""))
    n += sum(len(f.get("name", "")) + len(f.get("value", "")) for f in embed.get("fields", []))
    return n, len(embed.get("fields", []))


def pack_fields_into_embeds(
    fields: list[dict],
    *,
    color: int,
    title_for: Callable[[int, int], str],
    footer_for: Callable[[int, int], str] | None = None,
    timestamp: str | None = None,
) -> list[dict]:
    """フィールド列を、500を踏まない規模のembedに詰め分ける。

    title_for(i, total) / footer_for(i, total) は「何通目/全何通か」を受け取って
    見出し・フッタ文字列を返す。footer は全embedに付ける（構造を揃えておくと、
    失敗したembedだけ形が違うという切り分けのノイズが消える）。
    timestamp は最後のembedにだけ付ける。
    """
    groups, cur, cur_chars = [], [], 0
    for field in fields:
        fc = len(field.get("name", "")) + len(field.get("value", ""))
        if (cur_chars + fc > EMBED_CHAR_BUDGET or len(cur) >= EMBED_MAX_FIELDS) and cur:
            groups.append(cur)
            cur, cur_chars = [], 0
        cur.append(field)
        cur_chars += fc
    if cur:
        groups.append(cur)

    total, embeds = len(groups), []
    for i, group in enumerate(groups):
        embed = {"color": color, "title": title_for(i, total), "fields": group}
        if footer_for is not None:
            embed["footer"] = {"text": footer_for(i, total)}
        if timestamp and i == total - 1:
            embed["timestamp"] = timestamp
        embeds.append(embed)
    return embeds


def post_json(url: str, headers: dict, payload: dict, what: str, tries: int = 5) -> bool:
    """Discordへの投稿を再試行つきで行う。

    ・5xx / 通信エラー … Discord側の一時障害の可能性。指数バックオフで再試行。
    ・429            … retry_after に従って待つ（複数通に分ける以上いつか当たる）。
    ・その他4xx      … ペイロード不正なので再試行しても同じ。即諦めて診断材料を吐く。
    """
    payload = {**payload, "allowed_mentions": NO_MENTIONS}
    for attempt in range(tries):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
        except requests.RequestException as e:
            wait = min(2 ** attempt * 2, 30)
            print(f"[WARN] {what}: 通信エラー({e}) → {wait}s後に再試行 ({attempt+1}/{tries})")
            time.sleep(wait)
            continue

        if resp.status_code in (200, 201):
            return True

        if resp.status_code == 429:
            try:
                wait = float(resp.json().get("retry_after", 5.0)) + 0.5
            except Exception:
                wait = 5.0
            print(f"[WARN] {what}: 429 → {wait:.1f}s待機して再試行 ({attempt+1}/{tries})")
            time.sleep(wait)
            continue

        if 500 <= resp.status_code < 600:
            wait = min(2 ** attempt * 2, 30)
            print(f"[WARN] {what}: {resp.status_code} Discord側エラー → {wait}s後に再試行 "
                  f"({attempt+1}/{tries}) {resp.text[:200]}")
            time.sleep(wait)
            continue

        print(f"[ERROR] {what}: 投稿失敗 {resp.status_code} {resp.text[:500]}")
        return False

    print(f"[ERROR] {what}: 再試行{tries}回すべて失敗")
    return False


def post_embed_bisect(url: str, headers: dict, embed: dict, what: str, depth: int = 0) -> bool:
    """5xxが続くembedをフィールド単位で二分割して投稿し直す（救出と原因特定を兼ねる）。

    ・割ったら通った → 規模が原因。通った粒度がログに残るので次の予算を決められる。
    ・1フィールドまで割っても500 → そのフィールドが原因。nameと先頭を吐いて特定する。
    """
    if post_json(url, headers, {"embeds": [embed]}, what, tries=5 if depth == 0 else 2):
        return True

    fields = embed.get("fields", [])
    if len(fields) <= 1:
        f = fields[0] if fields else {}
        print(f"[ERROR] {what}: 単一フィールドでも500。これが原因フィールド → "
              f"name={f.get('name','')!r} value長={len(f.get('value',''))}字 "
              f"先頭100字={f.get('value','')[:100]!r}")
        return False

    mid = len(fields) // 2
    print(f"[WARN] {what}: {len(fields)}フィールド({embed_chars(embed)[0]}字)を "
          f"{mid}+{len(fields)-mid} に分割して再試行")
    ok = True
    for i, part in enumerate((fields[:mid], fields[mid:])):
        sub = dict(embed)
        sub["fields"] = part
        sub.pop("timestamp", None)
        sub["footer"] = {"text": f"{what}-{i+1}（分割投稿）"}
        if i:
            sub["title"] = f"{embed.get('title','')}（続き）"
        if not post_embed_bisect(url, headers, sub, f"{what}-{i+1}", depth + 1):
            ok = False
        time.sleep(1.0)
    return ok


def post_as_plaintext(url: str, headers: dict, embed: dict, what: str) -> bool:
    """embed投稿がどうしても通らないときの最後の砦。中身を平文に落として送る。

    embedは見栄えの都合でしかなく、本文が届くことのほうが重要。"""
    blocks = [f"**{f.get('name','')}**\n{f.get('value','')}" for f in embed.get("fields", [])]
    text   = (embed.get("title", "") + "\n\n" + "\n\n".join(blocks)).strip()
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        while len(block) > PLAIN_TEXT_LIMIT:
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(block[:PLAIN_TEXT_LIMIT]); block = block[PLAIN_TEXT_LIMIT:]
        if cur and len(cur) + 2 + len(block) > PLAIN_TEXT_LIMIT:
            chunks.append(cur); cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        chunks.append(cur)

    ok = True
    for i, chunk in enumerate(chunks):
        if not post_json(url, headers, {"content": chunk}, f"{what} 平文{i+1}/{len(chunks)}", tries=3):
            ok = False
        time.sleep(1.0)
    return ok


def post_embeds(url: str, headers: dict, embeds: list[dict],
                label: str = "embed", pace: float = 1.0) -> bool:
    """embed列を1メッセージ1embedで投稿する。失敗しても本文を落とさないよう段階的に粘る。

    素直に投稿 → だめなら二分割で救出＋原因特定 → それでもだめなら平文。
    戻り値は「全部届いたか」。届かなかった部分があれば False。
    """
    all_ok = True
    for idx, embed in enumerate(embeds):
        chars, nfields = embed_chars(embed)
        longest = max((len(f.get("value", "")) for f in embed.get("fields", [])), default=0)
        what = f"{label} {idx+1}/{len(embeds)}"
        # 失敗時に「6000/25/1024のどれに当たったのか」を後から言い当てられるようにしておく
        print(f"[post] {what} 送信: {chars}字 / {nfields}フィールド (最長field={longest}字)")

        if post_embed_bisect(url, headers, embed, what):
            print(f"[post] 投稿成功 ({idx+1}/{len(embeds)})")
        else:
            print(f"[WARN] {what}: embedを諦めて平文で投稿を試みます")
            if post_as_plaintext(url, headers, embed, what):
                print(f"[post] 平文フォールバック成功 ({idx+1}/{len(embeds)})")
            else:
                print(f"[ERROR] {what}: 平文フォールバックも失敗。この部分は失われました")
                all_ok = False
        time.sleep(pace)
    return all_ok
