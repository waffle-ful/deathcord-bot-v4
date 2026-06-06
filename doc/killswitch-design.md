# キルスイッチ（緊急遮断）設計仕様 v1

> オーナーが手動で発火する「ブレークグラス」型の緊急遮断。暴走 bot（Wick 等）の即時無力化と、
> 乗っ取り・誤操作からの復旧を、**可逆・監査可能・オーナー専用**で行う。
> 自動発火は **一切なし**（known-issues #6: 自動発火ループで BAN を食らった前科があるため）。

## 脅威モデルと前提

- **守る相手**: ①暴走した bot（特に anti-nuke の Wick）②権限を持つ副管理人アカウントの乗っ取り。
- **発火者**: **オーナー1人のみ**（env `OWNER_ID`）。`administrator` 権限では判定しない
  — 脅威モデルに「admin を持つ副管理人の乗っ取り」が入るため、admin 権限保有者を信用できない。
- **階層が全セキュリティの土台**: 空気くんのロールが全対象（Wick・Probot・副管理人ロール）より
  **上位**であること。これが乗っ取られた admin に空気くんを kick/降格させない盾であり、
  相手のロールを編集できる条件。**この前提は実行時に検証し、満たさなければ生の 403 ではなく
  明確な理由で拒否する**。

## 新規 env 変数（すべて deny-by-default）

| 変数 | 型 | 既定 | 意味 |
|------|----|------|------|
| `OWNER_ID` | int | `0` | 発火を許す唯一の Discord ユーザー ID。**`0` の間は全 panic コマンド/エンドポイントを拒否** |
| `PANIC_TOKEN` | str | `""` | 外部エンドポイントの共有シークレット。**空の間は `/panic` エンドポイントを無効（503）** |
| `PANIC_LOG_CHANNEL_ID` | int | `0` | 監査ログ投稿先チャンネル（任意）。0 なら print のみ |

`main.py` 冒頭の設定ブロック（L32 付近、`TOKEN = ...` の並び）に追記する。

## 新規 Mongo コレクション

`main.py` の DB 定義（L1457-1462 付近）に追加:
```python
killswitch_col = db["killswitch_snapshots"]
```

### スナップショット doc 形状
```
{
  "_id": ObjectId,                       # = snapshot_id
  "target_id": int,                      # 対象 bot のユーザーID
  "target_name": str,
  "guild_id": int,
  "mode": "strip" | "kick" | "ban",
  "actor": str,                          # "slash:<user_id>" / "endpoint:<ip>"
  "created_at": str,                     # UTC isoformat
  "status": "applied" | "failed",
  "restored": bool,                      # 既定 False
  "restored_at": str | None,
  "entries": [                           # 復旧に必要な原状
    {"role_id": int, "role_name": str, "type": "managed", "perms": int},   # 権限ゼロ化した managed ロール（perms=元の権限整数）
    {"role_id": int, "role_name": str, "type": "membership"}               # bot から外した非 managed ロール
  ],
  "warnings": [str]                      # @everyone に危険権限、など per-bot で直せない事項
}
```

## コア処理（共有ヘルパ。コマンドと外部口の両方が呼ぶ）

### `async def _panic_neutralize_bot(guild, target, mode, actor) -> dict`

`target` は `discord.Member`（対象 bot）。戻り値は構造化 dict。**例外を外に投げない**
（try/except で全体を包み、エラーも dict で返す）— health server / bot を絶対に落とさないため。

ガード（いずれか該当で即 refuse・何も変更しない）:
1. `target.bot` が False → 「v1 は bot のみ対象（メンバー隔離は v2）」で拒否。
2. `target.id == guild.me.id`（空気くん自身）または `target.id == OWNER_ID` → 拒否。
3. **階層前提検証**: `strip` で編集/削除する各ロールについて `guild.me.top_role.position > role.position`
   を確認。**managed ロール（危険権限の在処）が編集不可なら critical**:
   「空気くんを `<role_name>` より上に移動してください」と明示して拒否し、**部分無力化はしない**
   （半端に効くと「効いたつもりで Wick が生きてる」最悪パターンになる）。

