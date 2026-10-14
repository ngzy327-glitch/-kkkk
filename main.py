import asyncio
import os
import logging
import re
import time
import urllib.request
import urllib.parse
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, UserAlreadyParticipantError,
    InviteHashExpiredError, InviteHashInvalidError
)
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

BOT_USERNAMES_STR = os.environ.get("BOT_USERNAMES", "heji")
TARGET_BOT_USERNAMES = [u.strip().lstrip('@') for u in BOT_USERNAMES_STR.split(",") if u.strip()]

MIN_AMOUNT = float(os.environ.get("MIN_AMOUNT", "70"))
CONTROL_BOT = os.environ.get("CONTROL_BOT", "").strip().lstrip('@')
# =============================

if not all([API_ID, API_HASH, SESSIONS, BOT_TOKEN, MY_CHAT_ID, TARGET_BOT_USERNAMES]):
    print("❌ 缺少环境变量")
    exit(1)

BLOCK_RULES = {'kkpay': ['kkcoin'], 'dlqb': ['jiba']}
BLOCK_RULES = {k.lower(): [v.lower() for v in vs] for k, vs in BLOCK_RULES.items()}

CLAIM_BUTTON_KEYWORDS = ['领取红包', '🧧', '领取', '领红包', '抢红包']

def clean_text(text):
    if not text:
        return ""
    return re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00a0]', '', text).strip()

def is_claim_button(btn_text):
    cleaned = clean_text(btn_text)
    return any(k in cleaned for k in CLAIM_BUTTON_KEYWORDS)

