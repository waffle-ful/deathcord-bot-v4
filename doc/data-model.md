# データモデル (MongoDB)

**Database**: `discord_bot_db`
**Driver**: 本体は `motor`（async）、バッチは `pymongo`（sync）

## 全コレクション一覧

| コレクション | 論理的役割 | 主な書き手 | 主な読み手 | ドキュメント数目安 |
|-------------|-----------|----------|----------|---------------|
| `users` | ユーザー状態（XP・プロフィール・記憶） | main.py + batch/analyze_* | main.py + batch/analyze_*, focus_summary | サーバーメンバー数 |
| `system` | サーバー設定・状態（性格・ニックネーム・Bump） | main.py + batch/update_mongodb | main.py + market_report | 数件〜数十件 |
| `summaries` | サーバー要約アーカイブ | batch/update_mongodb + retro_summarize | main.py + post_summary + analyze_* | 毎2h追加・永久保持 |
| `market_predictions` | 週次市場予想データ | market_report.py | market_report.py | 1件/週 |

## コレクション: `users`

`_id` = Discord user ID（**文字列**、`str(user.id)`）。

### ブースターユーザーの完全形

```json
{
  "_id": "123456789012345678",
  "name": "表示名",                       // display_name（最後に観測した値）
  "xp": 12345,                            // 累計 XP
  "title": "任意の二つ名",                  // /as コマンドで設定、nickname に反映
  "bump_count": 42,                       // Bump 実行回数
  "invite_count": 3,                      // 招待人数
  "streak_days": 5,                       // 連続参加日数
  "conv_count": 100,                      // ブースター会話数（analyze_personality の閾値 >=10）
  "conv_count_nb": 50,                    // 非ブースター時代からの会話数（30毎に分析トリガー）
  "last_xp_at": ISODate("..."),           // XP クールダウン（60s）の判定用
  "last_active_date": "2026-04-24",       // JST 日付文字列、streak 判定用
  "last_content": "...",                  // 直前の発言（連投スパム検知）
  "nb_migrated": true,                    // nonbooster_history → butler_history マイグ済みフラグ
  "notified": false,                      // Bump 忘れ通知に使用（旧設計、現在未使用に近い）

  "profile": {                            // ブースター向け詳細プロフィール（analyze_personality が生成）
    "tone": "敬語なし・テンション高め",
    "communication_style": "短文多め・リアクション早い",
    "vocabulary": "やばい・草・〜じゃん",
    "personality": "ムードメーカーで面倒見がよい",
    "background": "シニアマネージャー・古参メンバー",
    "relations": "花子と特に親密",
    "interests_vibe": "ゲーム全般・深夜帯",
    "birthday": "3月15日",                 // PROFILE_EXTRACT_PROMPT が会話から抽出
    "hobbies": ["FPS", "読書"],            // 最大20件
    "memo": ["ペットを飼っている"]
  },

  "simple_profile": {                     // 非ブースター向け簡易プロフィール（analyze_nonbooster）
    "vibe": "静かだが鋭い",
    "tone_tags": ["短文", "絵文字少なめ"],
    "personality": "観察者タイプ",
    "background": "...",
    "relations": "...",
    "updated_at": "2026-04-23T15:00:00+00:00"
  },

  "butler_history": [                     // メイド会話履歴（最新 BUTLER_HISTORY_MAX*2 = 10件）
    {"role": "user", "content": "こんにちは"},
    {"role": "assistant", "content": "..."}
  ],

  "memories": [                           // 長期記憶、最新10件、MRU
    {
      "content": "最近FPSにハマっている",
      "date": "2026-04",                  // YYYY-MM
      "category": "趣味",                  // 趣味/出来事/感情/計画 のいずれか
      "embedding": [0.01, -0.02, ...]     // gemini-embedding-001 の次元
    }
  ],

  "claims": [                             // 過去の主張リスト、最新20件、MRU
    {
      "content": "やっぱり Python が最強だと思う",
      "date": "2026-04-23T15:00:00+00:00",
      "source": "chat"                    // enrich_memories.py が付与
    }
  ]
}
```

### 非ブースター / 新規ユーザー

ほとんどのフィールドが**欠落**している（まだ生成されていない）。`users_col.find_one({_id})` は `None` を返す可能性もあり、多くの呼び出し元が `or {}` で保護している。

```json
{
  "_id": "987654321...",
  "name": "...",
  "xp": 50,
  "conv_count_nb": 5      // 30発言ごとに simple_profile 生成トリガー
}
```

### 書込箇所サマリ（main.py）

