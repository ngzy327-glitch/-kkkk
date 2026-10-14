import asyncio
import os
import logging
import re
import time
import base64
import json
import urllib.request
import urllib.parse
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
from telethon.tl.types import MessageEntityCustomEmoji

logging.basicConfig(level=logging.CRITICAL)
for name in logging.root.manager.loggerDict:
    logging.getLogger(name).setLevel(logging.CRITICAL)
logging.getLogger('telethon').setLevel(logging.CRITICAL)

# ========== 环境变量 ==========
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
SESSIONS_STR = os.environ.get("SESSIONS", "")
SESSIONS = [s.strip() for s in SESSIONS_STR.split("|") if s.strip()]
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MY_CHAT_ID = int(os.environ.get("MY_CHAT_ID", 0))
BOT_USERNAMES_STR = os.environ.get("BOT_USERNAMES", "kkpay")
TARGET_BOT_USERNAMES = [u.strip().lstrip('@') for u in BOT_USERNAMES_STR.split(",") if u.strip()]
MIN_AMOUNT = float(os.environ.get("MIN_AMOUNT", "70"))

VISION_API_KEY = os.environ.get("VISION_API_KEY", "")
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "")
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen3.7-plus")
# =============================

if not all([API_ID, API_HASH, SESSIONS, BOT_TOKEN, MY_CHAT_ID, TARGET_BOT_USERNAMES]):
    print("❌ 缺少环境变量")
    exit(1)

BLOCK_RULES = {'kkpay': ['kkcoin'], 'dlqb': ['jiba']}
BLOCK_RULES = {k.lower(): [v.lower() for v in vs] for k, vs in BLOCK_RULES.items()}
CLAIM_BUTTON_KEYWORDS = ['领取红包', '🧧', '领取', '领红包', '抢红包']

def clean_text(text):
    if not text: return ""
    return re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00a0]', '', text).strip()

def is_claim_button(btn_text):
    cleaned = clean_text(btn_text)
    return any(k in cleaned for k in CLAIM_BUTTON_KEYWORDS)

def extract_total_amount(text):
    if not text: return None
    m = re.search(r'(?:总?金额[：:]\s*)([\d.]+)', text)
    if m:
        try: return float(m.group(1))
        except ValueError: return None
    return None

def parse_amount_from_response(response):
    if not response: return None
    text = clean_text(response.message or "")
    m = re.search(r'(?:领到|抢到|获得)\s*([\d.]+)', text)
    if m:
        try: return float(m.group(1))
        except ValueError: pass
    m = re.search(r'([\d.]+)\s*(?:USDT|usdt|U|u|CNY|cny|QQ|qq)', text)
    if m:
        try: return float(m.group(1))
        except ValueError: pass
    return None

async def send_tg(message):
:
    safe = urllib.parse.quote(message       , encoding='utf-8')
 print    url(f = f"https://api"[.推送telegram.org/bot{BOT_TOKEN}/sendMessage?chat_id={MY_CHAT_ID}&text={safe}"
    req = urllib.request.Request(url, method='GET')
    try:
        def _send():
            with urllib.request.urlopen(req, timeout=10) as resp: pass
        await asyncio.to_thread(_send)
    except Exception as e失败] {e}")

async def get_sender_info(event):
    try:
        sender = await event.get_sender()
        return sender.id, getattr(sender, 'username', None)
    except Exception:
        return None, None

async def get_chat_info(event):
    try:
        chat = await event.get_chat()
        return getattr(chat, 'title', '未知群组'), event.chat_id
    except Exception:
        return '未知群组', event.chat_id

def build_msg_link(chat_id, msg_id):
    return f"https://t.me/c/{str(chat_id).replace('-100','')}/{msg_id}"

async def click_button(event, i, j, btn_text, idx, reason, start_time):
    try:
        resp = await event.click(i=i, j=j)
        elapsed_ms = int((time.time() - start_time) * 1000)
        print(f"[账号{idx}] {reason} 点击: {btn_text} ({elapsed_ms}ms)")
        return True, resp, elapsed_ms
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds)
    except Exception:
        pass
    return False, None, 0

# ================== AI 识别 ==================
async def ai_identify(thumb_bytes: bytes) -> str:
    if not thumb_bytes or not VISION_API_KEY or not VISION_BASE_URL:
        return ""
    b64 = base64.b64encode(thumb_bytes).decode()
    url = f"{VISION_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {VISION_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": VISION_MODEL, "max_tokens": 10,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": "图片上是一个大写字母或数字，只回答这一个字符。"},
        ]}],
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(data, ensure_ascii=True).encode('utf-8'), headers=headers)
        def _do_request():
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read().decode('utf-8')
        resp_text = await asyncio.wait_for(asyncio.to_thread(_do_request), timeout=20.0)
        result = json.loads(resp_text)
        answer = result["choices"][0]["message"]["content"].strip().upper()
        ch = "".join(c for c in answer if c.isascii() and c.isalnum())
        return ch[:1]
    except Exception:
        pass
    return ""

