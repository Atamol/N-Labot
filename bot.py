import os
import time
import uuid
import base64
import hmac
import hashlib
import requests

import discord
from discord.ext import tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
SWITCHBOT_TOKEN = os.getenv("SWITCHBOT_TOKEN")
SWITCHBOT_SECRET = os.getenv("SWITCHBOT_SECRET")
SWITCHBOT_DEVICE_ID = os.getenv("SWITCHBOT_DEVICE_ID")

# GUILD_ID = int(os.getenv("GUILD_ID", 0))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))

THRESHOLD_TEMP = 3.0

intents = discord.Intents.default()
intents.message_content = True

class DiscordBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tree = app_commands.CommandTree(self)
        self.temp_state = None

        self.check_temperature_task = tasks.loop(minutes=3)(self.check_temperature)

    # SwitchBot API呼び出し
    def make_auth_headers(self, token: str, secret: str):

        # SwitchBot API v1.1 署名付きヘッダを作成
        t = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        string_to_sign = token + t + nonce
        sign = base64.b64encode(
            hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha256
            ).digest()
        ).decode("utf-8")

        return {
            "Authorization": f"Bearer {token}",
            "sign": sign,
            "t": t,
            "nonce": nonce,
            "Content-Type": "application/json"
        }

    # SwitchBotの温湿度計ステータスを取得
    def get_meter_status(self) -> dict:
        headers = self.make_auth_headers(SWITCHBOT_TOKEN, SWITCHBOT_SECRET)
        url = f"https://api.switch-bot.com/v1.1/devices/{SWITCHBOT_DEVICE_ID}/status"
        res = requests.get(url, headers=headers)
        data = res.json()

        if data.get("statusCode") == 100:
            return data["body"]
        else:
            print("SwitchBot API Error:", data)
            return {}

    # 定期タスク: 温度をチェック
    async def check_temperature(self):
        meter_data = self.get_meter_status()
        if not meter_data:
            return

        temp = meter_data.get("temperature")
        if not isinstance(temp, (int, float)):
            print("Temperature is not a number:", temp)
            return

        new_state = "BELOW" if temp <= THRESHOLD_TEMP else "ABOVE"

        if self.temp_state is None:
            self.temp_state = new_state
            print(f"初回チェック: temp_state={self.temp_state}, temp={temp}")
            return

        # 状態が変わったら通知
        if new_state != self.temp_state:
            channel = self.get_channel(CHANNEL_ID)
            if channel:
                if new_state == "BELOW":
                    msg = f"⚠️ 現在の温度は{temp}℃です。3℃を下回りました。"
                else:
                    msg = f"現在の温度は{temp}℃です。3℃を上回りました。"

                await channel.send(msg)
            self.temp_state = new_state

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        
        # コマンドの同期
        synced_cmds = await self.tree.sync()
        print(f"Synced {len(synced_cmds)} commands globally")

        # 定期タスク開始
        self.check_temperature_task.start()

bot = DiscordBot(intents=intents)

# 現在の温湿度とバッテリーを取得して表示
@bot.tree.command(name="status", description="現在の温湿度とバッテリーを表示")
async def meterstatus_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    meter_data = bot.get_meter_status()
    if not meter_data:
        await interaction.followup.send("温湿度計の取得に失敗しました。")
        return

    temp = meter_data.get("temperature")
    humi = meter_data.get("humidity")
    battery = meter_data.get("battery")

    message = f"温度: {temp}℃\n湿度: {humi}%\nバッテリー: {battery}%"
    await interaction.followup.send(message)

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Error: DISCORD_BOT_TOKEN is not set in .env.")
    else:
        bot.run(BOT_TOKEN)
