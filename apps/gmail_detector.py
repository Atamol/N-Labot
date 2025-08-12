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

# 前回処理済みのUIDを保持
LAST_PROCESSED_UID = 0

# 起動時に最新のメールUIDを取得し，LAST_PROCESSED_UIDを初期化
def initialize_last_uid():
    global LAST_PROCESSED_UID
    try:
        with IMAPClient("imap.gmail.com", ssl=True, use_uid=True) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            target_folder = "[Gmail]/すべてのメール"
            server.select_folder(target_folder)
            
            # 存在する最新のメールUIDを取得
            all_uids = server.search(['ALL'])
            if all_uids:
                LAST_PROCESSED_UID = max(all_uids)
            else:
                pass

    except Exception:
        pass

def start_gmail_detector(discord_bot: discord.Client, gmail_channel_id: int):
    # ループ前にUIDを初期化
    initialize_last_uid()
    
    th = threading.Thread(
        target=idle_loop,
        args=(discord_bot, gmail_channel_id),
        daemon=True
    )
    th.start()

def idle_loop(discord_bot: discord.Client, gmail_channel_id: int):
    global LAST_PROCESSED_UID
    while True:
        try:
            with IMAPClient("imap.gmail.com", ssl=True, use_uid=True) as server:
                # ログイン
                server.login(GMAIL_USER, GMAIL_PASS)

                # フォルダ選択
                target_folder = "[Gmail]/すべてのメール"
                server.select_folder(target_folder)

                # 新着メールの検出と処理
                fetch_latest_and_notify(server, discord_bot, gmail_channel_id)
        except Exception:
            pass

        time.sleep(3)

def fetch_latest_and_notify(server: IMAPClient, discord_bot: discord.Client, gmail_channel_id: int):
    global LAST_PROCESSED_UID
    all_uids = server.search(['ALL'])
    if not all_uids:
        return

    all_uids.sort()

    # 前回処理済みのUIDをローカル変数に保持
    current_last_uid = LAST_PROCESSED_UID
    
    new_uids = [uid for uid in all_uids if uid > current_last_uid]
    if not new_uids:
        return

    # 通知処理の前に最新のUIDをグローバル変数に記録する
    LAST_PROCESSED_UID = max(new_uids)

    # 最新10件のUIDを取得
    if len(new_uids) > 10:
        new_uids = new_uids[-10:]

    # 新しい順に処理
    for uid in reversed(new_uids):
        msg_info = server.fetch(uid, ['BODY[]']).get(uid)
        if not msg_info or (b'BODY[]' not in msg_info):
            continue

        raw_email = msg_info[b'BODY[]']
        msg = email.message_from_bytes(raw_email)

        subject = decode_str(msg.get("Subject", ""))
        if TARGET_SUBJECT in subject:
            body_text = get_body_text(msg)
            code = extract_code(body_text)
            if code:
                discord_bot.loop.call_soon_threadsafe(
                    asyncio.create_task,
                    send_discord_message(discord_bot, gmail_channel_id, code)
                )
                break

    # 最新のUIDを記録
    LAST_PROCESSED_UID = max(new_uids)

async def send_discord_message(discord_bot: discord.Client, channel_id: int, code: str):
    channel = discord_bot.get_channel(channel_id)
    if channel:
        await channel.send(f"Bambu Lab Verification Code: **{code}**")

def decode_str(s: str) -> str:
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

def get_body_text(msg: email.message.Message) -> str:
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

def extract_code(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text()
    match = CODE_REGEX.search(text)
    if match:
        return match.group(1)
    return ""