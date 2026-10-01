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

# telethon RequestWebView compatibility
try:
    from telethon.tl.functions.messages import RequestWebViewRequest
except Exception:
    RequestWebViewRequest = None

# httpx 用于发送通知 bot
try:
    import httpx
except Exception:
    httpx = None

# Playwright async API
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except Exception:
    async_playwright = None
    PWTimeout = Exception

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger("sdrp-autoclick")

# ---------------- ENV config ----------------
API_ID = int(os.environ.get("TELETHON_API_ID", "0"))
API_HASH = os.environ.get("TELETHON_API_HASH", "")
STRING_SESSION = os.environ.get("TELETHON_STRING_SESSION", "")

BOT_USERNAME = os.environ.get("BOT_USERNAME", "sdrp_bot")
TARGET_CHAT = int(os.environ.get("TARGET_CHAT", "-1003878320419"))

NOTIFY_BOT_TOKEN = os.environ.get("NOTIFY_BOT_TOKEN", "")
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "")
NOTIFY_DEBUG = os.environ.get("NOTIFY_DEBUG", "1") in ("1", "true", "yes")

# 如果消息只给了 t.me 链接或只有 token，而你需要把 initData 注入到真实 webapp 页面（envelope），
# 可在此处设置一个模板，例如:
# "https://envelope.kpaye.com/uploads/envelope/index.html?token={token}&bot={bot}"
WEBAPP_URL_TEMPLATE = os.environ.get("WEBAPP_URL_TEMPLATE", "")  # optional; used when src is t.me or only token

# Playwright config
PLAYWRIGHT_HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "1") in ("1", "true", "yes")
PLAYWRIGHT_TIMEOUT_MS = int(os.environ.get("PLAYWRIGHT_TIMEOUT_MS", "15000"))  # navigation / selector timeout
# ------------------------------------------------

if not API_ID or not API_HASH:
    logger.error("请先在环境变量中设置 TELETHON_API_ID 和 TELETHON_API_HASH")
    raise SystemExit(1)

if async_playwright is None:
    logger.warning("playwright 未安装或不可用：自动点击功能将无法运行。请在 requirements.txt 中加入 playwright 并在启动前运行 'playwright install chromium'。")

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

# Playwright: 打开页面并点击含文本 "去游玩" 的按钮，返回详细时序与结果
async def playwright_open_and_click(page_url: str, initdata: str, timeout_ms: int = PLAYWRIGHT_TIMEOUT_MS) -> dict:
    if async_playwright is None:
        return {"ok": False, "failure_reason": "playwright_not_installed"}

    # Prepare URL with fragment tgWebAppData
    enc = urllib.parse.quote(initdata, safe='')
    if "#" in page_url:
        full_url = page_url + "&tgWebAppData=" + enc
    else:
        full_url = page_url + "#tgWebAppData=" + enc

    logger.info("Playwright 打开页面 (len %d): %s", len(full_url), full_url[:300])

    # timings in ms using monotonic_ns
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
        except Exception as e:
            logger.debug("页面导航异常 (可能超时)：%s", e)

        # selectors to try (记录尝试顺序)
        selectors = [
            {"sel": "text=去游玩", "method": "text"},
            {"sel": "button:has-text('去游玩')", "method": "css"},
            {"sel": "//button[contains(., '去游玩')]", "method": "xpath"},
            {"sel": "//a[contains(., '去游玩')]", "method": "xpath"}
        ]

        selected = None
        detect_ms = None
        click_ms = None
        failure_reason = None
        debug_attempts = []

        # detection phase
        for s in selectors:
            sel = s["sel"]
            method = s["method"]
            t_before_wait = time.monotonic_ns()
            try:
                await page.wait_for_selector(sel, timeout=3000)
                t_found = time.monotonic_ns()
                # found -> detection time is t_found - t_start
                detect_ms = (t_found - t_start) // 1_000_000
                selected = {"selector": sel, "method": method}
                debug_attempts.append({"selector": sel, "method": method, "status": "found", "detect_ms": detect_ms})
                # attempt click
                t_click_start = time.monotonic_ns()
                await page.click(sel, timeout=5000)
                t_click_end = time.monotonic_ns()
                click_ms = (t_click_end - t_click_start) // 1_000_000
                debug_attempts[-1].update({"click_ms": click_ms, "click_status": "ok"})
                # wait short for network / DOM update
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    # not fatal; still consider click attempt done
                    pass
                t_end = time.monotonic_ns()
                total_ms = (t_end - t_start) // 1_000_000
                # capture content snip for debugging
                try:
                    content_snip = await page.content()
                except Exception:
                    content_snip = "<no content>"
                await context.close()
                await browser.close()
                return {
                    "ok": True,
                    "detection_method": method,
                    "selector_used": sel,
                    "failure_reason": None,
                    "timings_ms": {"detect_ms": detect_ms, "click_ms": click_ms, "total_ms": total_ms},
                    "debug": {"attempts": debug_attempts},
                    "content_snip": content_snip[:2000]
                }
            except PWTimeout:
                t_after = time.monotonic_ns()
                dt_ms = (t_after - t_start) // 1_000_000
                debug_attempts.append({"selector": sel, "method": method, "status": "timeout", "elapsed_ms": dt_ms})
                continue
            except Exception as e:
                t_err = time.monotonic_ns()
                dt_ms = (t_err - t_start) // 1_000_000
                debug_attempts.append({"selector": sel, "method": method, "status": f"error:{e}", "elapsed_ms": dt_ms})
                failure_reason = f"click_error:{e}"
                # continue trying other selectors
                continue

        # if reached here, no selector worked
        t_final = time.monotonic_ns()
        total_ms = (t_final - t_start) // 1_000_000
        await context.close()
        await browser.close()
        return {
            "ok": False,
            "detection_method": None,
            "selector_used": None,
            "failure_reason": failure_reason or "no_selector_matched",
            "timings_ms": {"detect_ms": detect_ms or None, "click_ms": click_ms or None, "total_ms": total_ms},
            "debug": {"attempts": debug_attempts},
            "content_snip": None
        }

