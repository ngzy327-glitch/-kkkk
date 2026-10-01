# main.py
import os
import asyncio
import logging
import re
import urllib.parse
import traceback
import time
from typing import Optional

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import InputUser
from telethon.tl import functions

# httpx 用于发送通知 bot
try:
    import httpx
except Exception:
    httpx = None

# Playwright
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except Exception:
    async_playwright = None
    PWTimeout = Exception

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger("sdrp-debug")

# ---------------- ENV config ----------------
API_ID = int(os.environ.get("TELETHON_API_ID", "0"))
API_HASH = os.environ.get("TELETHON_API_HASH", "")
STRING_SESSION = os.environ.get("TELETHON_STRING_SESSION", "")

BOT_USERNAME = os.environ.get("BOT_USERNAME", "sdrp_bot")
TARGET_CHAT = int(os.environ.get("TARGET_CHAT", "-1003878320419"))

NOTIFY_BOT_TOKEN = os.environ.get("NOTIFY_BOT_TOKEN", "")
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "")
NOTIFY_DEBUG = os.environ.get("NOTIFY_DEBUG", "1") in ("1", "true", "yes")

WEBAPP_URL_TEMPLATE = os.environ.get("WEBAPP_URL_TEMPLATE", "")  # 例如 "https://envelope.example.com/index.html?token={token}&bot={bot}"

PLAYWRIGHT_HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "1") in ("1", "true", "yes")
PLAYWRIGHT_TIMEOUT_MS = int(os.environ.get("PLAYWRIGHT_TIMEOUT_MS", "15000"))  # ms
# ------------------------------------------------

if not API_ID or not API_HASH:
    logger.error("请先在 Railway 环境变量中设置 TELETHON_API_ID 和 TELETHON_API_HASH")
    raise SystemExit(1)

if async_playwright is None:
    logger.warning("playwright 未安装或不可用：自动点击功能将无法运行。请确保 requirements.txt 中包含 playwright，并在容器首次启动时执行 python -m playwright install chromium")

def build_tme_url(bot_username: str, startapp_token: str) -> str:
    return f"https://t.me/{bot_username}?startapp={urllib.parse.quote(startapp_token, safe='')}"

def construct_page_url_from_src_or_token(src: Optional[str], token: str) -> Optional[str]:
    if src and isinstance(src, str) and src.startswith("http") and "t.me" not in src:
        return src
    if WEBAPP_URL_TEMPLATE:
        return WEBAPP_URL_TEMPLATE.format(token=urllib.parse.quote(token, safe=''), bot=BOT_USERNAME)
    return None

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

async def send_notification_via_bot(text: str, title: str = "debug"):
    if not NOTIFY_BOT_TOKEN or not NOTIFY_CHAT_ID:
        logger.debug("通知未配置，跳过发送")
        return {"ok": False, "error": "notify_not_configured"}
    if httpx is None:
        logger.error("httpx 未安装，无法发送通知")
        return {"ok": False, "error": "httpx_missing"}

    send_url = f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage"
    max_len = 3800
    chunks = [text[i:i+max_len] for i in range(0, len(text), max_len)]
    results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for i, chunk in enumerate(chunks, 1):
            payload = {
                "chat_id": NOTIFY_CHAT_ID,
                "text": f"{title} part {i}/{len(chunks)}\n{chunk}"
            }
            try:
                r = await client.post(send_url, data=payload)
                results.append({"status": r.status_code, "body": r.text[:200]})
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.exception("发送通知失败: %s", e)
                results.append({"error": str(e)})
    return {"ok": True, "results": results}

