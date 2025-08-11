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
UID_FILE = os.getenv("GMAIL_UID_FILE", "last_uid.txt")


def load_last_uid():
    #起動時に前回UIDを復元（なければ0）
    global LAST_PROCESSED_UID
    try:
        with open(UID_FILE, "r") as f:
            LAST_PROCESSED_UID = int(f.read().strip())
            print(f"[IMAP] restored LAST_PROCESSED_UID={LAST_PROCESSED_UID}")
    except Exception:
        LAST_PROCESSED_UID = 0


def save_last_uid():
    # 処理後に最新UIDを保存
    try:
        with open(UID_FILE, "w") as f:
            f.write(str(LAST_PROCESSED_UID))
    except Exception as e:
        print(f"[IMAP] save uid failed: {e}")


def start_gmail_detector(discord_bot: discord.Client, gmail_channel_id: int):
    # 起動時に前回UIDを復元
    load_last_uid()
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
        except Exception as e:
            print(f"[ERROR] idle_loop: {e}")

        time.sleep(1)

def fetch_latest_and_notify(server: IMAPClient, discord_bot: discord.Client, gmail_channel_id: int):
    global LAST_PROCESSED_UID
    # 前回UID+1から最新までの差分だけ取得
    uids = server.search(['UID', f'{LAST_PROCESSED_UID + 1}:*'])
    if not uids:
        return

    uids.sort()
    # 最新10件に制限
    new_uids = uids[-10:]

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
                print(f"[IMAP] Found code: {code}. Sending to Discord.")
                discord_bot.loop.call_soon_threadsafe(
                    asyncio.create_task,
                    send_discord_message(discord_bot, gmail_channel_id, code)
                )
                break

    # 最大UIDを記録・保存
    LAST_PROCESSED_UID = max(new_uids)
    save_last_uid()

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