# simplified RequestWebViewRequest wrapper (you already had working code)
async def request_initdata_from_tme_url(client: TelegramClient, bot_username: str, tme_url: str, try_ios_fallback: bool = True, msg=None) -> str:
    bot_ent = await client.get_entity(bot_username)
    bot_input = InputUser(user_id=bot_ent.id, access_hash=getattr(bot_ent, "access_hash", 0))
    last_exc = None
    platforms = ["android"] + (["ios"] if try_ios_fallback else [])
    from telethon.tl import functions
    for platform in platforms:
        try:
            if RequestWebViewRequest is not None:
                res = await client(RequestWebViewRequest(
                    peer=await client.get_input_entity(bot_username),
                    bot=bot_input,
                    platform=platform,
                    url=tme_url,
                    theme_params=None,
                    from_bot_menu=False,
                    start_param=None,
                ))
            else:
                res = await client(functions.messages.RequestWebViewRequest(
                    peer=await client.get_input_entity(bot_username),
                    bot=bot_input,
                    platform=platform,
                    url=tme_url,
                    theme_params=None,
                    from_bot_menu=False,
                    start_param=None,
                ))
            returned_url = getattr(res, "url", None)
            initdata = parse_initdata_from_returned_url(returned_url)
            if initdata:
                return initdata
        except Exception as e:
            last_exc = e
            logger.exception("RequestWebViewRequest(platform=%s) failed: %s", platform, e)
            continue
    raise RuntimeError(f"All RequestWebViewRequest attempts failed for URL {tme_url}") from last_exc

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
        logger.info("使用 t.me URL 请求 initData: %s", tme_url)

        try:
            initdata = await request_initdata_from_tme_url(client, BOT_USERNAME, tme_url, try_ios_fallback=True, msg=msg)
            logger.info("拿到 initData (len=%d)", len(initdata))
            # 先把 initData 发给通知 bot（记录）
            try:
                await send_notification_via_bot(initdata, title="initData (obtained)")
            except Exception:
                logger.debug("发送 initData 到通知 bot 失败（非致命）")

            # 构造页面 URL（优先使用原始 src 若为第三方页面，否则用 WEBAPP_URL_TEMPLATE）
            page_base = construct_page_url_from_src_or_token(src, token)
            if not page_base:
                msg_err = "无法构造 webapp 页面 URL：请在环境变量 WEBAPP_URL_TEMPLATE 中设置模板或确保消息中包含第三方 webapp URL"
                logger.error(msg_err)
                if NOTIFY_DEBUG and NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID and httpx is not None:
                    await send_notification_via_bot(msg_err, title="ERROR: page url missing")
                return

            # 执行 playwright 打开并点击，获取详细报告
            try:
                click_report = await playwright_open_and_click(page_base, initdata, timeout_ms=PLAYWRIGHT_TIMEOUT_MS)
            except Exception as e:
                tb = traceback.format_exc()
                logger.exception("Playwright 打开或点击发生异常: %s", e)
                click_report = {"ok": False, "failure_reason": f"playwright_exception:{e}", "debug": {"trace": tb}}

            # 汇总报告
            report = {
                "chat_id": event.chat_id,
                "message_id": getattr(msg, "id", None),
                "token": token,
                "page_base": page_base,
                "click_ok": bool(click_report.get("ok")),
                "failure_reason": click_report.get("failure_reason"),
                "detection_method": click_report.get("detection_method"),
                "selector_used": click_report.get("selector_used"),
                "timings_ms": click_report.get("timings_ms"),
                "debug": click_report.get("debug")
            }

            # 发送报告到通知 bot（结构化文本）
            report_text = (
                f"AutoClick Report\n"
                f"chat: {report['chat_id']} msg_id: {report['message_id']}\n"
                f"token: {report['token']}\n"
                f"page: {report['page_base']}\n"
                f"click_ok: {report['click_ok']}\n"
                f"failure_reason: {report['failure_reason']}\n"
                f"detection_method: {report['detection_method']}\n"
                f"selector_used: {report['selector_used']}\n"
                f"timings_ms: {report['timings_ms']}\n"
                f"debug_attempts: {report['debug']}\n"
            )

            if NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID and httpx is not None:
                await send_notification_via_bot(report_text, title="AutoClick Report")
            else:
                logger.info("AutoClick Report:\n%s", report_text)

        except Exception as e:
            tb = traceback.format_exc()
            logger.exception("获取或处理 initData 失败: %s", e)
            if NOTIFY_DEBUG and NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID and httpx is not None:
                await send_notification_via_bot(f"ERROR fetching initData or handling page:\n{tb}", title="DEBUG: initdata failure")

    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已停止")