async def request_initdata_from_tme_url(client: TelegramClient, bot_username: str, tme_url: str, try_ios_fallback: bool = True, msg=None) -> str:
    """
    调试增强版：
    1) 先打印 msg.stringify() / reply_markup 里的 url
    2) 尝试多个 peer 候选：msg.peer_id -> bot peer -> bot user
    3) 尝试 android / ios
    4) 把所有异常输出到日志并发到通知 bot
    """
    # 1) 打印 msg 的原始结构
    try:
        if msg is not None:
            logger.info("DEBUG: msg.stringify() = \n%s", msg.stringify())
            try:
                rm = getattr(msg, "reply_markup", None)
                if rm and hasattr(rm, "rows"):
                    for r in rm.rows:
                        for b in r.buttons:
                            logger.info("DEBUG: button url=%r web_app=%r data=%r", getattr(b, "url", None), getattr(b, "web_app", None), getattr(b, "data", None))
            except Exception:
                logger.debug("DEBUG: 打印 reply_markup 失败", exc_info=True)
    except Exception:
        logger.debug("DEBUG: 打印 msg.stringify 失败", exc_info=True)

    # 2) 准备候选 peer
    candidates = []
    try:
        if msg is not None:
            peer_chat = await client.get_input_entity(msg.peer_id)
            candidates.append(("msg.peer_id", peer_chat))
    except Exception:
        logger.debug("DEBUG: 构造 msg.peer_id candidate 失败", exc_info=True)

    try:
        bot_peer = await client.get_input_entity(bot_username)
        candidates.append(("bot_peer", bot_peer))
    except Exception:
        logger.debug("DEBUG: 构造 bot_peer candidate 失败", exc_info=True)

    try:
        bot_ent = await client.get_entity(bot_username)
        bot_input_user = InputUser(user_id=bot_ent.id, access_hash=getattr(bot_ent, "access_hash", 0))
        candidates.append(("bot_input_user", bot_input_user))
    except Exception:
        logger.debug("DEBUG: 构造 bot_input_user candidate 失败", exc_info=True)

    if not candidates:
        logger.warning("没有任何 peer candidate，直接使用 bot 残缺对象")
        bot_ent = await client.get_entity(bot_username)
        bot_input_user = InputUser(user_id=bot_ent.id, access_hash=getattr(bot_ent, "access_hash", 0))
        candidates.append(("bot_input_user_fallback", bot_input_user))

    # 3) 根据平台尝试
    platforms = ["android"] + (["ios"] if try_ios_fallback else [])
    last_exc = None
    tried = []

    for peer_label, peer_value in candidates:
        for platform in platforms:
            label = f"{peer_label}|{platform}"
            tried.append(label)
            try:
                logger.info("尝试 RequestWebViewRequest with peer=%s platform=%s url=%s", peer_label, platform, tme_url)

                # bot_input_user is input User object
                if isinstance(peer_value, InputUser):
                    # 这不应该作为 peer 参数，但作为 bot 参数
                    # 更稳妥方式：peer 仍用 bot_peer candidate
                    if "bot_peer" in [x[0] for x in candidates]:
                        peer_for_req = next(v for k, v in candidates if k == "bot_peer")
                    else:
                        peer_for_req = await client.get_input_entity(bot_username)
                    bot_for_req = peer_value
                else:
                    peer_for_req = peer_value
                    bot_for_req = InputUser(user_id=(await client.get_entity(bot_username)).id,
                                            access_hash=getattr((await client.get_entity(bot_username)), "access_hash", 0))

                res = await client(functions.messages.RequestWebViewRequest(
                    peer=peer_for_req,
                    bot=bot_for_req,
                    platform=platform,
                    url=tme_url,
                    theme_params=None,
                    from_bot_menu=False,
                    start_param=None,
                ))

                logger.info("RequestWebViewRequest 返回对象: %s", repr(res)[:1200])
                returned_url = getattr(res, "url", None)
                logger.info("returned_url = %r", returned_url)

                initdata = parse_initdata_from_returned_url(returned_url)
                if initdata:
                    logger.info("成功拿到 initData（len=%d）via %s", len(initdata), label)
                    return initdata

                logger.warning("在 %s 下返回了 URL 但未解析到 tgWebAppData: %s", label, returned_url)
            except Exception as e:
                last_exc = e
                tb = traceback.format_exc()
                logger.exception("RequestWebViewRequest 失败 (label=%s): %s", label, e)
                if NOTIFY_DEBUG and NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID and httpx is not None:
                    try:
                        debug_msg = f"RequestWebViewRequest fail (label={label}) url={tme_url}\n{tb}"
                        await send_notification_via_bot(debug_msg, title="DEBUG: RequestWebViewRequest fail")
                    except Exception:
                        logger.debug("DEBUG 通知发送失败", exc_info=True)

    # all failed
    raise RuntimeError({"error": "all_candidates_failed", "tried": tried, "last_exc": repr(last_exc)}) from last_exc


