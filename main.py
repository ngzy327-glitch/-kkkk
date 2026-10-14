import asyncio
import os
import logging
import re
import time
import base64
import hashlib
import urllib.request
import urllib.parse
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
from telethon.tl.types import MessageEntityCustomEmoji
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

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

# 【视觉 AI 配置】用于识别 emoji
VISION_API_KEY = os.environ.get("VISION_API_KEY", "")
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "https://maas.qianwenaiapi.com/compatible-mode/v1")
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen3.7-plus")
# =============================

if not all([API_ID, API_HASH, SESSIONS, BOT_TOKEN, MY_CHAT_ID, TARGET_BOT_USERNAMES]):
    print("❌ 缺少环境变量")
    exit(1)

BLOCK_RULES = {'kkpay': ['kkcoin'], 'dlqb': ['jiba']}
BLOCK_RULES = {k.lower(): [v.lower() for v in vs] for k, vs in BLOCK_RULES.items()}

def clean_text(text):
    if not text: return ""
    return re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00a0]', '', text).strip()

# ================== AI 识别自定义表情 ==================
async def ai_identify(thumb_bytes: bytes) -> str:
    """用视觉 AI 识别缩略图上的字母/数字"""
    if not thumb_bytes or not VISION_API_KEY: return ""
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
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode('utf-8')
        resp_text = await asyncio.to_thread(_do_request)
        result = json.loads(resp_text)
        answer = result["choices"][0]["message"]["content"].strip().upper()
        return "".join(c for c in answer if c.isascii() and c.isalnum())[:1]
    except Exception as e:
        print(f"[AI识别异常] {e}")
    return ""

async def decode_custom_emoji(client, doc_ids: list[int]) -> dict[int, str]:
    """批量下载缩略图并用 AI 识别"""
    if not doc_ids: return {}
    result = {}
    try:
        docs = await client(GetCustomEmojiDocumentsRequest(document_id=doc_ids))
    except Exception: return result
    
    tasks = []
    for doc in docs:
        if doc.thumbs:
            tasks.append(download_and_identify(client, doc))
    
    decoded = await asyncio.gather(*tasks)
    for doc_id, char in decoded:
        if char: result[doc_id] = char
    return result

async def download_and_identify(client, doc):
    try:
        thumb = await client.download_media(doc, file=bytes, thumb=-1)
        if thumb:
            ch = await ai_identify(thumb)
            return doc.id, ch
    except Exception: pass
    return doc.id, ""

# ================== 消息处理逻辑 ==================
async def send_tg(message):
    safe = urllib.parse.quote(message, encoding='utf-8')
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage?chat_id={MY_CHAT_ID}&text={safe}"
    req = urllib.request.Request(url, method='GET')
    try:
        def _send():
            with urllib.request.urlopen(req, timeout=10) as resp: pass
        await asyncio.to_thread(_send)
    except Exception as e: print(f"[推送失败] {e}")

async def start_account(session_string, idx):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH, connection_retries=5, retry_delay=5, auto_reconnect=True, request_retries=3, flood_sleep_threshold=86400, sequential_updates=False)

    async def handler(event):
        start_time = time.time()
        text = clean_text(event.raw_text or "")
        markup = event.message.reply_markup
        if not markup: return

        # 1. 尝试从纯文本中直接提取（如果表情是 Unicode 字符）
        # 匹配类似 "V + Z = ?"
        match = re.search(r'([A-Za-z0-9])\s*[\+＋]\s*([A-Za-z0-9])', text)
        answer = ""
        if match:
            answer = match.group(1) + match.group(2)
        
        # 2. 如果没提取到，说明是自定义表情，需要用 AI 识别
        if not answer:
            entities = event.message.entities or []
            doc_ids = [e.document_id for e in entities if isinstance(e, MessageEntityCustomEmoji)]
            # 如果消息里有自定义表情，并且包含 "=" 或 "?"
            if doc_ids and ("=" in text or "?" in text):
                print(f"[账号{idx}] 检测到自定义表情验证码，开始 AI 识别...")
                decoded = await decode_custom_emoji(client, doc_ids)
                # 把所有识别出的字符按顺序拼起来
                answer = "".join(decoded[d] for d in doc_ids if d in decoded)
                print(f"[账号{idx}] AI 识别结果: {answer}")
        
        if not answer: return # 无法解析答案

        # 3. 在按钮中寻找答案
        for i, row in enumerate(markup.rows):
            for j, button in enumerate(row.buttons):
                btn_text = clean_text(button.text)
                if btn_text.upper() == answer.upper():
                    try:
                        await event.click(i=i, j=j)
                        elapsed_ms = int((time.time() - start_time) * 1000)
                        print(f"[账号{idx}] 点击成功: {btn_text} ({elapsed_ms}ms)")
                        await send_tg(f"✅ [账号{idx}] 成功领取验证码红包\n答案: {answer}\n耗时: {elapsed_ms}ms")
                        return
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds)
                    except Exception as e:
                        print(f"[账号{idx}] 点击失败: {e}")

    try:
        await client.start(phone=lambda: "")
        bot_ids = []
        for uname in TARGET_BOT_USERNAMES:
            try:
                entity = await client.get_entity(uname)
                bot_ids.append(entity.id)
                print(f"[账号{idx}] 解析 @{uname} -> {entity.id}")
            except Exception as e:
                print(f"[账号{idx}] 无法解析 @{uname}: {e}")

        if bot_ids:
            client.add_event_handler(handler, events.NewMessage(from_users=bot_ids, incoming=True))
            print(f"[账号{idx}] 登录成功，监听 {len(bot_ids)} 个 Bot")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"[账号{idx}] 启动失败: {e}")

async def main():
    print(f"[启动] 共 {len(SESSIONS)} 个账号，监听：{TARGET_BOT_USERNAMES}")
    tasks = [start_account(s, i) for i, s in enumerate(SESSIONS)]
    await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass