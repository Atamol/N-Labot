import os
import time
import uuid
import base64
import hmac
import hashlib
import requests

SWITCHBOT_TOKEN = os.getenv("SWITCHBOT_TOKEN")
SWITCHBOT_SECRET = os.getenv("SWITCHBOT_SECRET")
SWITCHBOT_DEVICE_ID = os.getenv("SWITCHBOT_DEVICE_ID")

# 署名付きヘッダを作成
def make_auth_headers(token: str, secret: str):
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

# ステータスの取得
def get_meter_status():
    if not (SWITCHBOT_TOKEN and SWITCHBOT_SECRET and SWITCHBOT_DEVICE_ID):
        print("SwitchBot関連の環境変数が設定されていません。")
        return {}

    headers = make_auth_headers(SWITCHBOT_TOKEN, SWITCHBOT_SECRET)
    url = f"https://api.switch-bot.com/v1.1/devices/{SWITCHBOT_DEVICE_ID}/status"
    res = requests.get(url, headers=headers)
    data = res.json()

    if data.get("statusCode") == 100:
        return data["body"]
    else:
        print("SwitchBot API Error:", data)
        return {}