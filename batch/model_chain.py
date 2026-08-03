"""重いバッチ処理（focus / 要約 / 性格分析 / memory enrich）共通のモデル・フォールバック連鎖。

■ なぜ集中管理するか
モデルIDは以前 batch/*.py に個別直書きで、差し替えのたびに grep 全置換が必要だった
（doc/known-issues.md「モデルIDの集中管理が欲しい」）。ここ1箇所を書き換えれば全バッチに効く。

■ 2026-08-03: gemma-4 系を全面撤去
gemma-4（31b / 26b-a4b）は「TPM 実質無制限」を理由に重い処理の主力に据えていたが、
その無制限枠が撤廃されたため主力に据える利点が消滅した。以後は Gemini flash 系のみを
「別モデル＝別quotaプール」として数珠つなぎにし、無料枠の容量429（共有プール枯渇。
深夜＝欧米ピークで顕著）が来たら次のプールへ横滑りして凌ぐ。

■ 順番の意図
3.5-flash（新しく速い・枠が別）を主にし、実績のある 2.5 系を中段、最後に 3.6-flash。
上から順に試し、最初に本文が返ったモデルを採用する。

★モデルIDは /listmodels（= client.models.list()）で実在確認してから触ること。
  無効名は例外で黙ってスキップされ「容量が増えた気がするだけで実際ゼロ」になる。
  現行4件は 2026-08-03 に models.list で実在確認済み。
"""

# (model_id, ログ用ラベル) を上から順に試す
HEAVY_MODEL_CHAIN: list[tuple[str, str]] = [
    ("models/gemini-3.5-flash",      "①3.5-flash"),
    ("models/gemini-2.5-flash-lite", "②2.5-flash-lite"),
    ("models/gemini-2.5-flash",      "③2.5-flash"),
    ("models/gemini-3.6-flash",      "④3.6-flash"),
]

# 単発呼び出し（フォールバック不要な軽い用途）で使う先頭モデル
PRIMARY_MODEL: str = HEAVY_MODEL_CHAIN[0][0]

# 1モデルあたりのリトライ回数。連鎖が4段になったので1モデルを叩き続ける意味は薄い
# （容量429は同一モデルでは待っても空かないが、別モデル＝別プールなら即通ることが多い）。
# 2回×4モデル=8回 で、旧実装（5回×2モデル=10回）と総試行数・総待ち時間はほぼ同等。
ATTEMPTS_PER_MODEL: int = 2

# thinking 系モデルは思考トークンを max_output_tokens から食う。枠が小さいと
# 思考だけで枯れて finish_reason=MAX_TOKENS の空応答になる（gemma-4 で実際に踏んだ罠と同じ）。
# バッチは速度もコストも不問なので、下限を大きめに取って空応答を潰す。
MIN_OUTPUT_TOKENS: int = 4000


def output_tokens(max_tokens: int) -> int:
    """呼び出し側の希望枠に thinking 用の下限を効かせる。"""
    return max(max_tokens, MIN_OUTPUT_TOKENS)
