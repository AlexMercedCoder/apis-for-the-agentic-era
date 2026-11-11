"""Retries with a deadline, jittered backoff, and the gateway's reset.

`send(timeout)` makes one attempt and returns an httpx.Response. The
caller makes the attempt safe to repeat: a write carries the same
Idempotency-Key on every attempt, so a retry after a lost response
returns the first result instead of acting twice.
"""

import asyncio
import random
import time

import httpx

RETRY = {429, 500, 502, 503, 504}
IN_PROGRESS = "https://harbor.example/problems/request-in-progress"


class GaveUp(Exception):
    pass


def wait_for(resp, attempt, base=0.25, cap=4.0):
    reset = resp is not None and resp.status_code == 429 and (
        resp.headers.get("retry-after")
        or resp.headers.get("x-ratelimit-reset"))
    if reset:
        return float(reset) + random.uniform(0, 0.5)
    return random.uniform(0, min(cap, base * 2 ** attempt))


def busy(resp):
    return (resp.status_code == 409 and resp.headers.get(
        "content-type", "").endswith("problem+json")
        and resp.json().get("type") == IN_PROGRESS)


async def call(send, deadline=10.0, per_try=1.0, tries=4, log=print):
    start = time.monotonic()
    for attempt in range(1, tries + 1):
        left = deadline - (time.monotonic() - start)
        resp = None
        try:
            resp = await send(min(per_try, left))
            if resp.status_code not in RETRY and not busy(resp):
                return resp
            what = str(resp.status_code)
        except httpx.TimeoutException:
            what = f"timed out after {min(per_try, left):.1f} s"
        pause = wait_for(resp, attempt)
        left = deadline - (time.monotonic() - start)
        if attempt == tries or pause >= left:
            raise GaveUp(f"attempt {attempt}: {what}")
        log(f"attempt {attempt}: {what}, retry in {pause:.1f} s")
        await asyncio.sleep(pause)


class Breaker:
    """Stop calling an upstream that keeps failing, then try one call."""

    def __init__(self, failures=5, cool_off=30.0):
        self.failures, self.cool_off = failures, cool_off
        self.count, self.opened = 0, None

    def allow(self):
        if self.opened is None:
            return True
        return time.monotonic() - self.opened >= self.cool_off

    def record(self, ok):
        if ok:
            self.count, self.opened = 0, None
            return
        self.count += 1
        if self.count >= self.failures:
            self.opened = time.monotonic()
