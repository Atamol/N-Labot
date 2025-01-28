import os
import re
import time
import threading
import asyncio

import email
from email.header import decode_header

from imapclient import IMAPClient
from bs4 import BeautifulSoup

import discord

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_PASS")

TARGET_SUBJECT = "Bambu Lab Verification Code"
CODE_REGEX = re.compile(r"Your\s+verification\s+code\s+is:\s*(\d{6})", re.IGNORECASE)

def start_gmail_detector(discord_bot: discord.Client, gmail_channel_id: int):
    th = threading.Thread(
        target=idle_loop,
        args=(discord_bot, gmail_channel_id),
        daemon=True
    )
    th.start()

# サブスレッド上でIMAP IDLEを起動
def idle_loop(discord_bot: discord.Client, gmail_channel_id: int):
    while True:
        try:
            with IMAPClient("imap.gmail.com", ssl=True, use_uid=True) as server:
                server.login(GMAIL_USER, GMAIL_PASS)

                folders = server.list_folders()
                all_mail_folder = None
                for f in folders:
                    folder_name = f[2]
                    if "All Mail" in folder_name or "すべてのメール" in folder_name:
                        all_mail_folder = folder_name
                target_folder = all_mail_folder if all_mail_folder else "INBOX"
                server.select_folder(target_folder)
                print(f"[IMAP] IDLE start in folder: {target_folder}")

                # IDLE開始 （最長1分待機）
                server.idle()
                responses = server.idle_check(timeout=60)
                server.idle_done()

                # 新着の検知
                if responses:
                    fetch_latest_and_notify(server, discord_bot, gmail_channel_id)
                    print(f"[IMAP] mailbox update: {responses}")
        except Exception as e:
            print(f"[ERROR] idle_loop: {e}")

        time.sleep(1)

# 最新の10通を取得し，条件が合致するものから認証コードを抽出してDiscordへ送信
def fetch_latest_and_notify(server: IMAPClient, discord_bot: discord.Client, gmail_channel_id: int):
    all_uids = server.search(['ALL'])
    if not all_uids:
        return

    all_uids.sort()
    last10 = all_uids[-10:]
    data = server.fetch(last10, ['BODY[]'])
    if not data:
        return

    # 新しい順にチェック
    for uid in reversed(last10):
        msg_info = data.get(uid)
        if not msg_info or (b'BODY[]' not in msg_info):
            continue

        raw_email = msg_info[b'BODY[]']
        msg = email.message_from_bytes(raw_email)
        subject = decode_str(msg.get("Subject",""))

        if TARGET_SUBJECT in subject:
            body_text = get_body_text(msg)
            code = extract_code(body_text)
            if code:
                print(f"[IMAP] Found code: {code}. Sending to Discord.")
                discord_bot.loop.call_soon_threadsafe(
                    asyncio.create_task,
                    send_discord_message(discord_bot, gmail_channel_id, code)
                )
                break

async def send_discord_message(discord_bot: discord.Client, channel_id: int, code: str):
    channel = discord_bot.get_channel(channel_id)
    if channel:
        await channel.send(f"Bambu Lab Verification Code: {code}")

# メールヘッダのデコード
def decode_str(s):
    parts = decode_header(s)
    decoded = []
    for text, enc in parts:
        if isinstance(text, bytes):
            if enc:
                decoded.append(text.decode(enc, errors="ignore"))
            else:
                decoded.append(text.decode("utf-8", errors="ignore"))
        else:
            decoded.append(text)
    return "".join(decoded)

# メール本文を結合
def get_body_text(msg: email.message.Message):
    texts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ["text/plain", "text/html"]:
                payload = part.get_payload(decode=True)
                if payload:
                    texts.append(payload.decode(errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            texts.append(payload.decode(errors="replace"))
    return "\n\n".join(texts)

# HTMLをテキスト化
def extract_code(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text()
    match = CODE_REGEX.search(text)
    if match:
        return match.group(1)
    return None