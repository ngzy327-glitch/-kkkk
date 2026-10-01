import asyncio
import httpx
import time

INIT_URL = "https://example.webapp.backend/init"   # 抓包到的 init 接口
CLAIM_URL = "https://example.webapp.backend/claim" # 抓包到的 claim 接口

async def do_claim_with_initdata(initdata: str):
    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        t0 = time.time()
        # 第一步：把 initData 发到 init 接口（或页面要求的 endpoint）
        headers = {
            "User-Agent": "Mozilla/5.0 ...",           # 按抓包填
            "Accept": "application/json",
            "Content-Type": "application/json",
            # "Referer": "...", "Origin": "..." 若抓包需这些头则加上
        }
        payload = {"initData": initdata}  # 按抓包的 body 结构填
        r1 = await client.post(INIT_URL, json=payload, headers=headers)
        r1.raise_for_status()
        # 如果服务器在 Set-Cookie 中返回 session，则 client.cookies 已保存可复用
        j1 = r1.json()  # 按实际返回解析
        t1 = time.time()

        # 第二步：用返回的 token/session 去调用 claim（可能在同一 client）
        # 例如：如果返回包含 session_token
        session_token = j1.get("session_token")  # 按抓包字段
        claim_headers = headers.copy()
        if session_token:
            claim_headers["Authorization"] = f"Bearer {session_token}"
        # 或者直接复用 client.cookies，如果后端通过 cookie 识别会话

        claim_payload = {"action": "claim"}  # 按实际 body 填
        r2 = await client.post(CLAIM_URL, json=claim_payload, headers=claim_headers)
        r2.raise_for_status()
        j2 = r2.json()
        t2 = time.time()

        return {"init_resp": j1, "claim_resp": j2, "timings": (t1-t0, t2-t1)}

# 运行例子
# asyncio.run(do_claim_with_initdata("你的initData"))
