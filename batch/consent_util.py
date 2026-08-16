"""
consent_util.py

利用規約の同意ゲート（main.py の `system._id="tos_gate"`）を **batch 側にも適用** するための共通部品。

【なぜ必要か（2026-08-16 に判明した穴）】
main.py:2834 の同意ガードは `messages` コレクションへの記録を止めるだけで、
日報 / focus / retro の要約は Discord REST API から生ログを**直接**読むため、
未同意ユーザーの発言がそのまま Gemini（無料枠 = Google の製品改善・機械学習に利用され、
人間レビュアーが読み得る）へ送られていた。main.py 側のコメント
「要約対象からも自動的に外れる」は事実誤認だった。

対象（このモジュールを使う側）:
  - fetch_discord_logs.py … 4時間ごとの要約 → 日報
  - focus_summary.py      … /focus
  - retro_summarize.py    … /retroreport（retro_backfill.py も同経路）
  - analyze_personality.py / analyze_nonbooster.py / enrich_memories.py
      … messages・summaries 由来。ゲート有効化後も「過去に溜まった未同意者の発言」を
        毎晩 LLM に送り続けてしまうため、母集団から外す。

【真実の所在】
規約バージョンは main.py の TOS_VERSION だが、batch 側にハードコードすると二重管理になる。
`system._id="tos_gate"` ドキュメントに保存された version を唯一の真実として扱う。
（main.py で TOS_VERSION を上げても `/規約ゲート設定` 等で `_save_tos_gate()` が
  再実行されるまで doc 側は古い版のまま。改定時は必ず設定コマンドを叩き直すこと。）

【fail-closed】
ゲートは「同意していない人のデータを送らない」ための仕組みなので、判定できない時は
送らない側へ倒す = 処理を中止（SystemExit(1)）する。MONGODB_URI 未設定・接続失敗が該当。
ゲート自体が未導入（doc 無し / enabled=False）のときだけ、従来どおり素通しする。
"""

import os
import re

from pymongo import MongoClient

MONGODB_URI = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
DB_NAME     = "discord_bot_db"

# ユーザーメンション。<@123> と <@!123>（旧ニックネーム形式）の両方。
# ロールメンション <@&123> / チャンネル <#123> は & と # があるのでマッチしない（意図通り）。
_MENTION_RE = re.compile(r"<@!?(\d+)>")
MENTION_MASK = "[非公開ユーザー]"


class ConsentFilter:
    """同意済みユーザーの判定器。

    enabled=False（ゲート未導入）のときは全員を通す＝従来挙動。
    """

    def __init__(self, enabled: bool, agreed_ids: set, version: int):
        self.enabled          = bool(enabled)
        self.agreed_ids       = agreed_ids
        self.version          = version
        self.masked_mentions  = 0   # mask_content が置換した件数（ログ用の累計）

    def allows(self, author_id) -> bool:
        """この author_id の発言を LLM に送ってよいか。"""
        if not self.enabled:
            return True
        if author_id is None:
            return False          # 判定不能は送らない側へ
        return str(author_id) in self.agreed_ids

    def mongo_filter(self) -> dict:
        """users コレクションの find 条件に混ぜる用（未同意者を母集団から外す）。

        使い方: users_col.find({**{...既存条件...}, **cf.mongo_filter()})
        """
        if not self.enabled:
            return {}
        return {"tos_agreed.version": {"$gte": self.version}}

    def mask_content(self, text: str) -> str:
        """本文中の「未同意ユーザーへのメンション」を [非公開ユーザー] に置換する。

        同意者の発言そのものは残す（他人の発言を検閲することになるため）が、
        そこに含まれる未同意者の**ID**だけは LLM へ送る前に決定的に落とす。
        ID は一意なので誤爆しない。

        ★表示名・ニックネームには手を出さない。nickname_map に無い愛称は拾えず網羅できない上、
          一般名詞と同じ名前で誤爆して要約を壊す。中途半端に効く対策は「効いているつもり」を
          作るぶん有害なので、確実に効く範囲だけを対象にする。
        """
        if not self.enabled or not text:
            return text

        def _sub(m):
            if self.allows(m.group(1)):
                return m.group(0)
            self.masked_mentions += 1
            return MENTION_MASK

        return _MENTION_RE.sub(_sub, text)

    def filter_messages(self, msgs: list, key: str = "author_id") -> list:
        """整形済みメッセージ dict のリストから未同意者の発言を落とす。
        除外件数は呼び出し側でログに出せるよう、戻り値の長さ差分で分かる。"""
        if not self.enabled:
            return msgs
        return [m for m in msgs if self.allows(m.get(key))]

    def describe(self) -> str:
        if not self.enabled:
            return "規約ゲート無効（フィルタなし）"
        return f"規約ゲート有効（v{self.version} 同意済み {len(self.agreed_ids)}人のみ対象）"


def load_consent_filter(db=None) -> ConsentFilter:
    """`system._id="tos_gate"` と `users.tos_agreed` を読んで ConsentFilter を作る。

    db: 既に接続済みの pymongo Database があれば渡す（接続の重複を避ける）。
        None なら MONGODB_URI で自前接続する。
    """
    own_client = None
    try:
        if db is None:
            if not MONGODB_URI:
                # ゲートの状態を確認できない以上、送らない側へ倒す。
                print("[consent] MONGODB_URI / MONGO_URL が未設定です。"
                      "規約同意の判定ができないため処理を中止します。"
                      "（workflow の env に MONGODB_URI を追加してください）")
                raise SystemExit(1)
            own_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=15000)
            db = own_client[DB_NAME]

        gate = db["system"].find_one({"_id": "tos_gate"}) or {}
        if not gate.get("enabled", False):
            print("[consent] 規約ゲートは無効。全メッセージを対象にします（従来挙動）。")
            return ConsentFilter(False, set(), int(gate.get("version", 1) or 1))

        version    = int(gate.get("version", 1) or 1)
        agreed_ids = {
            str(d["_id"])
            for d in db["users"].find({"tos_agreed.version": {"$gte": version}}, {"_id": 1})
        }
        cf = ConsentFilter(True, agreed_ids, version)
        print(f"[consent] {cf.describe()}")
        return cf

    except SystemExit:
        raise
    except Exception as e:
        # 接続・クエリ失敗。ゲートが有効かどうかも分からないので中止する（fail-closed）。
        print(f"[consent] 規約同意リストの取得に失敗しました: {e}")
        print("[consent] 未同意者のデータを送信しないため、処理を中止します。")
        raise SystemExit(1)
    finally:
        if own_client is not None:
            own_client.close()


_cached_filter: ConsentFilter = None


def get_consent_filter(db=None) -> ConsentFilter:
    """プロセス内で1度だけ読み込む版。

    retro_backfill のように同じ処理を日数分ループするスクリプトで、
    毎回 MongoDB に問い合わせないためのキャッシュ。
    （バッチは長くても数十分なので、実行中の同意状況の変化は次回実行で反映されれば十分）
    """
    global _cached_filter
    if _cached_filter is None:
        _cached_filter = load_consent_filter(db)
    return _cached_filter
