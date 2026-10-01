# main.py
import os
import asyncio
import logging
import urllib.parse
import re
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import InputUser

# Try import RequestWebViewRequest; fallback handled later
try:
    from telethon.tl.functions.messages import RequestWebViewRequest
except Exception:
    RequestWebViewRequest = None

# async HTTP client for notifications
try:
    import httpx
except Exception:
    httpx = None

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger("sdrp-initdata")

# ---------- CONFIG from env ----------
API_ID = int(os.environ.get("TELETHON_API_ID", "0"))
API_HASH = os.environ.get("TELETHON_API_HASH", "")
STRING_SESSION = os.environ.get("TELETHON_STRING_SESSION", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "sdrp_bot")
TARGET_CHAT = int(os.environ.get("TARGET_CHAT", "-1003878320419"))

# Notification bot (where to forward initData)
NOTIFY_BOT_TOKEN = os.environ.get("NOTIFY_BOT_TOKEN", "")   # e.g. 123456:ABC-DEF...
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "")       # e.g. 123456789 or -1001234567890
# --------------------------------------

if not API_ID or not API_HASH:
    logger.error("请先在环境变量中设置 TELETHON_API_ID 和 TELETHON_API_HASH")
    raise SystemExit(1)

if httpx is None:
    logger.warning("httpx 未安装 — 通知功能将不可用。请在 requirements.txt 中加入 httpx 并重启。")

def find_startapp_and_url_from_message(msg):
    def find_in_url(u):
        if not u:
            return None
        try:
            u_unq = urllib.parse.unquote(u)
            parsed = urllib.parse.urlparse(u_unq)
            qs = urllib.parse.parse_qs(parsed.query)
            if 'startapp' in qs:
                return qs['startapp'][0], u
            frag = parsed.fragment or ""
            if frag:
                frag_unq = urllib.parse.unquote(frag)
                fq = dict(urllib.parse.parse_qsl(frag_unq))
                if 'startapp' in fq:
                    return fq['startapp'], u
            m = re.search(r"startapp=([^&\s#]+)", u_unq)
            if m:
                return urllib.parse.unquote(m.group(1)), u
        except Exception:
            logger.exception("解析 URL 出错")
        return None

    try:
        if hasattr(msg, "buttons") and msg.buttons:
            for row in msg.buttons:
                if not isinstance(row, (list, tuple)):
                    row = [row]
                for btn in row:
                    url = getattr(btn, "url", None)
                    if url:
                        res = find_in_url(url)
                        if res:
                            return res
                    web_app = getattr(btn, "web_app", None)
                    if web_app:
                        start_param = getattr(web_app, "start_param", None) or getattr(web_app, "start", None)
                        if start_param:
                            return start_param, f"web_app.start_param:{start_param}"
    except Exception:
        logger.exception("解析 message.buttons 出错")

    try:
        rm = getattr(msg, "reply_markup", None)
        if rm and hasattr(rm, "rows"):
            for r in rm.rows:
                for b in r.buttons:
                    url = getattr(b, "url", None)
                    if url:
                        res = find_in_url(url)
                        if res:
                            return res
                    web_app = getattr(b, "web_app", None)
                    if web_app:
                        start_param = getattr(web_app, "start_param", None) or getattr(web_app, "start", None)
                        if start_param:
                            return start_param, f"web_app.start_param:{start_param}"
    except Exception:
        logger.exception("解析 reply_markup 出错")

    try:
        text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
        urls = re.findall(r"https?://[^\s]+", text)
        for u in urls:
            res = find_in_url(u)
            if res:
                return res
    except Exception:
        logger.exception("解析文本 URL 出错")

    return None, None