# Playwright click logic
async def playwright_open_and_click(page_url: str, initdata: str, timeout_ms: int = PLAYWRIGHT_TIMEOUT_MS) -> dict:
    if async_playwright is None:
        return {"ok": False, "failure_reason": "playwright_not_installed"}

    enc = urllib.parse.quote(initdata, safe='')
    if "#" in page_url:
        full_url = page_url + "&tgWebAppData=" + enc
    else:
        full_url = page_url + "#tgWebAppData=" + enc

    logger.info("Playwright 打开页面：%s", full_url[:300])

    t_start = time.monotonic_ns()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=PLAYWRIGHT_HEADLESS, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
        )
        page = await context.new_page()

        try:
            await page.goto(full_url, wait_until="networkidle", timeout=timeout_ms)
        except Exception:
            logger.debug("页面导航可能超时或异常", exc_info=True)

        selectors = [
            {"sel": "text=去游玩", "method": "text"},
            {"sel": "button:has-text('去游玩')", "method": "css"},
            {"sel": "//button[contains(., '去游玩')]", "method": "xpath"},
            {"sel": "//a[contains(., '去游玩')]", "method": "xpath"}
        ]

        attempts = []
        failure_reason = None
        selected = None
        detect_ms = None
        click_ms = None

        for item in selectors:
            sel = item["sel"]
            method = item["method"]
            try:
                await page.wait_for_selector(sel, timeout=3000)
                t_found = time.monotonic_ns()
                detect_ms = (t_found - t_start) // 1_000_000
                attempts.append({"selector": sel, "method": method, "status": "found", "detect_ms": detect_ms})

                t_click_start = time.monotonic_ns()
                await page.click(sel, timeout=5000)
                t_click_end = time.monotonic_ns()
                click_ms = (t_click_end - t_click_start) // 1_000_000
                attempts[-1]["click_ms"] = click_ms
                selected = {"selector": sel, "method": method}
                # 允许网络停止
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                t_end = time.monotonic_ns()
                total_ms = (t_end - t_start) // 1_000_000
                try:
                    content_snip = await page.content()
                except Exception:
                    content_snip = "<no page content>"
                await context.close()
                await browser.close()
                return {
                    "ok": True,
                    "detection_method": method,
                    "selector_used": sel,
                    "failure_reason": None,
                    "timings_ms": {"detect_ms": detect_ms, "click_ms": click_ms, "total_ms": total_ms},
                    "debug": {"attempts": attempts},
                    "content_snip": content_snip[:2000]
                }
            except PWTimeout:
                attempts.append({"selector": sel, "method": method, "status": "timeout"})
            except Exception as e:
                attempts.append({"selector": sel, "method": method, "status": f"error:{e}"})
                failure_reason = f"click_error:{e}"

        t_final = time.monotonic_ns()
        total_ms = (t_final - t_start) // 1_000_000
        await context.close()
        await browser.close()
        return {
            "ok": False,
            "detection_method": None,
            "selector_used": None,
            "failure_reason": failure_reason or "no_selector_matched",
            "timings_ms": {"detect_ms": detect_ms, "click_ms": click_ms, "total_ms": total_ms},
            "debug": {"attempts": attempts},
            "content_snip": None
        }

# ---- main flow ----
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
        logger.info("提取到 startapp token: %s  source=%s", token, src)

        tme_url = build_tme_url(BOT_USERNAME, token)

        try:
            initdata = await request_initdata_from_tme_url(client, BOT_USERNAME, tme_url, try_ios_fallback=True, msg=msg)
            logger.info("成功拿到 initData（len=%d）", len(initdata))
            if NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID and httpx is not None:
                await send_notification_via_bot(initdata, title="initData (obtained)")
        except Exception as e:
            tb = traceback.format_exc()
            logger.exception("获取 initData 失败: %s", e)
            if NOTIFY_DEBUG and NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID and httpx is not None:
                await send_notification_via_bot(f"ERROR fetching initData:\n{tb}", title="DEBUG: initdata failure")
            return

        # 构造页面 URL：如果消息中 src 是第三方页面，就直接用；否则用模板
        page_base = construct_page_url_from_src_or_token(src, token)
        if not page_base:
            error_msg = "无法构造 webapp 页面 URL（因为消息中未提供第三方 page URL，且未设置 WEBAPP_URL_TEMPLATE）"
            logger.error(error_msg)
            if NOTIFY_DEBUG and NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID and httpx is not None:
                await send_notification_via_bot(error_msg, title="ERROR: missing WebApp URL")
            return

        try:
            click_report = await playwright_open_and_click(page_base, initdata, timeout_ms=PLAYWRIGHT_TIMEOUT_MS)
            # 发送 AutoClick Report
            report_text = (
                f"AutoClick Report\n"
                f"chat={event.chat_id} msg_id={getattr(msg, 'id', None)}\n"
                f"token={token}\n"
                f"page={page_base}\n"
                f"click_ok={click_report.get('ok')}\n"
                f"failure_reason={click_report.get('failure_reason')}\n"
                f"detection_method={click_report.get('detection_method')}\n"
                f"selector_used={click_report.get('selector_used')}\n"
                f"timings_ms={click_report.get('timings_ms')}\n"
                f"debug={click_report.get('debug')}\n"
            )
            logger.info(report_text)
            if NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID and httpx is not None:
                await send_notification_via_bot(report_text, title="AutoClick Report")
        except Exception as e:
            tb = traceback.format_exc()
            logger.exception("Playwright 打开或点击失败: %s", e)
            if NOTIFY_DEBUG and NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID and httpx is not None:
                await send_notification_via_bot(f"Playwright exception:\n{tb}", title="DEBUG: playwright exception")

    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已停止")
