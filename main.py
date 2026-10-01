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

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger("sdyl-initdata")

# ---------- 配置（从环境变量读取） ----------
API_ID = int(os.environ.get("TELETHON_API_ID", "0"))
API_HASH = os.environ.get("TELETHON_API_HASH", "")
STRING_SESSION = os.environ.get("TELETHON_STRING_SESSION", "")  # 推荐预先生成并设置
BOT_USERNAME = os.environ.get("BOT_USERNAME", "sdyl")  # 默认为 sdyl
TARGET_CHAT = int(os.environ.get("TARGET_CHAT", "-1003878320419"))  # 你的群 id
# ------------------------------------------------

if not API_ID or not API_HASH:
    logger.error("请在环境变量中设置 TELETHON_API_ID 和 TELETHON_API_HASH")
    raise SystemExit(1)

if not STRING_SESSION:
    logger.warning("未检测到 TELETHON_STRING_SESSION 环境变量。建议先在本地生成 StringSession 并设置为环境变量。")

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

    # 高层 buttons
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

    # reply_markup
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

    # 文本中 URL
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

async def request_initdata_via_mtproto(client: TelegramClient, bot_username: str, webview_url: str) -> str:
    bot_ent = await client.get_entity(bot_username)
    bot_input = InputUser(user_id=bot_ent.id, access_hash=getattr(bot_ent, 'access_hash', 0))

    logger.info("调用 RequestWebViewRequest，请求 URL: %s", webview_url)
    # 如果 telethon 内置方法可用，则使用它；否则通过 raw invoke
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
            # raw invoke fallback (函数名可能随 schema 不同)
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
        raise RuntimeError("RequestWebViewRequest 未返回 url 字段，无法取得 tgWebAppData")

    logger.info("MTProto 返回 URL：%s", returned_url)
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
    # 使用 StringSession（推荐）
    if STRING_SESSION:
        session = StringSession(STRING_SESSION)
        client = TelegramClient(session, API_ID, API_HASH)
    else:
        # 回退到无 session 文件模式（容器内会生成 .session 文件，短期可用）
        client = TelegramClient("sdyl_session", API_ID, API_HASH)

    await client.start()
    logger.info("Telethon 登录完成，开始监听群 %s 的新消息", TARGET_CHAT)

    @client.on(events.NewMessage(chats=TARGET_CHAT))
    async def on_new_message(event):
        msg = event.message
        logger.info("收到新消息：chat=%s msg_id=%s", event.chat_id, getattr(msg, "id", None))

        token, src = find_startapp_and_url_from_message(msg)
        if not token:
            logger.info("未发现 startapp token")
            return

        logger.info("提取到 startapp token=%s 来源=%s", token, src)

        if isinstance(src, str) and src.startswith("http"):
            webview_url = src
        else:
            logger.warning("未找到完整 webview URL，无法用 RequestWebViewRequest（src=%s）", src)
            return

        try:
            initdata = await request_initdata_via_mtproto(client, BOT_USERNAME, webview_url)
            logger.info("成功获取 initData（长度 %d）: %s", len(initdata), initdata)
            # TODO: 这里拿到 initData 后请立即使用（例如调用 webapp 后端接口）
        except Exception as e:
            logger.exception("获取 initData 失败: %s", e)

    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("已停止")