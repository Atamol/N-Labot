from dotenv import load_dotenv
load_dotenv()

import os
import discord
from discord.ext import tasks
from discord import app_commands

import switchbot
import gmail_detector

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TEMP_CHANNEL_ID = int(os.getenv("TEMP_CHANNEL_ID", "0"))
GMAIL_CHANNEL_ID = int(os.getenv("GMAIL_CHANNEL_ID", "0"))
THRESHOLD_TEMP = 5.0

intents = discord.Intents.default()
intents.message_content = True

class DiscordBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.temp_state = None
        self.check_temperature_task = tasks.loop(minutes=3)(self.check_temperature)

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")

        # スラッシュコマンド同期
        synced = await tree.sync()
        print(f"Synced {len(synced)} commands globally.")

        # 温湿度チェックの定期タスク開始
        self.check_temperature_task.start()

        # gmail_detector.pyの起動
        gmail_detector.start_gmail_detector(self, GMAIL_CHANNEL_ID)

    # SwitchBotで温湿度を取得し，5℃を下回ったら通知
    async def check_temperature(self):
        meter_data = switchbot.get_meter_status()
        if not meter_data:
            return

        temp = meter_data.get("temperature")
        if not isinstance(temp, (int, float)):
            print("Invalid temperature:", temp)
            return

        new_state = "BELOW" if temp <= THRESHOLD_TEMP else "ABOVE"
        if self.temp_state is None:
            self.temp_state = new_state
            print(f"初回チェック: temp_state={self.temp_state}, temp={temp}")
            return

        if new_state != self.temp_state:
            channel = self.get_channel(TEMP_CHANNEL_ID)
            if channel:
                if new_state == "BELOW":
                    msg = f"⚠️ 現在の温度は{temp}℃です。"
                else:
                    msg = f"現在の温度は{temp}℃です。"
                await channel.send(msg)
            self.temp_state = new_state


# スラッシュコマンド
bot = DiscordBot(intents=intents)
tree = app_commands.CommandTree(bot)

@tree.command(name="status", description="現在の温湿度とバッテリーを表示")
async def meterstatus_command(interaction: discord.Interaction):
    meter_data = switchbot.get_meter_status()
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