| 操作 | 行 | 説明 |
|-----|----|------|
| `$inc xp, $set` | L1774 | 通常発言時の XP 加算・連続参加 streak 更新 |
| `$inc bump_count, xp:100` | L1972, L2040 | Bump 検出時（通常・Webhook 両方） |
| `$set butler_history` | L851 | 会話履歴更新（save_butler_history） |
| `$set profile` | L910 | プロフィール抽出（extract_and_save_profile） |
| `$set simple_profile, $unset conv_count_nb` | L1214 | 非ブースター性格分析（30発言毎） |
| `$push claims` with `$slice:-20` | L579 | 主張保存 |
| `$push memories` with `$slice:-10` | L606 | 記憶保存（embedding 付き） |
| `$set title` | L2124 | /as コマンド |
| `$set xp` | L2321 | /setxp（管理者） |
| `$set butler_history: []` | L2304 | /clearmaid |
| `$inc invite_count, xp` | L2955, L2965 | 招待検出 + ボーナス |

### Vector Search インデックス

- **名前**: `memories_vector_index`
- **対象**: `users.memories.embedding`
- **作成**: MongoDB Atlas の UI で手動作成（セットアップスクリプトなし ⚠️）
- **使用**: main.py `search_memories` L650-669、メモリトリガーワード（`MEMORY_TRIGGER_WORDS`）がヒットした時のみ実行
- **フォールバック**: Vector Search が例外 / 0 件なら `_get_recent_memories` で最新 N 件を返す

### xp 降順インデックス

- **名前**: `xp_desc`
- **作成**: `setup_hook` で `create_index([("xp", -1)], background=True)` 実行（L1505）
- **用途**: `/top` コマンドのランキング表示

## コレクション: `system`

`_id` は文字列（設定名）で、複数種類のドキュメントが混在する。**ドキュメント種別で `_id` を使い分けている**。

### `_id = "personality"`

現在のサーバー全体の人格（6種のいずれか）。

```json
{"_id": "personality", "value": "yandere"}
```

- 書込: `/personality` コマンド (L2146-) / `set_server_personality` L277
- 読込: `get_server_personality` L273 → 応答・Bump メッセージ・ランクアップ・market_report すべて

### `_id = "nickname_map"`

エイリアス → 正式名のマッピング。AI 要約で自動検出 + 手動登録（`/addnick`）を merge。

```json
{
  "_id": "nickname_map",
  "map": {
    "たろー": "山田太郎",
    "はな": "花子"
  }
}
```

- 書込:
  - `batch/update_mongodb.py` L56: AI 抽出を merge、**既存値（手動登録）が優先** (`{**ai_map, **existing_map}`)
  - main.py `/addnick` L2730: 新規追加
  - main.py `/removenick` L2751: 削除（`$unset`）
- 読込: `get_nickname_map` L733 → すべてのメイド応答プロンプトに注入

### `_id = <bot_id>` (Bump Bot 毎)

6個の Bump Bot それぞれの状態。

```json
{
  "_id": "302050872383242240",        // DISBOARD
  "last_bump_at": ISODate("..."),
  "notified": false                    // 次回クールダウン通知の送信済みフラグ
}
```

- 書込: `check_bump` L1977, `check_bump_webhook` L2043
- 読込: `notification_task` L3028（10分毎に全Botをループしてクールダウン経過を検知）

### `_id = ObjectId(...)` (mimic_log)

`/mimic` 実行のログ（セキュリティ監査用）。

```json
{
  "type": "mimic_log",
  "invoker_id": "...", "invoker_name": "...",
  "target_id": "...", "target_name": "...",
  "channel_id": "...",
  "started_at": "2026-04-24T..."
}
```

- 書込: main.py L2476 のみ
- 読込: なし（運用上のログ）

## コレクション: `summaries`

2 時間毎の要約ドキュメントが永続保持される。**削除は一切ない**（`retro_date` 以外）。

```json
{
  "_id": ObjectId,
  "summary": "## 全体の雰囲気\n...\n## 主なトピック\n...",  // ## 区切りセクション
  "message_count": 150,
  "created_at": "2026-04-24T06:00:00+00:00",
  "fetched_at": "2026-04-24T06:00:00+00:00",
  // is_latest フラグは廃止。最新判定は created_at 降順ソートで行う
  // （旧 doc に残存する is_latest は未読・未書で無害）

  // 遡及日報のみ
  "retro_date": "2026-03-01",
  "is_retro": true
}
```

### インデックス

- `created_at DESC`（`background=True`, update_mongodb.py。最新要約の判定はこの降順ソートで行う。`is_latest DESC` index は廃止）

### 書込箇所

- `batch/update_mongodb.py`: 新しい要約を insert（`is_latest` は書かない。最新判定は created_at 降順）
- `batch/retro_summarize.py`: `retro_date` + `is_retro: True` を書いた遡及ドキュメントを再作成（既存があれば delete → insert。`is_latest` は書かない）

### 読込箇所

