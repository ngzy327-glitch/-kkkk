# main.py
import os
import asyncio
import logging
import re
import urllib.parse
import traceback
from typing import Optional, Tuple

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import InputUser

# 兼容不同 Telethon 版本
try:
    from telethon.tl.functions.messages import RequestWebViewRequest, GetBotCallbackAnswerRequest
except Exception:
    RequestWebViewRequest = None
    GetBotCallbackAnswerRequest = None

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

# 是否把详细调试（包括异常/返回对象）发到通知 bot（True/False）
NOTIFY_DEBUG = os.environ.get("NOTIFY_DEBUG", "1") in ("1", "true", "yes")

if not API_ID or not API_HASH:
    logger.error("请在 Railway 环境变量中设置 TELETHON_API_ID 和 TELETHON_API_HASH")
    raise SystemExit(1)

if httpx is None:
    logger.warning("httpx 未安装，通知功能将不可用。请确保 requirements.txt 中包含 httpx")


def build_tme_url(bot_username: str, startapp_token: str) -> str:
    return f"https://t.me/{bot_username}?startapp={urllib.parse.quote(startapp_token, safe='')}"


def find_startapp_and_source_from_message(msg):
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


def parse_initdata_from_returned_url(returned_url: Optional[str]) -> Optional[str]:
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


async def send_notification_via_bot(initdata: str, title: str = "initData"):
    if not NOTIFY_BOT_TOKEN or not NOTIFY_CHAT_ID:
        logger.debug("通知未配置，跳过发送")
        return {"ok": False, "error": "notify_not_configured"}
    if httpx is None:
        logger.error("httpx 未安装，无法发送通知")
        return {"ok": False, "error": "httpx_missing"}

    send_url = f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage"
    max_len = 3800
    chunks = [initdata[i:i+max_len] for i in range(0, len(initdata), max_len)]
    results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for i, chunk in enumerate(chunks, 1):
            text = f"{title} part {i}/{len(chunks)}\n{chunk}"
            try:
                r = await client.post(send_url, data={"chat_id": NOTIFY_CHAT_ID, "text": text})
                results.append({"status": r.status_code, "body": r.text[:200]})
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.exception("发送通知失败: %s", e)
                results.append({"error": str(e)})
    return {"ok": True, "results": results}


