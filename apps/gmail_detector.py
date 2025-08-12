import os
import re
import time
import threading
import asyncio
import email
from email.header import decode_header
from imapclient import IMAPClient, exceptions
from bs4 import BeautifulSoup
import discord

# --- 環境変数と定数 ---
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_PASS")
TARGET_SUBJECT = "Bambu Lab Verification Code"
CODE_REGEX = re.compile(r"Your\s+verification\s+code\s+is:\s*(\d{6})", re.IGNORECASE)
UID_FILE = "last_processed_uid.txt"
LAST_PROCESSED_UID = 0

# --- UIDとDiscord関連の関数 (変更なし) ---
def load_last_uid():
    global LAST_PROCESSED_UID
    if os.path.exists(UID_FILE):
        try:
            with open(UID_FILE, "r") as f:
                content = f.read().strip()
                if content.isdigit():
                    LAST_PROCESSED_UID = int(content)
                    print(f"[INFO] Loaded LAST_PROCESSED_UID: {LAST_PROCESSED_UID}")
                    return
        except Exception as e:
            print(f"[ERROR] Failed to load UID from file '{UID_FILE}': {e}")
    print("[INFO] UID file not found or invalid. LAST_PROCESSED_UID is set to 0.")
    LAST_PROCESSED_UID = 0

def save_last_uid(uid: int):
    global LAST_PROCESSED_UID
    LAST_PROCESSED_UID = uid
    try:
        with open(UID_FILE, "w") as f:
            f.write(str(uid))
    except Exception as e:
        print(f"[ERROR] Failed to save UID to file '{UID_FILE}': {e}")

async def send_discord_message(discord_bot: discord.Client, channel_id: int, code: str):
    channel = discord_bot.get_channel(channel_id)
    if channel:
        await channel.send(f"Bambu Lab Verification Code: **{code}**")

# --- メインロジック (IMAP IDLE部分は維持) ---
def start_gmail_detector(discord_bot: discord.Client, gmail_channel_id: int):
    load_last_uid()
    th = threading.Thread(
        target=connection_loop,
        args=(discord_bot, gmail_channel_id),
        daemon=True
    )
    th.start()

def connection_loop(discord_bot: discord.Client, gmail_channel_id: int):
    while True:
        try:
            with IMAPClient("imap.gmail.com", ssl=True, use_uid=True) as server:
                print("[INFO] Connecting to IMAP server...")
                server.login(GMAIL_USER, GMAIL_PASS)
                print("[INFO] Login successful.")
                server.select_folder("[Gmail]/すべてのメール")
                process_new_emails(server, discord_bot, gmail_channel_id)
                idle_mode(server, discord_bot, gmail_channel_id)
        except exceptions.IMAPClientError as e:
            print(f"[ERROR] IMAP Client Error: {e}. Reconnecting in 30 seconds...")
        except Exception as e:
            print(f"[ERROR] An unexpected error occurred in connection_loop: {e}. Reconnecting in 30 seconds...")
        time.sleep(30)

def idle_mode(server, discord_bot, gmail_channel_id):
    print("[INFO] Entering IDLE mode. Waiting for new messages...")
    while True:
        try:
            server.idle()
            responses = server.idle_check(timeout=299)
            server.idle_done()
            if responses:
                print(f"[INFO] Received notification from server: {responses}")
                process_new_emails(server, discord_bot, gmail_channel_id)
                print("[INFO] Re-entering IDLE mode...")
        except exceptions.IMAPClientError as e:
            print(f"[ERROR] Lost connection during IDLE: {e}. Will attempt to reconnect.")
            break # 接続ループに戻る

def process_new_emails(server, discord_bot, gmail_channel_id):
    global LAST_PROCESSED_UID
    if LAST_PROCESSED_UID == 0:
        all_uids = server.search()
        if all_uids:
            latest_uid = max(all_uids)
            save_last_uid(latest_uid)
            print(f"[INFO] Starting point set to UID {latest_uid}.")
        return

    uids = server.search(['UID', f'{LAST_PROCESSED_UID + 1}:*'])
    if not uids: return
    
    print(f"[INFO] Found {len(uids)} new email(s).")
    uids_to_process = sorted(uids)[-10:]
    
    # BODY[]でメール全体を取得し、後から解析する
    fetched_emails = server.fetch(uids_to_process, ['BODY[]', 'ENVELOPE'])

    for uid, data in fetched_emails.items():
        env = data.get(b'ENVELOPE')
        if not env or TARGET_SUBJECT not in decode_str(env.subject or b""):
            continue

        raw_email = data.get(b'BODY[]')
        if not raw_email: continue
            
        msg = email.message_from_bytes(raw_email)
        
        # ★★★ 修正点：元のコードに基づいた、確実な本文取得ロジックに戻す ★★★
        body_text = get_body_text(msg)
        
        if body_text:
            code = extract_code(body_text)
            if code:
                print(f"[INFO] Found code '{code}' in email UID {uid}.")
                asyncio.run_coroutine_threadsafe(
                    send_discord_message(discord_bot, gmail_channel_id, code),
                    discord_bot.loop
                )
                save_last_uid(uid)
                return # 最新のコードを1件見つけたら処理を終了

    save_last_uid(max(uids_to_process))

def decode_str(encoded_bytes: bytes) -> str:
    decoded_parts = []
    for text, charset in decode_header(encoded_bytes):
        if isinstance(text, bytes):
            try:
                decoded_parts.append(text.decode(charset or 'utf-8', errors='ignore'))
            except (LookupError, TypeError):
                decoded_parts.append(text.decode('utf-8', errors='ignore'))
        else:
            decoded_parts.append(text)
    return "".join(decoded_parts)

# ★★★ 修正点：最初のコードをベースにした本文取得関数 ★★★
def get_body_text(msg: email.message.Message) -> str:
    """メールオブジェクトからtext/plainとtext/htmlの両方の内容を抽出する"""
    texts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            # 添付ファイルは無視し、テキストとHTMLパートのみを対象とする
            if ctype in ["text/plain", "text/html"]:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    try:
                        texts.append(payload.decode(charset, errors='replace'))
                    except LookupError:
                        texts.append(payload.decode('utf-8', errors='replace'))
    else: # シングルパートメールの場合
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or 'utf-8'
            try:
                texts.append(payload.decode(charset, errors='replace'))
            except LookupError:
                texts.append(payload.decode('utf-8', errors='replace'))

    # HTMLとテキストを単純に結合する
    return "\n\n".join(texts)

def extract_code(body_text: str) -> str:
    """抽出された本文テキストからコードを抜き出す"""
    try:
        # パーサーには高速な 'lxml' を使用
        soup = BeautifulSoup(body_text, "lxml")
        # HTMLタグを除去したプレーンテキストを取得
        plain_text = soup.get_text(separator=" ")
        match = CODE_REGEX.search(plain_text)
        if match:
            return match.group(1)
    except Exception as e:
        # body_textにHTMLが含まれない場合も考慮し、直接正規表現を試す
        print(f"[DEBUG] BeautifulSoup parsing failed: {e}. Falling back to regex on raw text.")
        match = CODE_REGEX.search(body_text)
        if match:
            return match.group(1)
    return ""