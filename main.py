# main.py
import os
import asyncio
import logging
import re
import urllib.parse
from typing import Optional, Tuple

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import InputUser

# 兼容不同 Telethon 版本
try:
    from telethon.tl.functions.messages import RequestWebViewRequest
except Exception:
    RequestWebViewRequest = None

try:
    import httpx
except Exception:
    httpx = None

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger("sdrp-initdata")

# -------------------------
# 读取 env 配置
# -------------------------
API_ID = int(os.environ.get("TELETHON_API_ID", "0"))
API_HASH = os.environ.get("TELETHON_API_HASH", "")
STRING_SESSION = os.environ.get("TELETHON_STRING_SESSION", "")

BOT_USERNAME = os.environ.get("BOT_USERNAME", "sdrp_bot")
TARGET_CHAT = int(os.environ.get("TARGET_CHAT", "-1003878320419"))

NOTIFY_BOT_TOKEN = os.environ.get("NOTIFY_BOT_TOKEN", "")
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "")

if not API_ID or not API_HASH:
    logger.error("请在 Railway 环境变量中设置 TELETHON_API_ID 和 TELETHON_API_HASH")
    raise SystemExit(1)

if httpx is None:
    logger.warning("httpx 未安装，通知功能将不可用。请确保 requirements.txt 中包含 httpx")


def build_tme_url(bot_username: str, startapp_token: str) -> str:
    """
    构造 t.me 链接：
    https://t.me/sdrp_bot?startapp=rp_bOTsrnJq
    """
    return f"https://t.me/{bot_username}?startapp={urllib.parse.quote(startapp_token, safe='')}"


def find_startapp_and_source_from_message(msg):
    """
    从消息中提取最新的 startapp token 和对应 source
    返回: (token, source_url_or_note)
    """
    def find_in_url(u: Optional[str]):
        if not u:
            return None
        u_unq = urllib.parse.unquote(u)
        parsed = urllib.parse.urlparse(u_unq)
        qs = urllib.parse.parse_qs(parsed.query)
        if "startapp" in qs:
            return qs["startapp"][0], u

        frag = parsed.fragment or ""
        if frag:
            frag_unq = urllib.parse.unquote(frag)
            fq = dict(urllib.parse.parse_qsl(frag_unq))
            if "startapp" in fq:
                return fq["startapp"], u

        m = re.search(r"startapp=([^&\s#]+)", u_unq)
        if m:
            return urllib.parse.unquote(m.group(1)), u

        return None

    # 1) message.buttons / reply_markup
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

    # 2) 文本中的 URL
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


def parse_initdata_from_returned_url(returned_url: Optional[str]) -> Optional[str]:
    """
    从 RequestWebViewRequest 返回的 URL 中解析 tgWebAppData
    例如:
      https://...#tgWebAppData=query_id%3D...%26hash%3D...
    返回 decode 后的 initData
    """
    if not returned_url:
        return None

    frag = urllib.parse.urlparse(returned_url).fragment or ""
    initdata_enc = None

    if frag.startswith("tgWebAppData="):
        initdata_enc = frag.split("tgWebAppData=", 1)[1]
    else:
        kvs = dict(urllib.parse.parse_qsl(frag))
        initdata_enc = kvs.get("tgWebAppData")

    if not initdata_enc and "tgWebAppData=" in returned_url:
        initdata_enc = returned_url.split("tgWebAppData=", 1)[1].split("&", 1)[0]

    if not initdata_enc:
        return None

    return urllib.parse.unquote(initdata_enc)


async def send_notification_via_bot(initdata: str):
    """
    把 initData 发送给通知 bot
    用 Telegram Bot API 的 sendMessage 发送
    """
    if not NOTIFY_BOT_TOKEN or not NOTIFY_CHAT_ID:
        logger.warning("未配置 NOTIFY_BOT_TOKEN / NOTIFY_CHAT_ID，跳过 initData 通知")
        return {"ok": False, "error": "notify_not_configured"}

    if httpx is None:
        logger.error("httpx 未安装，无法发送通知")
        return {"ok": False, "error": "httpx_missing"}

    send_url = f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage"

    # Telegram 单条消息长度有限，分片发送
    max_len = 3800
    chunks = [initdata[i:i+max_len] for i in range(0, len(initdata), max_len)]

    results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for i, chunk in enumerate(chunks, 1):
            text = f"initData part {i}/{len(chunks)}\n{chunk}"
            try:
                r = await client.post(send_url, data={"chat_id": NOTIFY_CHAT_ID, "text": text})
                results.append({"status": r.status_code, "body": r.text[:200]})
                await asyncio.sleep(0.25)
            except Exception as e:
                logger.exception("发送通知失败: %s", e)
                results.append({"error": str(e)})

    return {"ok": True, "results": results}


