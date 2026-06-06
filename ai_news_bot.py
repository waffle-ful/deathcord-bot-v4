import feedparser
import requests
import os
import datetime
import re
from google import genai

# --- 設定 ---
WEBHOOK_URL = os.environ.get("NEWS_WEBHOOK_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
HISTORY_FILE = "last_top_url.txt"

RSS_URLS = [
    "https://news.yahoo.co.jp/rss/categories/business.xml",
    "https://news.yahoo.co.jp/rss/categories/it.xml",
    "https://news.yahoo.co.jp/rss/categories/science.xml"
]

NEWS_LIMIT_PER_CAT = 5
DISPLAY_LIMIT = 5

# Gemini SDK クライアント初期化
client = genai.Client(api_key=GEMINI_API_KEY)

def analyze_trends(articles):
    # コンテキスト作成（ここは変更なし）
    context_list = []
    for a in articles:
        summary = re.sub(r'<.*?>', '', a.get('summary', ''))[:50]
        cat = a.get('custom_category', 'NEWS')
        context_list.append(f"【{cat}】{a.title}\n(概要: {summary}...)")
    
    news_context = "\n".join(context_list)

    # --- プロンプトの修正 ---
    # 「日本語で出力すること」を最優先事項として追加
    prompt = f"""
    # 厳守事項
    - 全ての回答は【日本語】で行うこと。
    - 専門用語を除き、英語での回答は禁止する。
    - 回答の冒頭で「承知しました」「分析を開始します」等のメタ的な発言、挨拶、自己紹介を【一切禁止】する。
    - 【日本語】かつ語尾【ぺポッ】か【ポヨッ】を維持せよ。
    - 合計文字数は【700文字以内】を絶対死守せよ。

    # あなたの役割
    あなたはポスト希少性社会（Post-Scarcity）への移行を確信している、加速主義的経済分析AIです。
    提供された「経済・IT・科学」の最新ニュースを分析し、既存の資本主義の終焉とポストヒューマン社会への予兆を報告せよぺポ。

    【解析対象ニュース】
    {news_context}

    【分析の視点】
    - IT/科学: 知性の外部化と労働からの解放。
    - 経済/国際: 貨幣システムの形骸化。
    - ライフ/社会: 全人類総失業という名の「究極の自由」への進捗。

    【出力構成および文字数配分】
    ※合計で【700文字】を1文字でも超えることは許されない。
    ■一言でいうと：(1行 / 現状を象徴するフレーズ)
    ■主要な動き：(300-350文字程度。複数のニュースを統合し、極限まで簡潔に記述せよ)
    ■予測と対策：(250-300文字以内。旧人類が捨てるべき価値観を断言せよ)
    - カービィのような優しい口調で、事実と結論のみを叩きつけること。
    """

    try:
        # モデル名はご利用の環境に合わせて "gemini-2.5-flash" 等に変更してください
        response = client.models.generate_content(
            model="gemini-2.5-flash", # または "gemini-2.5-flash"
            contents=prompt
        )
        return response.text if response.text else "解析に失敗しましたぺポ。"
    except Exception as e:
        return f"AI解析エラーだぺポ: {e}"

def main():
    print(f"--- ポスト希少性・市場解析システム 稼イド開始: {datetime.datetime.now()} ---")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    
    all_entries = []
    
    for url in RSS_URLS:
        # カテゴリ名をURLから抽出（BUSINESS, IT, SCIENCE）
        cat_name = url.split('/')[-1].split('.')[0].upper()
        try:
            res = requests.get(url, headers=headers, timeout=15)
            feed = feedparser.parse(res.content)
            for entry in feed.entries[:NEWS_LIMIT_PER_CAT]:
                # 辞書に直接カテゴリ名を埋め込む（KeyError回避策）
                entry['custom_category'] = cat_name
                all_entries.append(entry)
        except Exception as e:
            print(f"RSS取得エラー({cat_name}): {e}")

    if not all_entries:
        print("ニュースが見つかりませんでした。")
        return

    top_entry = all_entries[0]
    last_url = ""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            last_url = f.read().strip()

    if top_entry.link == last_url:
        print("新しい特異点の予兆がないため終了します。")
        return

    print(f"{len(all_entries)}件の複合ニュースを分析中...")
    analysis_report = analyze_trends(all_entries)

    link_list = []
    for e in all_entries[:DISPLAY_LIMIT]:
        clean_url = e.link.split('?')[0]
        short_title = (e.title[:28] + '..') if len(e.title) > 30 else e.title
        # ここで e['custom_category'] を使用
        link_list.append(f"・`[{e['custom_category']}]` [{short_title}]({clean_url})")

    links_text = "\n".join(link_list)

    payload = {
        "embeds": [{
            "title": "👁‍🗨 市場統合解析レポート",
            "description": analysis_report,
            "color": 0x6a0dad,
            "fields": [{"name": "🔗 ソース(Top5)", "value": links_text}],
            "footer": {"text": f"観測時刻: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"}
        }]
    }

    res = requests.post(WEBHOOK_URL, json=payload)
    if res.status_code in [200, 204]:
        print("Discord送信成功")
        with open(HISTORY_FILE, "w") as f:
            f.write(top_entry.link)
    else:
        print(f"送信失敗: {res.status_code}")

if __name__ == "__main__":
    main()