`mode == "strip"`（既定・**可逆**）:
- `target.roles`（`@everyone` を除く）を走査して entries を構築:
  - `role.managed` の場合: entry `{type:"managed", perms: role.permissions.value}` を記録し、
    **`await role.edit(permissions=discord.Permissions.none(), reason=...)`** で権限をゼロ化。
    ※ managed ロールは「メンバーシップ削除」は不可だが「権限の編集」は階層が上なら可能。
  - 非 managed の場合: entry `{type:"membership"}` を記録し、bot から外す（**bulk**:
    対象を集めて `await target.remove_roles(*non_managed_roles, reason=...)` の1コール）。
    ※ メンバーシップ除去は他の保持者に影響しない（権限編集と違い巻き添えなし）。
- `@everyone` が危険権限（administrator/ban/kick/manage_guild/manage_roles 等）を持つ場合は
  per-bot で直せないため `warnings` に記録（変更はしない）。

`mode == "kick"`: entries を記録（復旧記録用）後 `await target.kick(reason=...)`。
`mode == "ban"`: entries を記録後 `await guild.ban(target, reason=..., delete_message_seconds=0)`。

順序（復旧の正しさのため原状を先に確定）:
1. 現在状態から entries/warnings を構築。
2. **Mongo に snapshot doc を insert（status 仮 "applied"）** ← restore の真実の源。破壊操作より前。
3. 破壊操作（権限ゼロ化 / 除去 / kick / ban）を実行。失敗は per-action で捕捉し記録。
4. snapshot doc を結果で update（成否・実際に触れたロール）。
5. **best-effort で監査ログ**（`_panic_audit`、失敗してもキルを止めない）。

戻り値 dict（例）:
```
{ "ok": bool, "mode": str, "target": {"id","name"}, "snapshot_id": str,
  "actions": [ "zeroed perms on <role>", "removed <role>", ... ],
  "unactionable": [ "<role>: 空気くんより上位" ],
  "warnings": [...],
  "reversibility": "strip=完全可逆 / kick=再招待は手動(bot不可) / ban=unbanのみ" }
```

### `async def _panic_restore(guild, target_id=None, snapshot_id=None) -> dict`

- snapshot_id 指定があればそれを、無ければ `target_id` の **最新の applied かつ restored=False** を取得。
- **二重 restore 拒否**（restored=True なら「既に復旧済み」を返す）。
- entries を復元:
  - `type=="managed"`: `await guild.get_role(role_id).edit(permissions=discord.Permissions(perms))` で原状回復。
  - `type=="membership"`: 対象 bot を REST で取得し（`await guild.fetch_member(target_id)`）、ロールを再付与。
  - mode が kick/ban の場合は honest に報告（kick=bot 再招待は OAuth で bot 不可、ban=unban のみ実施可）。
- `restored=True`, `restored_at` を更新。構造化 dict を返す。

### `async def _panic_check(guild) -> dict`

ギルド内の各 bot（`m.bot == True` のメンバー）について、`guild.me` が上位か・managed ロールが
編集可能かを報告。`/panic_check` と外部 `check` アクションが使う。`guild.me.top_role` と
各 bot の `top_role` / managed ロール位置を比較。

### `async def _panic_audit(guild, text)`（best-effort）

`print(f"[PANIC] {text}")` を必ず実行。`PANIC_LOG_CHANNEL_ID` があれば try/except で embed 投稿。
**失敗してもキル処理を中断しない**（混乱時に最も失敗しやすいのが ch 投稿）。

## スラッシュコマンド（すべて `OWNER_ID` 照合 — admin 権限では判定しない）

各ハンドラ先頭で共通ゲート:
```python
if not await check_home_guild(interaction): return
if OWNER_ID == 0 or interaction.user.id != OWNER_ID:
    await interaction.response.send_message("⛔ このコマンドはオーナー専用です。", ephemeral=True)
    return
```
加えて UI 上の隠蔽として `@app_commands.default_permissions(administrator=True)` を付与
（防御の多層化。ただし**実セキュリティは上記 OWNER_ID 照合**）。

- **`/panic_check`** — 階層 readiness レポートを ephemeral embed で表示。
- **`/panic_bot bot:<Member> mode:<strip|kick|ban>`** — `mode` は `app_commands.Choice`。
  実行前に**確認ボタン**（ephemeral・60秒タイムアウト・対象と mode を明記）→ 押下で
  `_panic_neutralize_bot` を実行し結果を ephemeral で返す。`bot` 引数が bot でなければ早期拒否。
- **`/panic_restore` `[target:<Member>]`** — target 省略時は直近の未復旧スナップショットを対象。
  `_panic_restore` を呼び結果を返す。

すべて `interaction.guild` を使用。`@client.tree.command(...)` でインポート時登録（既存の慣習どおり、
`client` を再生成しない）。