async def request_initdata_from_tme_url(client: TelegramClient, bot_username: str, tme_url: str, try_ios_fallback: bool = True) -> str:
    """
    直接用 t.me URL 调用 MTProto 的 RequestWebViewRequest
    例如：
      https://t.me/sdrp_bot?startapp=rp_bOTsrnJq
    返回 initData 字符串
    """
    bot_ent = await client.get_entity(bot_username)
    bot_input = InputUser(user_id=bot_ent.id, access_hash=getattr(bot_ent, 'access_hash', 0))

    tried_platforms = ["android"] + (["ios"] if try_ios_fallback else [])
    last_exc = None

    for platform in tried_platforms:
        try:
            logger.info("尝试 RequestWebViewRequest using platform=%s url=%s", platform, tme_url)

            if RequestWebViewRequest is not None:
                res = await client(RequestWebViewRequest(
                    peer=bot_ent,
                    bot=bot_input,
                    platform=platform,
                    url=tme_url,
                    theme_params=None,
                    from_bot_menu=False,
                    start_param=None,
                ))
            else:
                # 兼容某些版本的 Telethon
                from telethon.tl import functions
                res = await client(functions.messages.RequestWebViewRequest(
                    peer=bot_ent,
                    bot=bot_input,
                    platform=platform,
                    url=tme_url,
                    theme_params=None,
                    from_bot_menu=False,
                    start_param=None,
                ))

            returned_url = getattr(res, "url", None)
            logger.info("RequestWebViewRequest returned_url: %s", returned_url)
            initdata = parse_initdata_from_returned_url(returned_url)

            if initdata:
                logger.info("成功拿到 initData（len=%d）", len(initdata))
                return initdata

            raise RuntimeError(f"RequestWebViewRequest（platform={platform}）返回了 url，但没有 tgWebAppData，url={returned_url}")

        except Exception as e:
            last_exc = e
            logger.exception("platform=%s failed for RequestWebViewRequest: %s", platform, e)

    raise RuntimeError(f"All RequestWebViewRequest attempts failed for URL {tme_url}") from last_exc


async def main():
    if STRING_SESSION:
        session = StringSession(STRING_SESSION)
        client = TelegramClient(session, API_ID, API_HASH)
    else:
        client = TelegramClient("sdrp_session", API_ID, API_HASH)

    await client.start()
    logger.info("Telethon 登录成功，开始监听群 %s", TARGET_CHAT)

    @client.on(events.NewMessage(chats=TARGET_CHAT))
    async def on_new_message(event):
        msg = event.message
        logger.info("收到群消息：chat=%s msg_id=%s", event.chat_id, getattr(msg, "id", None))

        token, src = find_startapp_and_source_from_message(msg)
        if not token:
            logger.info("当前消息中没有 startapp token")
            return

        logger.info("提取到最新 startapp token: %s  source=%s", token, src)

        # 1) 如果是完整 t.me 链接，优先直接用
        if isinstance(src, str) and src.startswith("http"):
            candidate_url = src
        else:
            # 2) 如果只有 token，就构造 t.me 形式
            candidate_url = build_tme_url(BOT_USERNAME, token)

        logger.info("准备使用 t.me URL: %s", candidate_url)

        try:
            initdata = await request_initdata_from_tme_url(client, BOT_USERNAME, candidate_url, try_ios_fallback=True)
            logger.info("拿到 initData（长度=%d）", len(initdata))
            # 通知到你的通知 bot
            notify_res = await send_notification_via_bot(initdata)
            logger.info("通知发送结果: %s", notify_res)
        except Exception as e:
            logger.exception("获取或发送 initData 失败: %s", e)

    logger.info("监听器已启动，等待新消息，按 Ctrl+C 停止")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已停止")