async def send_notification_via_bot(initdata: str):
    """
    把 initdata 发送给你指定的通知 bot/chat。
    会将文本按 4000 字符分片发送以避免单条超长限制。
    使用 Telegram Bot API: sendMessage
    """
    if not NOTIFY_BOT_TOKEN or not NOTIFY_CHAT_ID:
        logger.warning("未配置 NOTIFY_BOT_TOKEN/NOTIFY_CHAT_ID，跳过通知发送")
        return {"ok": False, "error": "no_notify_config"}

    if httpx is None:
        logger.error("httpx 未安装，无法发送通知")
        return {"ok": False, "error": "httpx_missing"}

    send_url = f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage"
    # split into chunks (safe length)
    max_len = 3800
    parts = [initdata[i:i+max_len] for i in range(0, len(initdata), max_len)]
    results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for idx, part in enumerate(parts):
            text = f"initData part {idx+1}/{len(parts)}:\n{part}"
            try:
                r = await client.post(send_url, data={"chat_id": NOTIFY_CHAT_ID, "text": text})
                results.append({"status": r.status_code, "text": r.text})
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.exception("发送通知失败: %s", e)
                results.append({"error": str(e)})
    return {"ok": True, "results": results}

async def request_initdata_via_mtproto(client: TelegramClient, bot_username: str, webview_url: str) -> str:
    bot_ent = await client.get_entity(bot_username)
    bot_input = InputUser(user_id=bot_ent.id, access_hash=getattr(bot_ent, 'access_hash', 0))

    logger.info("调用 RequestWebViewRequest 请求 URL: %s", webview_url)
    try:
        if RequestWebViewRequest is not None:
            res = await client(RequestWebViewRequest(
                peer=bot_ent,
                bot=bot_input,
                platform='android',
                url=webview_url,
                theme_params=None,
                from_bot_menu=False,
                start_param=None,
            ))
        else:
            from telethon.tl import functions
            res = await client(functions.messages.RequestWebViewRequest(
                peer=bot_ent,
                bot=bot_input,
                platform='android',
                url=webview_url,
                theme_params=None,
                from_bot_menu=False,
                start_param=None,
            ))
    except Exception as e:
        logger.exception("RequestWebViewRequest 调用失败: %s", e)
        raise

    returned_url = getattr(res, "url", None)
    if not returned_url:
        raise RuntimeError("RequestWebViewRequest 未返回 url 字段")

    parsed = urllib.parse.urlparse(returned_url)
    frag = parsed.fragment or ""
    initdata_enc = None
    if frag:
        if frag.startswith("tgWebAppData="):
            initdata_enc = frag.split("tgWebAppData=", 1)[1]
        else:
            kvs = dict(urllib.parse.parse_qsl(frag))
            initdata_enc = kvs.get("tgWebAppData")
    if not initdata_enc and "tgWebAppData=" in returned_url:
        initdata_enc = returned_url.split("tgWebAppData=", 1)[1].split("&", 1)[0]
    if not initdata_enc:
        raise RuntimeError("未在返回 URL 中找到 tgWebAppData")

    initdata = urllib.parse.unquote(initdata_enc)
    return initdata

async def main():
    if STRING_SESSION:
        session = StringSession(STRING_SESSION)
        client = TelegramClient(session, API_ID, API_HASH)
    else:
        client = TelegramClient("sdrp_session", API_ID, API_HASH)

    await client.start()
    logger.info("Telethon 登录完成，开始监听群 %s", TARGET_CHAT)

    @client.on(events.NewMessage(chats=TARGET_CHAT))
    async def on_new_message(event):
        msg = event.message
        logger.info("收到新消息：chat=%s msg_id=%s", event.chat_id, getattr(msg, "id", None))

        token, src = find_startapp_and_url_from_message(msg)
        if not token:
            logger.info("未发现 startapp token")
            return

        logger.info("提取到 startapp token (最新): %s  来源: %s", token, src)

        if isinstance(src, str) and src.startswith("http"):
            webview_url = src
        else:
            logger.warning("未找到完整 webview URL，无法用 RequestWebViewRequest。src=%s", src)
            return

        try:
            initdata = await request_initdata_via_mtproto(client, BOT_USERNAME, webview_url)
            # 出于安全考虑，日志中不打印完整 initData，仅打印长度与前后若干字符
            logger.info("获取到 initData（len=%d） prefix=%s... suffix=... ", len(initdata), initdata[:40])
            # 将 initData 发送到通知 Bot（异步）
            notify_res = await send_notification_via_bot(initdata)
            logger.info("通知发送结果: %s", notify_res)
        except Exception as e:
            logger.exception("获取或发送 initData 失败: %s", e)

    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("已停止")