def extract_total_amount(text):
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
    if not response:
        return None
    text = clean_text(response.message or "")
    m = re.search(r'(?:领到|抢到|获得)\s*([\d.]+)', text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m = re.search(r'([\d.]+)\s*(?:USDT|usdt|U|u|CNY|cny|QQ|qq)', text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
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

# ================== 加群核心逻辑（支持邀请链接） =================',=

def parse_ tjoin_target(target: str):
    """
    解析加[群目标，返回 (kind, value)
   1:]
    kind: 'username' | 'invite_hash'
    """
    t = target.strip()

    # 去掉常见前缀
    if t.startswith('https://t.me/'):
        t = t[len('https://t.me/'):]
    elif t.startswith('http://t.me/'):
        t = t[len('http://t.me/'):]
    elif t.startswith('t.me/'):
        t = t[len('t.me/'):]
    elif t.startswith('@'):
        t = t[1:]

    # 私密群邀请链接：+xxxx 或 joinchat/xxxx
    if t.startswith('+'):
        return 'invite_hash if t.startswith('joinchat/'):
        return 'invite_hash', t[len('joinchat/'):]

    # 公开群
    return 'username', t

async def join_target(client, target, idx):
    """加群，支持公开群和邀请链接"""
    kind, value = parse_join_target(target)

    try:
        if kind == 'invite_hash':
            # 私密群邀请链接
            try:
                result = await client(ImportChatInviteRequest(value))
                print(f"[账号{idx}] 已通过邀请链接加入: {target}")
                return True
            except UserAlreadyParticipantError:
                print(f"[账号{idx}] 已在群中: {target}")
                return True
            except InviteHashExpiredError:
                print(f"[账号{idx}] 邀请链接已过期: {target}")
                return False
            except InviteHashInvalidError:
                print(f"[账号{idx}] 邀请链接无效: {target}")
                return False
        else:
            # 公开群
            try:
                entity = await client.get_entity(value)
                await client(JoinChannelRequest(entity))
                print(f"[账号{idx}] 已加入公开群: {target}")
                return True
            except UserAlreadyParticipantError:
                print(f"[账号{idx}] 已在群中: {target}")
                return True

    except FloodWaitError as e:
        print(f"[账号{idx}] 加群限流 {e.seconds} 秒，等待中...")
        await asyncio.sleep(e.seconds)
        # 重试一次
        try:
            if kind == 'invite_hash':
                await client(ImportChatInviteRequest(value))
            else:
                entity = await client.get_entity(value)
                await client(JoinChannelRequest(entity))
            print(f"[账号{idx}] 重试成功: {target}")
            return True
        except Exception as e2:
            print(f"[账号{idx}] 重试仍失败: {e2}")
    except Exception as e:
        print(f"[账号{idx}] 加群失败: {e}")

    return False

# ================== 控制指令处理 ==================

async def handle_control_command(client, event, idx):
    text = (event.raw_text or "").strip()

    # /join <目标>
    if text.startswith('/join'):
        target = text[5:].strip()
        if not target:
            print(f"[账号{idx}] /join 缺少参数")
            return
        await join_target(client, target, idx)

    # /startbot <目标>
    elif text.startswith('/startbot'):
        target = text[9:].strip()
        if not target:
            print(f"[账号{idx}] /startbot 缺少参数")
            return
        try:
            entity = await client.get_entity(target)
            await client.send_message(entity, '/start')
            print(f"[账号{idx}] 已启动 Bot: {target}")
        except FloodWaitError as e:
            print(f"[账号{idx}] 启动 Bot 限流 {e.seconds} 秒")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"[账号{idx}] 启动 Bot 失败: {e}")

# ================== 单账号运行 ==================

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

    bot_id_to_username = {}

    async def send_detailed_notice(event, response, elapsed_ms, idx, low_amount=False):
        chat_title, chat_id = await get_chat_info(event)
        sender_id, sender_username = await get_sender_info(event)
        msg_link = build_msg_link(event.chat_id, event.message.id)
        amount = parse_amount_from_response(response)

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

        sender_bot_username = bot_id_to_username.get(event.sender_id, '').lower()
        if sender_bot_username in BLOCK_RULES:
            blocked_coins = BLOCK_RULES[sender_bot_username]
            text_lower = text.lower()
            for coin in blocked_coins:
                if coin in text_lower:
                    print(f"[账号{idx}] 屏蔽 @{sender_bot_username} 的 {coin} 红包，跳过")
                    return

        total = extract_total_amount(text)
        if total is not None and total < MIN_AMOUNT:
            print(f"[账号{idx}] 总金额 {total} 低于 {MIN_AMOUNT}，跳过")
            return

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

    async def control_handler(event):
        await handle_control_command(client, event, idx)

    try:
        await client.start(phone=lambda: "")

        bot_ids = []
        for uname in TARGET_BOT_USERNAMES:
            try:
                entity = await client.get_entity(uname)
                bot_ids.append(entity.id)
                bot_id_to_username[entity.id] = uname
                print(f"[账号{idx}] 解析 @{uname} -> {entity.id}")
            except Exception as e:
                print(f"[账号{idx}] 无法解析 @{uname}: {e}")

        if CONTROL_BOT:
            try:
                ctrl_entity = await client.get_entity(CONTROL_BOT)
                client.add_event_handler(
                    control_handler,
                    events.NewMessage(from_users=ctrl_entity.id, incoming=True)
                )
                print(f"[账号{idx}] 控制 Bot @{CONTROL_BOT} -> {ctrl_entity.id}")
            except Exception as e:
                print(f"[账号{idx}] 无法解析控制 Bot @{CONTROL_BOT}: {e}")

        if bot_ids:
            client.add_event_handler(
                handler,
                events.NewMessage(from_users=bot_ids, incoming=True)
            )
            print(f"[账号{idx}] 登录成功，监听 {len(bot_ids)} 个 Bot，阈值 {MIN_AMOUNT}")
        else:
            print(f"[账号{idx}] 没有解析到任何目标 Bot")

        await client.run_until_disconnected()
    except Exception as e:
        print(f"[账号{idx}] 启动失败: {e}")

async def main():
    print(f"[启动] 共 {len(SESSIONS)} 个账号，监听：{TARGET_BOT_USERNAMES}，阈值 {MIN_AMOUNT}")
    if CONTROL_BOT:
        print(f"[启动] 控制 Bot：@{CONTROL_BOT}")
    tasks = [start_account(s, i) for i, s in enumerate(SESSIONS)]
    await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass