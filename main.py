import urllib.parse
from telethon.tl import functions, types

async def request_initdata_with_fallback(client, bot_username, webview_src, msg=None):
    """
    尝试多种方式通过 MTProto 拿到官方签发的 initData。
    - webview_src: 优先为按钮里的完整 URL，如果只有 start_param 则也可传入 start_param
    - msg: 原始 Telethon message（可选），仅用于调试/在必要时调用 getBotCallbackAnswer
    返回 initdata 字符串（已 URL-decoded）或抛出异常。
    """
    bot_ent = await client.get_entity(bot_username)
    bot_input = InputUser(user_id=bot_ent.id, access_hash=getattr(bot_ent, 'access_hash', 0))

    # 辅助：解析 res.url 得到 initData
    def parse_initdata_from_returned_url(returned_url):
        if not returned_url:
            return None
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
            return None
        return urllib.parse.unquote(initdata_enc)

    # 1) 如果传入的是完整 URL（以 http 开头），优先尝试直接调用 RequestWebViewRequest
    tried_urls = []
    exceptions = []
    candidates = []

    if webview_src and isinstance(webview_src, str) and webview_src.startswith("http"):
        candidates.append(("orig_url", webview_src))

    # 2) 如果 webview_src 看起来像 start_param（不以 http 开头），尝试构造 t.me 链接
    if webview_src and isinstance(webview_src, str) and not webview_src.startswith("http"):
        tme = f"https://t.me/{bot_username}?startapp={urllib.parse.quote(webview_src, safe='')}"
        candidates.append(("tme_startapp", tme))

    # 3) 如果 msg 可用且消息里有 callback_data，我们可以尝试 getBotCallbackAnswer 来获得 URL
    if msg is not None:
        try:
            # 取得 msg id
            msg_id = getattr(msg, "id", None)
            if msg_id:
                # try getBotCallbackAnswer; this may return url/text
                logger.debug("尝试 messages.GetBotCallbackAnswer 获取 callback answer（若存在）")
                try:
                    g = await client(functions.messages.GetBotCallbackAnswerRequest(peer=await client.get_entity(bot_username),
                                                                                   msg_id=msg_id,
                                                                                   game=False,
                                                                                   data=b""))
                    # g 结构视库版本不同。尝试从返回中提取 url/text
                    # 如果返回包含 url 字段（如 g.url）或 message.text 含 url，加入候选
                    ret_repr = repr(g)
                    logger.debug("GetBotCallbackAnswer 返回: %s", ret_repr)
                    # attempt parse url from returned text fields
                    # fallback: inspect attributes
                    url_candidate = None
                    if hasattr(g, "url") and g.url:
                        url_candidate = g.url
                    else:
                        # try extract url from text/message
                        txt = getattr(g, "message", None) or getattr(g, "text", None) or ""
                        import re
                        m = re.search(r"https?://[^\s]+", txt)
                        if m:
                            url_candidate = m.group(0)
                    if url_candidate:
                        candidates.append(("callback_answer_url", url_candidate))
                except Exception as e:
                    logger.debug("GetBotCallbackAnswer 请求失败或不可用: %s", e)
        except Exception as e:
            logger.debug("尝试使用 msg 调用 GetBotCallbackAnswer 出错: %s", e)

    # 如果没有任何候选，抛错
    if not candidates:
        raise RuntimeError("没有可用的 URL 候选（既不是完整 URL，亦无 start_param 或 callback 回应）。请检查消息结构并传入正确的 URL 或 msg 对象以便尝试 getBotCallbackAnswer。")

    # 逐个候选尝试 RequestWebViewRequest（或 raw invoke）
    for label, url_candidate in candidates:
        tried_urls.append((label, url_candidate))
        try:
            logger.info("尝试用 RequestWebViewRequest（%s）: %s", label, url_candidate)
            if RequestWebViewRequest is not None:
                res = await client(RequestWebViewRequest(
                    peer=bot_ent,
                    bot=bot_input,
                    platform='android',
                    url=url_candidate,
                    theme_params=None,
                    from_bot_menu=False,
                    start_param=None,
                ))
            else:
                res = await client(functions.messages.RequestWebViewRequest(
                    peer=bot_ent,
                    bot=bot_input,
                    platform='android',
                    url=url_candidate,
                    theme_params=None,
                    from_bot_menu=False,
                    start_param=None,
                ))
            returned_url = getattr(res, "url", None)
            logger.debug("RequestWebViewRequest 返回: %r", returned_url)
            initdata = parse_initdata_from_returned_url(returned_url)
            if initdata:
                logger.info("从返回 URL (%s) 成功解析到 initData（len=%d）", label, len(initdata))
                return initdata
            else:
                logger.warning("从返回 URL (%s) 未能解析到 tgWebAppData，返回 URL=%r", label, returned_url)
        except Exception as e:
            logger.exception("RequestWebViewRequest(%s) 失败: %s", label, e)
            exceptions.append((label, str(e)))
            # 如果是明确的 UrlInvalidError，继续尝试下一个候选

    # 所有候选尝试完毕仍失败
    raise RuntimeError({
        "error": "all_attempts_failed",
        "tried": tried_urls,
        "exceptions": exceptions
    })