async def request_initdata_from_tme_url(client: TelegramClient, bot_username: str, tme_url: str,
                                        try_ios_fallback: bool = True, msg=None) -> str:
    """
    强化版：使用 InputPeer (get_input_entity) 作为 peer 参数，并在失败时尝试 GetBotCallbackAnswer（若 msg 可用）。
    返回 initData 或抛异常。
    """
    # 获取 peer (InputPeerUser) 与 bot (InputUser)
    peer_input = None
    bot_input = None
    try:
        peer_input = await client.get_input_entity(bot_username)  # InputPeerUser
        bot_ent = await client.get_entity(bot_username)  # User
        bot_input = InputUser(user_id=bot_ent.id, access_hash=getattr(bot_ent, 'access_hash', 0))
    except Exception as e:
        logger.exception("获取实体/peer 失败: %s", e)
        # 继续，后续调用可能仍用 bot_ent 等

    tried_platforms = ["android"] + (["ios"] if try_ios_fallback else [])
    last_exc = None

    for platform in tried_platforms:
        try:
            logger.info("尝试 RequestWebViewRequest (platform=%s) url=%s", platform, tme_url)
            # 优先用 telethon 提供的方法（如果存在）
            if RequestWebViewRequest is not None and peer_input is not None and bot_input is not None:
                res = await client(RequestWebViewRequest(
                    peer=peer_input,
                    bot=bot_input,
                    platform=platform,
                    url=tme_url,
                    theme_params=None,
                    from_bot_menu=False,
                    start_param=None,
                ))
            else:
                # raw invoke fallback，注意参数类型需正确
                from telethon.tl import functions
                # 使用 peer_input if available else try bot_input / bot_ent
                peer_arg = peer_input if peer_input is not None else (await client.get_entity(bot_username))
                res = await client(functions.messages.RequestWebViewRequest(
                    peer=peer_arg,
                    bot=bot_input if bot_input is not None else (await client.get_entity(bot_username)),
                    platform=platform,
                    url=tme_url,
                    theme_params=None,
                    from_bot_menu=False,
                    start_param=None,
                ))

            # 打印/上报返回对象以便调试
            try:
                logger.debug("RequestWebViewRequest raw res repr: %s", repr(res)[:800])
                if NOTIFY_DEBUG:
                    await send_notification_via_bot("RequestWebViewRequest raw repr:\n" + repr(res)[:3000], title="debug: raw res")
            except Exception:
                logger.debug("发送 raw res debug 失败，不影响主流程")

            returned_url = getattr(res, "url", None)
            logger.info("RequestWebViewRequest returned_url: %s", returned_url)
            initdata = parse_initdata_from_returned_url(returned_url)
            if initdata:
                return initdata
            else:
                logger.warning("platform=%s 返回了 url 但未包含 tgWebAppData，url=%s", platform, returned_url)
                last_exc = RuntimeError(f"No tgWebAppData in returned url for platform={platform}: {returned_url}")
        except Exception as e:
            last_exc = e
            tb = traceback.format_exc()
            logger.exception("RequestWebViewRequest(platform=%s) 调用失败: %s", platform, e)
            if NOTIFY_DEBUG:
                try:
                    await send_notification_via_bot(f"RequestWebViewRequest exception (platform={platform}):\n{tb}", title="debug: RequestWebViewRequest exception")
                except Exception:
                    pass
            # 继续尝试下一个平台

    # 如果所有平台都失败，尝试通过 GetBotCallbackAnswer（仅当 msg 可用）获取实际跳转 URL
    if msg is not None and GetBotCallbackAnswerRequest is not None:
        try:
            msg_id = getattr(msg, "id", None)
            if msg_id:
                logger.info("尝试通过 GetBotCallbackAnswerRequest 获取 callback answer（msg_id=%s）", msg_id)
                from telethon.tl import functions
                try:
                    g = await client(functions.messages.GetBotCallbackAnswerRequest(
                        peer=await client.get_input_entity(bot_username),
                        msg_id=msg_id,
                        data=b""
                    ))
                    # 尝试从返回中解析 URL 或文本
                    logger.debug("GetBotCallbackAnswer raw repr: %s", repr(g)[:800])
                    text = ""
                    if hasattr(g, "message") and g.message:
                        text = str(g.message)
                    elif hasattr(g, "text") and g.text:
                        text = str(g.text)
                    else:
                        text = repr(g)

                    # 从文本中查找 url
                    import re
                    m = re.search(r"https?://[^\s'\"<>]+", text)
                    if m:
                        found = m.group(0)
                        logger.info("从 GetBotCallbackAnswer 返回中发现 URL: %s", found)
                        # 尝试用该 URL 再次 RequestWebViewRequest
                        try:
                            return await request_initdata_from_tme_url(client, bot_username, found, try_ios_fallback=True, msg=None)
                        except Exception as e2:
                            logger.exception("用从 GetBotCallbackAnswer 得到的 URL 再次请求失败: %s", e2)
                except Exception as e:
                    logger.exception("GetBotCallbackAnswerRequest 调用失败: %s", e)
        except Exception:
            logger.exception("尝试 GetBotCallbackAnswer 流程异常")

    # 全部尝试失败，抛出最后一个异常
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

        # 构造 candidate t.me URL（优先使用消息中原 URL）
        if isinstance(src, str) and src.startswith("http"):
            candidate_url = src
        else:
            candidate_url = build_tme_url(BOT_USERNAME, token)

        logger.info("准备使用 URL: %s", candidate_url)
        try:
            initdata = await request_initdata_from_tme_url(client, BOT_USERNAME, candidate_url, try_ios_fallback=True, msg=msg)
            logger.info("成功拿到 initData（len=%d）", len(initdata))
            # 发送到通知 bot（可分片）
            notify_res = await send_notification_via_bot(initdata, title="initData")
            logger.info("通知发送结果: %s", notify_res)
        except Exception as e:
            tb = traceback.format_exc()
            logger.exception("获取或发送 initData 失败: %s", e)
            # 把调试信息发到通知 bot（如果可用）
            if NOTIFY_DEBUG and NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID and httpx is not None:
                try:
                    await send_notification_via_bot(f"ERROR fetching initData for URL {candidate_url}:\n{tb}", title="DEBUG: RequestWebViewRequest Failure")
                except Exception:
                    logger.exception("发送调试通知失败")

    logger.info("监听器已启动")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已停止")
