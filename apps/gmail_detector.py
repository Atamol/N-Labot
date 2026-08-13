import os
import re
import time
import socket
import logging
import threading
import asyncio
import email
from email.header import decode_header

from imapclient import IMAPClient
from bs4 import BeautifulSoup
import discord

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_PASS")

BAMBU_SENDER_REGEX = re.compile(r"@(?:[\w-]+\.)*bambulab\.(?:com|net)$", re.IGNORECASE)
BAMBU_SUBJECT_REGEX = re.compile(r"verification|認証コード|確認コード", re.IGNORECASE)

CODE_REGEX = re.compile(r"verification\s+code[^0-9]*?(\d{6})", re.IGNORECASE | re.DOTALL)
CODE_FALLBACK_REGEX = re.compile(r"(?<!\d)(\d{6})(?!\d)")

LAST_PROCESSED_UID = 0

# 直近送信したコードの重複抑止 (30秒以内の同コードはスキップ)
_LAST_SENT_CODE = None
_LAST_SENT_AT = 0.0
_DEDUP_WINDOW = 30.0

def initialize_last_uid():
    global LAST_PROCESSED_UID
    try:
        with IMAPClient("imap.gmail.com", ssl=True, use_uid=True) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            target_folder = "[Gmail]/すべてのメール"
            status = server.folder_status(target_folder, ['UIDNEXT'])
            uidnext = status.get(b'UIDNEXT')
            if uidnext:
                LAST_PROCESSED_UID = uidnext - 1
    except Exception:
        log.exception("initialize_last_uid failed")

def _enable_keepalive(server: IMAPClient):
    # ルーター，NATの沈黙切断を防ぐ
    sock = server.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt, val in (
        ("TCP_KEEPIDLE", 60),
        ("TCP_KEEPINTVL", 30),
        ("TCP_KEEPCNT", 4),
    ):
        if hasattr(socket, opt):
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)

_started = False

def start_gmail_detector(discord_bot: discord.Client, gmail_channel_id: int):
    global _started
    if _started:
        log.warning("start_gmail_detector called twice, ignoring")
        return
    _started = True
    log.info("start_gmail_detector invoked")
    initialize_last_uid()

    th = threading.Thread(
        target=idle_loop,
        args=(discord_bot, gmail_channel_id),
        daemon=True,
        name="gmail-idle",
    )
    th.start()

def idle_loop(discord_bot: discord.Client, gmail_channel_id: int):
    target_folder = "[Gmail]/すべてのメール"
    # NATのアイドル切断を避けるため短めに
    IDLE_TIMEOUT = 5 * 60
    backoff = 5
    BACKOFF_MAX = 300

    while True:
        try:
            with IMAPClient("imap.gmail.com", ssl=True, use_uid=True, timeout=60) as server:
                server.login(GMAIL_USER, GMAIL_PASS)
                server.select_folder(target_folder)
                _enable_keepalive(server)

                # 再接続後の取りこぼしを拾う
                fetch_latest_and_notify(server, discord_bot, gmail_channel_id)

                # 接続維持できたのでバックオフ解除
                backoff = 5

                while True:
                    server.idle()
                    try:
                        responses = server.idle_check(timeout=IDLE_TIMEOUT)
                    finally:
                        server.idle_done()

                    if not responses:
                        # タイムアウト時はNOOPで生存確認して再IDLE
                        server.noop()
                        continue

                    if any(len(r) >= 2 and r[1] in (b'EXISTS', b'RECENT') for r in responses):
                        fetch_latest_and_notify(server, discord_bot, gmail_channel_id)
        except Exception:
            log.exception("Gmail idle loop crashed, reconnecting in %ds", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

def fetch_latest_and_notify(server: IMAPClient, discord_bot: discord.Client, gmail_channel_id: int):
    global LAST_PROCESSED_UID
    log.info("fetch start, LAST_PROCESSED_UID=%d", LAST_PROCESSED_UID)

    new_uids = sorted(
        uid for uid in server.search(['UID', f'{LAST_PROCESSED_UID + 1}:*'])
        if uid > LAST_PROCESSED_UID
    )
    if not new_uids:
        log.info("no new mail")
        return

    matched = []
    for uid, data in sorted(server.fetch(new_uids, ['ENVELOPE']).items()):
        sender, subject = summarize_envelope(data.get(b'ENVELOPE'))
        hit = bool(BAMBU_SENDER_REGEX.search(sender) or BAMBU_SUBJECT_REGEX.search(subject))
        log.info("uid=%d from=%s subject=%r bambu=%s", uid, sender, subject, hit)
        if hit:
            matched.append(uid)

    LAST_PROCESSED_UID = max(new_uids)

    if not matched:
        return

    # 直近1通だけ処理 (古いコードは無効のため)
    uid = matched[-1]
    msg_info = server.fetch(uid, ['BODY[]']).get(uid)
    if not msg_info or (b'BODY[]' not in msg_info):
        return

    msg = email.message_from_bytes(msg_info[b'BODY[]'])
    body_text = get_body_text(msg)
    code = extract_code(body_text)
    log.info("extracted code=%s for uid=%d", code, uid)
    if code:
        _dispatch_discord_message(discord_bot, gmail_channel_id, code)

def _dispatch_discord_message(discord_bot: discord.Client, channel_id: int, code: str):
    global _LAST_SENT_CODE, _LAST_SENT_AT
    now = time.monotonic()
    log.info("dispatch called: code=%s last=%s elapsed=%.2f", code, _LAST_SENT_CODE, now - _LAST_SENT_AT)
    if code == _LAST_SENT_CODE and (now - _LAST_SENT_AT) < _DEDUP_WINDOW:
        log.info("Skip duplicate code: %s", code)
        return
    _LAST_SENT_CODE = code
    _LAST_SENT_AT = now

    loop = getattr(discord_bot, "loop", None)
    if loop is None or not loop.is_running():
        log.warning("Discord loop not ready, dropping code: %s", code)
        return
    asyncio.run_coroutine_threadsafe(
        send_discord_message(discord_bot, channel_id, code),
        loop,
    )

async def send_discord_message(discord_bot: discord.Client, channel_id: int, code: str):
    channel = discord_bot.get_channel(channel_id)
    if channel is None:
        log.warning("Channel %d not found", channel_id)
        return
    # 重複送信を避けるためリトライしない (失効したらユーザーが再送する)
    try:
        await channel.send(f"Bambu Lab Verification Code: **{code}**")
    except Exception:
        log.exception("Discord send failed for code %s", code)

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

def summarize_envelope(env) -> tuple[str, str]:
    if env is None:
        return "", ""
    sender = ""
    if env.from_:
        addr = env.from_[0]
        mailbox = (addr.mailbox or b"").decode(errors="replace")
        host = (addr.host or b"").decode(errors="replace")
        sender = f"{mailbox}@{host}"
    subject = decode_str(env.subject.decode(errors="replace")) if env.subject else ""
    return sender, subject

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
    text = soup.get_text(separator=" ")
    match = CODE_REGEX.search(text) or CODE_FALLBACK_REGEX.search(text)
    if match:
        return match.group(1)
    log.warning("no code found in body: %r", text[:300])
    return ""