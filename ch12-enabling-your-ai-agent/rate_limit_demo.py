"""Sam's assistant makes eleven reads in a row and waits out the limit.

The gateway allows each person behind the assistant ten calls a minute
(Chapter 11). The eleventh gets 429 Too Many Requests with
X-RateLimit-Reset and no Retry-After, and reliable.call() waits that many
seconds, plus jitter, before trying again.

    uv run python rate_limit_demo.py
"""

import asyncio
import time

import host as support


async def main():
    sam = support.Host(person="sam", say=lambda m: print(m.strip()))
    codes = []
    start = time.monotonic()
    for n in range(1, 12):
        if n == 11:
            print(f"calls 1 to 10: {' '.join(codes)}")
            print("call 11:")
        resp = await sam.mcp("tools/call", {
            "name": "order_overview",
            "arguments": {"order_id": "ord-5002"}}, name="order_overview")
        codes.append(str(resp.status_code))
    print(f"call 11: {resp.status_code} after "
          f"{time.monotonic() - start:.0f} s in all")


asyncio.run(main())
