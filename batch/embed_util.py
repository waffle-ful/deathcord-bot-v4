"""embed_util.py — summaries 意味検索(focus Tier3)の embedding 規約の【唯一の真実】。

3つの呼び出し元が同じ embedding 空間を共有しなければならない:
  - summarize.py        … 新規 summary doc を書くとき(RETRIEVAL_DOCUMENT)
  - embed_summaries.py  … 既存 doc のバックフィル(RETRIEVAL_DOCUMENT)
  - focus_summary.py    … keyword クエリ側(RETRIEVAL_QUERY)

model / 次元 / 切り詰め長 / task_type のどれかがズレると、別空間のベクトルを
cosine 比較することになり recall が【無言で】死ぬ(エラーにならず結果だけ劣化)。
だから規約はこの1ファイルに固定し、3呼び出し元は必ずここを import する。

※ main.py の memories embedding とは別系統。相互に cosine 比較しないので、
  そちらと次元やモデルを合わせる必要はない(あちらは触らない)。
※ 次元は output_dimensionality を【指定しない】=モデル既定(3072・正規化済)を全員で使う。
  ここを後から変えると既存の埋め込み全てが無効化され再バックフィルが必要になる。
"""

EMBED_MODEL     = "models/gemini-embedding-001"
EMBED_DIM       = 3072   # gemini-embedding-001 のネイティブ次元（正規化済）。
                         # 明示固定する理由: requirements-batch は google-genai を未ピン(>=)。将来の版で
                         # 既定次元が変わると、版またぎで埋めた doc 同士の長さが食い違い cosine が無言で
                         # 0.0 を返す（=embed_util を作った目的そのものの事故）。3072 はネイティブなので
                         # 明示しても今日の挙動は不変。ここを変えると既存埋め込みは全て要・再バックフィル。
EMBED_DOC_CHARS = 1800   # 入力上限(~2048トークン)に収める保守的キャップ。auto_truncate も併用。


def _embed(client, text, task_type):
    """1ベクトルを返す。取れなければ None（呼び出し側でスキップ/リトライ判断）。"""
    from google.genai import types
    resp = client.models.embed_content(
        model=EMBED_MODEL,
        contents=(text or "")[:EMBED_DOC_CHARS],
        config=types.EmbedContentConfig(
            task_type=task_type, auto_truncate=True, output_dimensionality=EMBED_DIM,
        ),
    )
    if getattr(resp, "embeddings", None):
        return list(resp.embeddings[0].values)
    if getattr(resp, "embedding", None):       # 旧形状フォールバック
        return list(resp.embedding.values)
    return None


def embed_document(client, text):
    """summary doc 側（書込・バックフィル共通）。task_type=RETRIEVAL_DOCUMENT。"""
    return _embed(client, text, "RETRIEVAL_DOCUMENT")


def embed_query(client, text):
    """keyword クエリ側。task_type=RETRIEVAL_QUERY。
    短い検索語 vs 長い doc 重心 という非対称検索の精度を上げるため doc 側と type を分ける。"""
    return _embed(client, text, "RETRIEVAL_QUERY")


def cosine(a, b):
    """純Python・依存なしのコサイン類似度（batch に numpy は無い）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na  += x * x
        nb  += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))