async def decode_custom_emoji(client, doc_ids: list[int]) -> dict[int, str]:
    if not doc_ids: return {}
    result = {}
    try:
        docs = await client(GetCustomEmojiDocumentsRequest(document_id=doc_ids))
    except Exception:
        return result
    tasks = []
    for doc in docs:
        if doc.thumbs:
            tasks.append(_download_and_identify(client, doc))
    decoded = await asyncio.gather(*tasks)
    for doc_id, char in decoded:
        if char: result[doc_id] = char
    return result

async def _download_and_identify(client, doc):
    try:
        thumb = await client.download_media(doc, file=bytes, thumb=-1)
        if thumb:
            ch = await ai_identify(thumb)
            return doc.id, ch
    except Exception: pass
    return doc.id, ""

# ================== 单账号 ==================
async def start_account(session_string, idx):
    client = TelegramClient(
        StringSession(session_string),
        API_ID, API_HASH,
        connection_retries=5, retry_delay=5,
        auto_reconnect=True, request_retries=3,
        flood_sleep_threshold=86400, sequential_updates=False,
    )

    bot_id_to_username = {}

    async def send_detailed_notice(event, response, elapsed_ms, idx, low_amount=False):
        chat_title, chat_id = await get_chat_info(event)
        sender_id, sender_username = await get_sender_info(event)
        msg_link = build_msg_link(event.chat_id, event.message.id)
        amount = parse_amount_from_response(response)
        text = clean_text(event.raw_text or "")
        m = re.search(r'(.+?)\s*发送了一个红包', text)
        sender_display = m.group(1).strip() if m else (f"@{sender_username}" if sender_username else f"ID:{sender_id}")
        amount_str = f"{amount}" if amount is not None else "未知"
        flag = "⚠️ 金额低于阈值" if low_amount else "🎉 抢到红包"
        msg = (
            f"{flag}\n账号：{idx}\n群名：{chat_title}\n群ID：{chat_id}\n"
            f"红包来自：{sender_display}\n金额：{amount_str}\n"
            f"延迟：{elapsed_ms} ms\n链接：{msg_link}"
        )
        await send_tg(msg)

    async def handler(event):
        start_time = time.time()
        raw_text = event.raw_text or ""
        text = clean_text(raw_text)
        entities = event.message.entities or []
        markup = event.message.reply_markup
        if not markup:
            return

        # 黑名单
        sender_bot_username = bot_id_to_username.get(event.sender_id, '').lower()
        if sender_bot_username in BLOCK_RULES:
            for coin in BLOCK_RULES[sender_bot_username]:
                if coin in text.lower():
                    return

        # 金额门槛
        total = extract_total_amount(text)
        if total is not None and total < MIN_AMOUNT:
            return

        # 验证码红包
        is_code_rp = ("验证码" in text) or ("+" in text and "=" in text) or ("＋" in text and "=" in text)
        if is_code_rp:
            answer = ""
            m = re.search(r'([A-Za-z0-9])\s*[\+＋]\s*([A-Za-z0-9])', text)
            if m:
                answer = m.group(1) + m.group(2)
            if not answer:
                doc_ids = [e.document_id for e in entities if isinstance(e, MessageEntityCustomEmoji)]
                if doc_ids:
                    decoded = await decode_custom_emoji(client, doc_ids)
                    answer = "".join(decoded.get(d, "") for d in doc_ids)
            if answer:
                for i, row in enumerate(markup.rows):
                    for j, button in enumerate(row.buttons):
                        btn_text = clean_text(button.text)
                        if btn_text.upper() == answer.upper():
                            success, resp, elapsed = await click_button(
                                event, i, j, btn_text, idx, f"验证码({answer})", start_time
                            )
                            if success:
                                amt = parse_amount_from_response(resp)
                                low = (amt is not None and amt < MIN_AMOUNT)
                                await send_detailed_notice(event, resp, elapsed, idx, low_amount=low)
                                return
            return

        # 公开红包
        for i, row in enumerate(markup.rows):
            for j, button in enumerate(row.buttons):
                btn_text = getattr(button, 'text', '') or ''
                if is_claim_button(btn_text):
                    success, resp, elapsed = await click_button(
                        event, i, j, btn_text, idx, "公开红包", start_time
                    )
                    if success:
                        amt = parse_amount_from_response(resp)
                        low = (amt is not None and amt < MIN_AMOUNT)
                        await send_detailed_notice(event, resp, elapsed, idx, low_amount=low)
                        return

    try:
        await client.start(phone=lambda: "")
        bot_ids = []
        for uname in TARGET_BOT_USERNAMES:
            try:
                entity = await client.get_entity(uname)
                bot_ids.append(entity.id)
                bot_id_to_username[entity.id] = uname
                print(f"[账号{idx}] @{uname} -> {entity.id}")
            except Exception:
                print(f"[账号{idx}] 无法解析 @{uname}")
        if bot_ids:
            client.add_event_handler(handler, events.NewMessage(from_users=bot_ids, incoming=True))
            print(f"[账号{idx}] 监听 {len(bot_ids)} 个 Bot")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"[账号{idx}] 启动失败: {e}")

async def main():
    print(f"[启动] {len(SESSIONS)} 个账号，监听：{TARGET_BOT_USERNAMES}")
    tasks = [start_account(s, i) for i, s in enumerate(SESSIONS)]
    await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
        