| 呼び出し元 | 目的 |
|----------|------|
| main.py `get_latest_summary` L742 | メイド応答プロンプトに注入 |
| main.py `report_cmd` L2396 | `/report` コマンド |
| main.py `retroreport_cmd` L2597 | `/retroreport` の既存チェック |
| main.py `summarystatus_cmd` L2778 | `/summarystatus` 管理者コマンド |
| batch/post_summary.py | 日報投稿（最新を created_at 降順で取得、is_retro/retro_date 除外） |
| batch/analyze_personality.py | 直近7日分の要約を性格分析に使用 |
| batch/analyze_nonbooster.py | 同上 |
| batch/enrich_memories.py | 同上 |
| batch/retro_summarize.py | 前後 CONTEXT_DAYS=2 日の要約を文脈に |
| batch/focus_summary.py | 絞り込み要約の材料 |

## コレクション: `market_predictions`

市場レポート（`market_report.py`）専用。月曜→金曜の週次サイクル。

```json
{
  "_id": ObjectId,
  "message_id": "...",              // 投票メッセージ ID（リアクション集計対象）
  "channel_id": "...",
  "week_of": "2026-04-20",           // 月曜日の日付（週識別）
  "nikkei_open": 38500.0,            // 月曜始値（基準値）
  "created_at": ISODate,
  "resolved": false                  // 金曜集計で true に
}
```

- 書込: market_report.py L382-391（月曜の投票送信直後）、L440-445（金曜の集計時に resolved=true）
- 読込: L403-406（金曜の集計で最新未解決予想を取得）

## データの流れ（横断ビュー）

```
Discord サーバーのチャット
    │
    ├─ on_message ──▶ users.$inc(xp)          [main.py, 即時]
    │                 users.$inc(bump_count)
    │                 users.butler_history    (メイド会話時のみ)
    │                 users.memories          (重要発言のみ, embedded)
    │                 users.claims            (主張パターン検出時)
    │
    └─ バッチ (2h毎)
        fetch → summarize → update_mongodb
            ├─▶ summaries.insert              [新要約、最新は created_at 降順で判定]
            └─▶ system.nickname_map           (merge, 既存優先)

       バッチ (日次 0:00)
        analyze_personality
            └─▶ users.profile                 (ブースター)
        analyze_nonbooster
            └─▶ users.simple_profile          (非ブースター)
        enrich_memories
            ├─▶ users.claims                  (要約から主張抽出)
            └─▶ users.memories (+embedding)

       market_report (平日16時)
        └─▶ market_predictions (月曜のみ)
            users.$inc(xp) (金曜集計時、正解者+50)
```

## 注意すべきデータの不変条件（invariants）

1. **最新要約は `created_at` 降順ソートで選択する**
   - 読み手（main.py `get_latest_summary` / `summarystatus_cmd`、batch/post_summary.py `fetch_latest_summary`）は `find_one({"summary": {"$exists": True}, "is_retro": {"$ne": True}, "retro_date": {"$exists": False}}, sort=[("created_at", -1)])` で取得
   - 遡及（retro）要約は `is_retro`/`retro_date` で除外される
   - `is_latest` フラグは廃止（known-issue #8 を 2026-06-06 に解消）。旧フラグ方式の「複数件 True が残る」リスクは消滅した

2. **`users.memories` の embedding 次元数は固定**
   - `gemini-embedding-001` のデフォルト次元（本プロジェクトでは 768 と推定）
   - モデル変更時は全 memories の embedding 再計算が必要
   - Atlas Vector Search index も再作成

3. **`users._id` は文字列型**
   - `str(user.id)` で常に文字列化されている
   - バッチ側でも同じ。intで検索すると絶対に hit しない

4. **`users.butler_history` の長さは 10 以下**
   - `save_butler_history` L849 で切り詰め
   - `butler_history[-10:]` で保持

5. **`users.claims` は 20 件, `users.memories` は 10 件**
   - `$slice: -20` / `-10` で自動切り詰め
   - 古いものから削除

6. **`system.nickname_map.map` のキーに特殊文字は入れない**
   - MongoDB のドット記法で `map.{key}` としてアクセスするため、キーに `.` があると壊れる

## グッドプラクティス

### 安全な読み取りパターン

```python
doc = await users_col.find_one({"_id": uid}) or {}
profile = doc.get("profile", {})
hobbies = profile.get("hobbies", [])
```

全ての呼び出し元で `or {}` / `.get(..., default)` を使う。**KeyError を投げない**。

### 安全な書き込みパターン

```python
await users_col.update_one(
    {"_id": uid},
    {"$set": {...}, "$inc": {...}},
    upsert=True          # ← 必須、初出ユーザー対応
)
```

### 書き込みが複雑な操作は `find_one_and_update`

`return_document=True` で更新後のドキュメントを取得し、1 ラウンドトリップで済ませる（L1774, L1972）。
