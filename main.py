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
SESSION_STRING = (os.environ.get("SESSION_STRING", "") or "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MY_CHAT_ID = int(os.environ.get("MY_CHAT_ID", 0))

# 监听这些人的发言，推送到私聊
TARGET_USERNAMES = [u.strip().lower().lstrip('@')
                    for u in os.environ.get("TARGET_USERNAMES", "").split(",")
                    if u.strip()]
# =============================

missing = []
if not API_ID: missing.append("API_ID")
if not API_HASH: missing.append("API_HASH")
if not SESSION_STRING: missing.append("SESSION_STRING")
if not BOT_TOKEN: missing.append("BOT_TOKEN")
if not MY_CHAT_ID: missing.append("MY_CHAT_ID")
if missing:
    print(f"❌ 缺少环境变量: {', '.join(missing)}")
    exit(1)

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID, API_HASH,
    connection_retries=5,
    retry_delay=5,
    auto_reconnect=True,
    request_retries=3,
    flood_sleep_threshold=86400,
    sequential_updates=False,
)

USERNAME_TO_ID = {}
ID_TO_USERNAME = {}

def clean_text(text):
    if not text: return ""
    return re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00a0]', '', text).strip()

async def send_tg(message):
    """发送推送消息到你的私聊"""
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

@client.on(events.NewMessage(incoming=True))
async def handler(event):
    """统一处理群消息，包含：1. 自动抢红包 2. 特定用户发言推送"""
    if not event.is_group:
        return

    text = clean_text(event.raw_text or "")
    markup = event.message.reply_markup

    # ================= 功能1：自动抢“验证码红包” =================
    # 特征：文本包含“验证码”和“领取红包”，且下方有按钮
    if markup and "验证码" in text and "领取红包" in text:
        # 提取“验证码”后面的字母/数字组合，例如 "MCPc"
        # 正则匹配：验证码后面的内容，一直到空格或“领取”为止
        match = re.search(r'验证码\s*([A-Za-z0-9]+)', text)
        if not match:
            # 兼容其他格式，比如“请选择正确的验证码 MCPc”
            match = re.search(r'验证码\s*([A-Za-z0-9]+)\s*领取', text)
        
        if match:
            target_code = match.group(1).strip()
            print(f"[抢红包] 提取到答案: {target_code}")
            
            # 遍历按钮，寻找匹配项
            for i, row in enumerate(markup.rows):
                for j, button in enumerate(row.buttons):
                    btn_text = clean_text(button.text)
                    # 忽略大小写比对，或者完全一致比对
                    if btn_text.lower() == target_code.lower() or btn_text == target_code:
                        try:
                            print(f"[抢红包] 命中按钮: {btn_text}，正在点击...")
                            await event.click(i=i, j=j)
                            print(f"[抢红包] 点击成功！")
                            # 可选：抢到后通知你
                            await send_tg(f"✅ 成功领取验证码红包\n答案：{target_code}")
                            return  # 抢到就结束，不再执行后续推送
                        except Exception as e:
                            print(f"[抢红包] 点击失败: {e}")

    # ================= 功能2：特定用户发言推送到私聊 =================
    # 如果不在目标名单里，直接跳过（避免频繁调用 get_sender 触发 429）
    if event.sender_id not in ID_TO_USERNAME:
        return

    try:
        sender = await event.get_sender()
    except Exception:
        return
    if sender is None:
        return

    username = ID_TO_USERNAME.get(event.sender_id, "未知")
    first_name = getattr(sender, 'first_name', '') or ''
    last_name = getattr(sender, 'last_name', '') or ''
    full_name = (first_name + ' ' + last_name).strip() or '未知用户'
    username_str = f"@{username}" if username else "无用户名"

    try:
        chat = await event.get_chat()
        chat_title = getattr(chat, 'title', '未知群组')
        raw_id = str(event.chat_id).replace('-100', '')
        msg_link = f"https://t.me/c/{raw_id}/{event.message.id}"
    except Exception:
        chat_title = '未知群组'
        msg_link = '无法获取链接'

    content = text or '[非文本消息]'
    msg = (
        f"👤 发送者：{full_name}\n"
        f"🔗 用户名：{username_str}\n"
        f"📍 群组：{chat_title}\n"
        f"💬 内容：\n{content}\n"
        f"🔗 链接：{msg_link}"
    )
    await send_tg(msg)

async def resolve_usernames():
    """启动时解析用户名到 ID"""
    if not TARGET_USERNAMES:
        return
    print(f"[预热] 解析 {len(TARGET_USERNAMES)} 个用户名...")
    for username in TARGET_USERNAMES:
        try:
            entity = await client.get_entity(username)
            USERNAME_TO_ID[username] = entity.id
            ID_TO_USERNAME[entity.id] = username
            print(f"  ✓ @{username} -> {entity.id}")
        except Exception as e:
            print(f"  ✗ @{username} 解析失败: {e}")

async def main():
    try:
        await client.start(phone=lambda: "")
        print(f'[启动] SESSION_STRING 长度: {len(SESSION_STRING)}')
        await resolve_usernames()
        print(f'[运行中] 监听所有群...')
        await client.run_until_disconnected()
    except FloodWaitError as e:
        print(f'[限流] 需等待 {e.seconds} 秒...')
        await asyncio.sleep(e.seconds)
        await main()
    except Exception as e:
        print(f'[崩溃] {type(e).__name__}: {e}')
        await asyncio.sleep(10)
        await main()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass