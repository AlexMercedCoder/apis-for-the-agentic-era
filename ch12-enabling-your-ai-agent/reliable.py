"""Retries with a deadline, jittered backoff, and the gateway's reset.

`send(timeout)` makes one attempt and returns an httpx.Response. The
caller makes the attempt safe to repeat: a write carries the same
Idempotency-Key on every attempt, so a retry after a lost response
returns the first result instead of acting twice.
"""

import asyncio
import random
import time
from email.utils import parsedate_to_datetime

import httpx

RETRY = {429, 500, 502, 503, 504}
IN_PROGRESS = "https://harbor.example/problems/request-in-progress"


class GaveUp(Exception):
    pass


def wait_for(resp, attempt, base=0.25, cap=4.0):
    reset = resp is not None and resp.status_code == 429 and (
        resp.headers.get("retry-after")
        or resp.headers.get("x-ratelimit-reset"))
    if reset and seconds(reset) is not None:
        return seconds(reset) + random.uniform(0, 0.5)
    return random.uniform(0, min(cap, base * 2 ** attempt))


def seconds(value):
    """Retry-After is seconds or an HTTP date (RFC 9110, 10.2.3)."""
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None                                  # unreadable: back off
    return max(0.0, when - time.time())


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
        except httpx.TransportError as err:
            what = (f"timed out after {min(per_try, left):.1f} s"
                    if isinstance(err, httpx.TimeoutException)
                    else type(err).__name__)
        pause = wait_for(resp, attempt)
        left = deadline - (time.monotonic() - start)
        if attempt == tries or pause >= left:
            raise GaveUp(f"attempt {attempt}: {what}")
        log(f"attempt {attempt}: {what}, retry in {pause:.1f} s")
        await asyncio.sleep(pause)


class Open(Exception):
    pass


async def guarded(breaker, send, **kwargs):
    """call() behind a breaker that always hears how the call ended."""
    if not breaker.allow():
        raise Open("upstream failing; waiting out the cool-off")
    ok = False
    try:
        resp = await call(send, **kwargs)
        ok = resp.status_code < 500
        return resp
    finally:
        breaker.record(ok)


class Breaker:
    """Stop calling an upstream that keeps failing, then try one call.

    Closed: every call goes. After `failures` failures in a row it opens
    and refuses calls; after `cool_off` seconds it lets exactly one trial
    through (half-open), and that trial's result closes or reopens it.
    """

    def __init__(self, failures=5, cool_off=30.0):
        self.failures, self.cool_off = failures, cool_off
        self.count, self.opened, self.trial = 0, None, False

    def allow(self):
        if self.opened is None:
            return True
        if self.trial or time.monotonic() - self.opened < self.cool_off:
            return False
        self.trial = True
        return True

    def record(self, ok):
        self.trial = False
        if ok:
            self.count, self.opened = 0, None
            return
        self.count += 1
        if self.count >= self.failures:
            self.opened = time.monotonic()
