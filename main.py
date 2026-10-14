import asyncio
import os
import logging
import re
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
# 【新增】只抢这个 Bot 的红包，不带 @
TARGET_BOT_USERNAME = os.environ.get("BOT_USERNAME", "heji").strip().lstrip('@')
# =============================

if not all([API_ID, API_HASH, SESSIONS, BOT_TOKEN, MY_CHAT_ID, TARGET_BOT_USERNAME]):
    print("❌ 缺少环境变量")
    exit(1)

# 公开红包按钮关键词
CLAIM_BUTTON_KEYWORDS = ['领取红包', '🧧', '领取', '领红包', '抢红包']

def clean_text(text):
    if not text:
        return ""
    return re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00a0]', '', text).strip()

def is_claim_button(btn_text):
    cleaned = clean_text(btn_text)
    return any(k in cleaned for k in CLAIM_BUTTON_KEYWORDS)

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

async def click_button(event, i, j, btn_text, idx, reason):
    try:
        await event.click(i=i, j=j)
        print(f"[账号{idx}] {reason} 点击成功: {btn_text}")
        await send_tg(f"✅ [账号{idx}] {reason}\n按钮: {btn_text}")
        return True
    except FloodWaitError as e:
        print(f"[账号{idx}] 限流 {e.seconds} 秒")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        print(f"[账号{idx}] 点击失败: {e}")
    return False

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

    async def handler(event):
        text = clean_text(event.raw_text or "")
        markup = event.message.reply_markup
        if not markup:
            return

        # ---------- 模式1：验证码红包 ----------
        if "验证码" in text and "领取红包" in text:
            match = re.search(r'验证码\s*([A-Za-z0-9]+)', text)
            if match:
                target_code = match.group(1).strip()
                print(f"[账号{idx}] 验证码红包，答案: {target_code}")
                for i, row in enumerate(markup.rows):
                    for j, button in enumerate(row.buttons):
                        btn_text = clean_text(button.text)
                        if btn_text.lower() == target_code.lower():
                            if await click_button(event, i, j, btn_text, idx,
                                                  f"验证码红包({target_code})"):
                                return
                return

        # ---------- 模式2：公开红包 ----------
        for i, row in enumerate(markup.rows):
            for j, button in enumerate(row.buttons):
                btn_text = getattr(button, 'text', '') or ''
                if is_claim_button(btn_text):
                    print(f"[账号{idx}] 公开红包，按钮: {btn_text}")
                    if await click_button(event, i, j, btn_text, idx, "公开红包"):
                        return

    try:
        await client.start(phone=lambda: "")

        # 【关键】启动时解析目标 Bot 为 ID，只解析一次
        try:
            entity = await client.get_entity(TARGET_BOT_USERNAME)
            bot_id = entity.id
            print(f"[账号{idx}] 解析 @{TARGET_BOT_USERNAME} -> {bot_id}")
        except Exception as e:
            print(f"[账号{idx}] 无法解析 @{TARGET_BOT_USERNAME}: {e}")
            return

        # 【关键】用 from_users 在事件层过滤，不触发任何额外 API
        client.add_event_handler(
            handler,
            events.NewMessage(from_users=bot_id, incoming=True)
        )
        print(f"[账号{idx}] 登录成功，只监听 @{TARGET_BOT_USERNAME}")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"[账号{idx}] 启动失败: {e}")

async def main():
    print(f"[启动] 共 {len(SESSIONS)} 个账号，只抢 @{TARGET_BOT_USERNAME}")
    tasks = [start_account(s, i) for i, s in enumerate(SESSIONS)]
    await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass