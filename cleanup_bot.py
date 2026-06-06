import discord
import os
import asyncio

# --- 設定 ---
TOKEN = os.environ.get("CLEANUP_BOT_TOKEN")
# 自己紹介チャンネルのIDを環境変数に入れるか、直接ここに数値で入力してください
INTRO_CHANNEL_ID = int(os.environ.get("INTRO_CHANNEL_ID", 0))

intents = discord.Intents.default()
intents.members = True  # メンバーリスト取得に必須
intents.message_content = True 

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    
    if INTRO_CHANNEL_ID == 0:
        print("❌ INTRO_CHANNEL_ID が設定されていません。終了します。")
        await client.close()
        return

    intro_channel = client.get_channel(INTRO_CHANNEL_ID)
    if not intro_channel:
        print(f"❌ チャンネル ID {INTRO_CHANNEL_ID} が見つかりませんでした。")
        await client.close()
        return

    guild = intro_channel.guild
    print(f"🔍 サーバー: {guild.name} 内をスキャン中...")

    # 現在のサーバーメンバーのIDをセット（高速照合用）
    # guild.members を取得するには、Developer PortalでSERVER MEMBERS INTENTの有効化が必要です
    current_member_ids = {m.id for m in guild.members}
    
    deleted_count = 0
    
    try:
        # 自己紹介チャンネルの過去ログを取得（最新100〜200件程度で十分であればlimitを調整）
        async for message in intro_channel.history(limit=300):
            # ボットのメッセージは無視
            if message.author.bot:
                continue

            # 投稿者が既にサーバーにいない場合
            if message.author.id not in current_member_ids:
                try:
                    print(f"🗑️ 削除中: {message.author.display_name} (ID: {message.author.id})")
                    await message.delete()
                    deleted_count += 1
                    # 短時間に大量削除すると制限にかかるため、少し待機
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"⚠️ 削除エラー: {e}")

    except Exception as e:
        print(f"❌ ヒストリー取得エラー: {e}")

    print(f"✨ 掃除完了！ 合計 {deleted_count} 件のメッセージを削除しました。")
    await client.close()

if __name__ == "__main__":
    client.run(TOKEN)
