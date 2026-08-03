"""
summarize.py

1. /tmp/logs.json を読み込む
2. gemini-3-flash-preview で要約を生成（1日20回枠）
3. /tmp/summary_result.json に保存（次のスクリプトへ受け渡し）

Context Cachingは使用しない。要約テキストをBot返答時に直接プロンプトへ埋め込む。
"""

import os
import json
import time
from datetime import datetime, timezone

from google import genai
from google.genai import types

from embed_util import embed_document   # focus Tier3: summaries 意味検索用 embedding 規約
from model_chain import HEAVY_MODEL_CHAIN, ATTEMPTS_PER_MODEL, output_tokens

GEMINI_API_KEY         = os.environ["GEMINI_API_KEY"]
# 要約生成モデルは batch/model_chain.py に集約（gemma-4 は TPM 無制限枠の撤廃により 2026-08-03 に撤去）
INPUT_PATH             = "/tmp/logs.json"
OUTPUT_PATH            = "/tmp/summary_result.json"

SUMMARY_SYSTEM_PROMPT = """あなたはこのDiscordサーバーの古参メンバーです。
チャットログを読んで、後から見返す「観察メモ」を書いてください。
前置き・自己紹介・「以下の通り」などの導入文は一切不要です。各セクションの見出しから即座に本文を書き始めてください。
各セクションは具体的に書いてください（箇条書き指定のセクションは最低3項目）。

【重要: 人物の識別ルール】
- ログの各行には「表示名(uid:数字)」の形式でユーザーIDが付いています。
- 同じuidの発言は必ず同一人物です。名前が似ていてもuidが異なれば別人として扱ってください。
- 人物に言及する際は表示名のみを使用し、uidは書かないでください。

## 全体の雰囲気・トーン
（今日のサーバー全体の空気感。静か/賑やか、どんな感情が漂っていたかを簡潔に）

## 主なトピック
（今日話されていた話題を箇条書きで。各トピックに一言コメントを添える）

## メンバー別の動き
（発言が目立ったメンバーを名前付きで列挙し、各人の具体的な発言・行動・テンションを箇条書きで。
　形式は「- 表示名: 何をどう言っていたか・どんな様子だったか」。最低3人、いれば多いほどよい）

## メンバーの人間関係・関係性
（誰と誰が絡んでいたか、仲良さそうな組み合わせ、いじり合いの構図などを名前付きで）

## 記憶すべき事実・発言・決定
（後から思い出す価値のある情報を名前付きで箇条書き。誰かの主張・意見・予定・決定・報告・印象的な出来事。
　形式は「- 表示名: 〜と主張/予定/報告した」。憶測は書かず、ログにある事実だけを拾うこと）

## 感情の波・注目の瞬間
（何で盛り上がり何で冷めたか、テンションの上下と、特に反応が多かった発言・面白い流れを具体的に）

## 今日の内輪ネタ・キーワード
（このログに出てきた独自ワード・ノリ・内輪ジョークを箇条書きで）

## 直近の話題（最後の30分）
（ログの終盤で何が話されていたか。次の会話の糸口になる部分）

## ニックネーム・愛称マッピング
（同一人物が複数の呼び方で呼ばれている場合、以下の形式でJSON出力せよ。該当なければ空オブジェクト）
例: {"たろー": "山田太郎", "はな": "花子"}
必ずこのセクションの末尾にJSONのみを1行で記載せよ。
"""


def load_logs(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def format_messages_for_prompt(messages: list[dict]) -> str:
    if not messages:
        return "（この時間帯のメッセージはありません）"
    lines = []
    for msg in messages:
        ts  = msg["timestamp"][:16].replace("T", " ")
        uid = msg.get("author_id", "")
        # uid付きで出力することでAIが同一人物を正確に識別できる
        author_str = f"{msg['author']}(uid:{uid})" if uid else msg['author']
        lines.append(f"[{ts}] #{msg['channel']} {author_str}: {msg['content']}")
    return "\n".join(lines)


def _generate_with_model(client: genai.Client, model: str, log_text: str) -> str:
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Content(
                role="user",
                parts=[types.Part(text=(
                    "以下のDiscordチャットログの観察メモを書いてください。\n"
                    "【重要】前置きや導入文は一切書かないこと。各セクションの見出しから即座に本文を始めること。\n"
                    "各セクションは具体的に（箇条書き指定のセクションは最低3項目）書くこと。\n\n"
                    f"{log_text}"
                ))]
            )
        ],
        config=types.GenerateContentConfig(
            system_instruction=SUMMARY_SYSTEM_PROMPT,
            temperature=0.3,
            max_output_tokens=output_tokens(5500),   # thinking 系の空応答対策
        ),
    )
    return response.text


def generate_summary(client: genai.Client, log_text: str) -> str:
    for model, label in HEAVY_MODEL_CHAIN:
        for attempt in range(ATTEMPTS_PER_MODEL):
            try:
                print(f"[summarize] {label} ({model}) 試行{attempt + 1}/{ATTEMPTS_PER_MODEL}...")
                summary = _generate_with_model(client, model, log_text)
                if summary and summary.strip():
                    print(f"[summarize] 完了 ({len(summary)} chars) - {label}")
                    return summary.strip()
            except Exception as e:
                err = str(e)
                is_503 = "503" in err or "UNAVAILABLE" in err or "high demand" in err.lower()
                if is_503:
                    wait = (attempt + 1) * 20
                    print(f"[WARN] 503 ({label}), {wait}秒後にリトライ: {e}")
                    time.sleep(wait)
                else:
                    print(f"[ERROR] {label} 失敗: {e}")
                    break

    raise RuntimeError("全モデル・全リトライで要約生成に失敗しました")


def main():
    data = load_logs(INPUT_PATH)
    messages   = data.get("messages", [])
    fetched_at = data.get("fetched_at", datetime.now(timezone.utc).isoformat())

    if not messages:
        print("[summarize] No messages. Writing empty summary.")
        result = {
            "summary":       "（この時間帯はメッセージがありませんでした）",
            "message_count": 0,
            "created_at":    datetime.now(timezone.utc).isoformat(),
            "fetched_at":    fetched_at,
        }
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return

    client   = genai.Client(api_key=GEMINI_API_KEY)
    log_text = format_messages_for_prompt(messages)
    summary  = generate_summary(client, log_text)

    # ニックネームマッピングを抽出してresultに保存
    nickname_map = extract_nickname_map(summary)

    result = {
        "summary":       summary,
        "message_count": len(messages),
        "created_at":    datetime.now(timezone.utc).isoformat(),
        "fetched_at":    fetched_at,
        "nickname_map":  nickname_map,
    }

    # focus Tier3: summary doc を embedding して result に同梱（update_mongodb が保存）。
    # 失敗しても要約本体の書き出しは止めない（embedding は補助なので欠落許容＝後でバックフィル可）。
    try:
        vec = embed_document(client, summary)
        if vec:
            result["embedding"] = vec
            print(f"[summarize] embedding 付与: dim={len(vec)}")
        else:
            print("[summarize] embedding 空（スキップ・後でバックフィル）")
    except Exception as e:
        print(f"[summarize] embedding 失敗（スキップ・後でバックフィル）: {e}")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[summarize] Done. saved to {OUTPUT_PATH} nickname_map={nickname_map}")


def extract_nickname_map(summary: str) -> dict:
    """要約テキストからニックネームマッピングJSONを抽出"""
    import re
    # ## ニックネームセクション以降のJSONを探す
    pattern = r"## ニックネーム[^\n]*\n(\{[^\n]+\})"
    m = re.search(pattern, summary)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


if __name__ == "__main__":
    main()