## 外部エンドポイント（既存 aiohttp health server に追加）

`start_web_server()`（L20-29）の `app.router.add_get("/", _health_handler)` の後に追加:
```python
app.router.add_post("/panic", _panic_web_handler)
```

### `async def _panic_web_handler(request) -> web.Response`

**必ず web.Response を返す**（例外で health server を落とさない）。手順:
1. `PANIC_TOKEN` が空 → `web.json_response({"error":"disabled"}, status=503)`。
2. ヘッダ `X-Panic-Token` を取り出し、**`hmac.compare_digest`（定数時間比較）**で `PANIC_TOKEN` と照合。
   不一致 → `print` で送信元IP付き警告ログ → `web.json_response({"error":"forbidden"}, status=403)`。
3. **簡易レート制限**: モジュール内 in-memory で「直近60秒の試行回数」を数え、閾値（例 10）超で 429。
   公開 `.onrender.com` URL 上にあるためブルートフォース対策。
4. JSON body をパース: `{"action": "neutralize"|"restore"|"check", "bot_id": int, "mode": "strip"|"kick"|"ban"}`。
5. **ギルド取得は gateway キャッシュに依存しない**（動機が「全 bot 停止」のため空気くんの gateway が
   未接続でも動くべき）: `guild = client.get_guild(HOME_GUILD_ID) or await client.fetch_guild(HOME_GUILD_ID)`。
   メンバー取得は `await guild.fetch_member(bot_id)`（REST）。
   ※ 空気くんが完全オフライン（未ログイン）なら何もできない — これは v1 の許容限界として doc 記載。
6. action に応じてコア共有ヘルパを呼び、結果 dict を `web.json_response(result)` で返す。
7. 全体を try/except で包み、想定外は `web.json_response({"error": str(e)}, status=500)`。

`import hmac` をファイル先頭の import 群に追加（標準ライブラリ）。

## 安全策チェックリスト（実装が必ず満たすこと）

- [ ] **自動発火経路ゼロ**: どのイベントハンドラ（on_message 等）からも panic 系を呼ばない。
- [ ] OWNER_ID==0 / PANIC_TOKEN=="" は **deny**（fail-safe）。
- [ ] 空気くん自身・OWNER を対象にできない。
- [ ] 非 bot を対象にできない（v1）。
- [ ] managed ロールが編集不可（階層が下）なら**何もせず明確に拒否**（部分無力化しない）。
- [ ] Mongo snapshot は破壊操作の**前**に書く。
- [ ] 監査 ch 投稿失敗はキルを止めない。
- [ ] 外部 handler は必ず Response を返す（health server を落とさない）。
- [ ] token 比較は `hmac.compare_digest`。
- [ ] 結果に可逆性を正直に明記。
- [ ] `python -m py_compile main.py` が通る。

## 運用上の注意

- **`HOME_GUILD_ID` はハードコード fallback を持つ**（env 未設定でも本番ギルドID に解決される）。
  したがって `OWNER_ID` と `PANIC_TOKEN` を設定すると、外部 `/panic` は env の `HOME_GUILD_ID`
  有無に関わらず**本番ギルドに作用する**。token は十分長いランダム値にすること。
- 外部口は公開 `.onrender.com` URL 上にある。守りは「`hmac.compare_digest` ＋ 高エントロピー token
  ＋ 送信元IP単位の失敗レート制限（token 照合前）」の多層。token は秘匿し漏洩時は即ローテーション。
- 空気くんが**完全オフライン（未ログイン）**だと外部口でもギルド操作はできない（v1 の許容限界）。
- **strip はロール由来の権限のみ無効化する**。チャンネル個別の権限上書き（channel overwrite）で
  権限を得ている bot はそれを保持し得る。Wick の anti-nuke 権限（ban/kick/manage_guild 等）は
  ギルド全体ロール由来なので strip で無効化できるが、**完全除去が要るなら kick/ban を使う**。
  この点は strip 実行結果の警告にも表示される。
- **要確認（reliance gate）**: 「managed ロールの権限を `Permissions.none()` に edit できる」ことが
  strip の全前提。Discord が managed ロールの権限編集を許すかは**捨て垢 bot で実地検証してから**
  本番の Wick に strip を撃つこと（membership 変更は不可だが権限編集は可、というのが設計の賭け）。

## v2 以降（今回スコープ外）

全 bot 一括ロックダウン / メンバー隔離 / 確認の2要素化 / レート制限の永続化。
