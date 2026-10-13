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

# 多账号 session，用英文半角 | 分隔
SESSIONS_STR = os.environ.get("SESSIONS", "")
SESSIONS = [s.strip() for s in SESSIONS_STR.split("|") if s.strip()]

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MY_CHAT_ID = int(os.environ.get("MY_CHAT_ID", 0))
# =============================

if not all([API_ID, API_HASH, SESSIONS, BOT_TOKEN, MY_CHAT_ID]):
    print("❌ 缺少环境变量 (API_ID / API_HASH / SESSIONS / BOT_TOKEN / MY_CHAT_ID)")
    exit(1)

def clean_text(text):
    if not text:
        return ""
    return re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00a0]', '', text).strip()

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

        # 必须同时包含"验证码"和"领取红包"
        if "验证码" not in text or "领取红包" not in text:
            return

        # 提取验证码
        match = re.search(r'验证码\s*([A-Za-z0-9]+)', text)
        if not match:
            return

        target_code = match.group(1).strip()
        print(f"[账号{idx}] 提取到验证码: {target_code}")

        # 遍历按钮，寻找匹配项
        for i, row in enumerate(markup.rows):
            for j, button in enumerate(row.buttons):
                btn_text = clean_text(button.text)
                if btn_text.lower() == target_code.lower():
                    try:
                        await event.click(i=i, j=j)
                        print(f"[账号{idx}] 点击成功: {btn_text}")
                        await send_tg(f"✅ [账号{idx}] 成功领取红包\n验证码: {target_code}")
                        return
                    except FloodWaitError as e:
                        print(f"[账号{idx}] 限流 {e.seconds} 秒")
                        await asyncio.sleep(e.seconds)
                    except Exception as e:
                        print(f"[账号{idx}] 点击失败: {e}")

    try:
        await client.start(phone=lambda: "")
        # 监听所有群消息
        client.add_event_handler(handler, events.NewMessage(incoming=True))
        print(f"[账号{idx}] 登录成功，监听所有群")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"[账号{idx}] 启动失败: {e}")

async def main():
    print(f"[启动] 共 {len(SESSIONS)} 个账号")
    tasks = [start_account(s, i) for i, s in enumerate(SESSIONS)]
    await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass