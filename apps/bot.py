import os
import discord
from discord.ext import tasks
from discord import app_commands
import asyncio
import gmail_detector
# from reservation import init_reservations

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TEMP_CHANNEL_ID = int(os.getenv("TEMP_CHANNEL_ID", "0"))
GMAIL_CHANNEL_ID = int(os.getenv("GMAIL_CHANNEL_ID", "0"))
TEST_CHANNEL_ID = int(os.getenv("TEST_CHANNEL_ID", "0"))

THRESHOLD_TEMP = 5.0
DISABLE_SWITCHBOT = os.getenv("DISABLE_SWITCHBOT", "0") == "1"

# SwitchBotを使うときだけimport
if not DISABLE_SWITCHBOT:
    import switchbot

intents = discord.Intents.default()
intents.message_content = True

class DiscordBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tree = app_commands.CommandTree(self)

        # 温湿度チェックタスク（SwitchBot無効時は登録しない）
        if not DISABLE_SWITCHBOT:
            self.check_temperature_task = tasks.loop(minutes=3)(self.check_temperature)

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")

        # Gmail検出は常時開始
        gmail_detector.start_gmail_detector(self, GMAIL_CHANNEL_ID)

        # SwitchBotが有効なときだけ温度監視タスクを開始
        if not DISABLE_SWITCHBOT:
            self.check_temperature_task.start()

        # 予約機能を使う場合はコメント解除
        # await init_reservations(self)

        # スラッシュコマンド同期
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} commands globally.")

        # テスト用チャンネルへの通知
        channel_test = self.get_channel(TEST_CHANNEL_ID)
        if channel_test:
            await channel_test.send(
                "再起動しました。"
            )
        else:
            print("指定したチャンネルが見つかりませんでした。")

    async def check_temperature(self):
        if DISABLE_SWITCHBOT:
            return

        # switchbot をローカル参照（将来の切替に強い）
        from switchbot import get_meter_status

        meter_data = get_meter_status()
        if not meter_data:
            return

        temp = meter_data.get("temperature")
        if not isinstance(temp, (int, float)):
            print("Invalid temperature:", temp)
            return

        new_state = "BELOW" if temp <= THRESHOLD_TEMP else "ABOVE"

        if getattr(self, "temp_state", None) is None:
            self.temp_state = new_state
            print(f"Switchbot動作チェック: temp_state={self.temp_state}, temp={temp}")
            return

        if new_state != self.temp_state:
            channel = self.get_channel(TEMP_CHANNEL_ID)
            if channel:
                msg = (
                    f"⚠️現在の温度は{temp}℃です。"
                    if new_state == "BELOW"
                    else f"現在の温度は{temp}℃です。"
                )
                await channel.send(msg)
            self.temp_state = new_state

bot = DiscordBot(intents=intents)

# SwitchBot無効時はスラッシュコマンド自体を登録しない
if not DISABLE_SWITCHBOT:
    @bot.tree.command(name="status", description="現在の温湿度とバッテリーを表示")
    async def meterstatus_command(interaction: discord.Interaction):
        from switchbot import get_meter_status

        meter_data = get_meter_status()
        if not meter_data:
            await interaction.response.send_message("温湿度計の取得に失敗しました。")
            return

        temp = meter_data.get("temperature")
        humi = meter_data.get("humidity")
        battery = meter_data.get("battery")
        msg = f"温度: {temp}℃\n湿度: {humi}%\nバッテリー: {battery}%"
        await interaction.response.send_message(msg)

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("BOT_TOKEN not set.")
    else:
        bot.run(BOT_TOKEN)
