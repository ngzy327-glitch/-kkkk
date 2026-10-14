import asyncio
import os
import logging
import re
import time
import urllib.request
import urllib.parse
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

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
TARGET_BOT_USERNAME = os.environ.get("BOT_USERNAME", "heji").strip().lstrip('@')

# 【新增】金额门槛：低于这个金额不抢
MIN_AMOUNT = float(os.environ.get("MIN_AMOUNT", "70"))
# =============================

if not all([API_ID, API_HASH, SESSIONS, BOT_TOKEN, MY_CHAT_ID, TARGET_BOT_USERNAME]):
    print("❌ 缺少环境变量")
    exit(1)

CLAIM_BUTTON_KEYWORDS = ['领取红包', '🧧', '领取', '领红包', '抢红包']

def clean_text(text):
    if not text:
        return ""
    return re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00a0]', '', text).strip()

def is_claim_button(btn_text):
    cleaned = clean_text(btn_text)
    return any(k in cleaned for k in CLAIM_BUTTON_KEYWORDS)

def extract_total_amount(text):
    """
    从消息文本里提取总金额（比如"金额：888.0 USDT"）。
    找不到返回 None。
    """
    if not text:
        return None
    m = re.search(r'(?:总?金额[：:]\s*)([\d.]+)', text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None

def parse_amount_from_response(response):
    """从机器人返回的消息里解析金额"""
    if not response:
        return None
    text = clean_text(response.message or "")
    # 优先匹配"领到了 X USDT"这种
    m = re.search(r'(?:领到|抢到|获得)\s*([\d.]+)', text)
    if m:
        return float(m.group(1))
    # 兜底：任何 数字 + 币种
    m = re.search(r'([\d.]+)\s*(?:USDT|usdt|U|u|CNY|cny|QQ|qq)', text)
    if m:
        return float(m.group(1))
    return None

async def send_tg(message):
    safe = urllib.parse.quote(message, encoding='utf-8')
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage?chat_id={MY_CHAT_ID}&text={safe}"
    req = urllib.request.Request(url, method='GET')
    try:
        def _send():
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        await asyncio.to_thread(_send)
    except Exception as e:
        print(f"[推送失败] {e}")

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
    raw_id = str(chat_id).replace('-100', '')
    return f"https://t.me/c/{raw_id}/{msg_id}"

async def click_button(event, i, j, btn_text, idx, reason, start_time):
    try:
        resp = await event.click(i=i, j=j)
        elapsed_ms = int((time.time() - start_time) * 1000)
        print(f"[账号{idx}] {reason} 点击成功: {btn_text} ({elapsed_ms}ms)")
        return True, resp, elapsed_ms
    except FloodWaitError as e:
        print(f"[账号{idx}] 限流 {e.seconds} 秒")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        print(f"[账号{idx}] 点击失败: {e}")
    return False, None, 0

async def start_account(session_string, idx):
    client = TelegramClient(
        StringSession(session_string),
        API_ID, API_HASH,
        connection_retries=5,
        retry_delay=5,
        auto_reconnect=True,
        request_retries=3,
        flood_sleep_threshold=86400,
        sequential_updates=False,
    )

    async def send_detailed_notice(event, response, elapsed_ms, idx, low_amount=False):
        chat_title, chat_id = await get_chat_info(event)
        sender_id, sender_username = await get_sender_info(event)
        msg_link = build_msg_link(event.chat_id, event.message.id)

        amount = parse_amount_from_response(response)

        # 尝试从消息文本解析红包发起者
        text = clean_text(event.raw_text or "")
        m = re.search(r'(.+?)\s*发送了一个红包', text)
        if m:
            sender_display = m.group(1).strip()
        else:
            sender_display = f"@{sender_username}" if sender_username else f"ID:{sender_id}"

        amount_str = f"{amount}" if amount is not None else "未知"
        flag = "⚠️ 金额低于阈值" if low_amount else "🎉 抢到红包"

        msg = (
            f"{flag}\n"
            f"账号：{idx}\n"
            f"群名：{chat_title}\n"
            f"群ID：{chat_id}\n"
            f"红包来自：{sender_display}\n"
            f"金额：{amount_str}\n"
            f"延迟：{elapsed_ms} ms\n"
            f"链接：{msg_link}"
        )
        await send_tg(msg)

    async def handler(event):
        start_time = time.time()
        text = clean_text(event.raw_text or "")
        markup = event.message.reply_markup
        if not markup:
            return

        # 【点击前拦截】如果消息明确写了总金额且低于阈值，跳过
        total = extract_total_amount(text)
        if total is not None and total < MIN_AMOUNT:
            print(f"[账号{idx}] 总金额 {total} 低于 {MIN_AMOUNT}，跳过")
            return

        # ---------- 模式1：验证码红包 ----------
        if "验证码" in text and "领取红包" in text:
            match = re.search(r'验证码\s*([A-Za-z0-9]+)', text)
            if match:
                target_code = match.group(1).strip()
                for i, row in enumerate(markup.rows):
                    for j, button in enumerate(row.buttons):
                        btn_text = clean_text(button.text)
                        if btn_text.lower() == target_code.lower():
                            success, resp, elapsed = await click_button(
                                event, i, j, btn_text, idx,
                                f"验证码红包({target_code})", start_time
                            )
                            if success:
                                amt = parse_amount_from_response(resp)
                                low = (amt is not None and amt < MIN_AMOUNT)
                                await send_detailed_notice(event, resp, elapsed, idx, low_amount=low)
                                return
                return

        # ---------- 模式2：公开红包 ----------
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
        try:
            entity = await client.get_entity(TARGET_BOT_USERNAME)
            bot_id = entity.id
            print(f"[账号{idx}] 解析 @{TARGET_BOT_USERNAME} -> {bot_id}")
        except Exception as e:
            print(f"[账号{idx}] 无法解析 @{TARGET_BOT_USERNAME}: {e}")
            return

        client.add_event_handler(
            handler,
            events.NewMessage(from_users=bot_id, incoming=True)
        )
        print(f"[账号{idx}] 登录成功，只监听 @{TARGET_BOT_USERNAME}，阈值 {MIN_AMOUNT}")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"[账号{idx}] 启动失败: {e}")

async def main():
    print(f"[启动] 共 {len(SESSIONS)} 个账号，只抢 @{TARGET_BOT_USERNAME}，金额阈值 {MIN_AMOUNT}")
    tasks = [start_account(s, i) for i, s in enumerate(SESSIONS)]
    await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass