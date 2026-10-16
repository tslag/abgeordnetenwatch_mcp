"""Request pacing for the Abgeordnetenwatch API.

The API documents 30 requests/minute per IP. Measured on 2026-10-16 it is considerably stricter:

* **Spacing:** after an idle minute, requests >= 1.2 s apart were accepted (13 of 13) while a second
  request 0.9 s after the first got HTTP 429 every time (3 of 3, answered within ~20 ms).
* **Sustained rate:** at a perfectly even 24 requests/minute the first 429 came at request #31 (after 75 s);
  after each short pause only 10-12 further requests were accepted before the next 429 - a steady state of
  about 15 requests/minute with a burst allowance of roughly 10 (it behaves like a leaky bucket).
* A 429 is not followed by a long ban: waiting a few seconds is enough for one request to be accepted again.

A plain "N per minute" window is therefore not enough. This limiter paces requests instead, with margin
below the measured values:

* **Token bucket** - ``rate_per_minute`` tokens refill steadily, at most ``burst`` can be spent back to
  back. In any window of T seconds at most ``burst + rate * T`` requests start (defaults: 4 + 0.2/s, i.e.
  one request every 5 s once the burst is used; <= 16 in 60 s against ~25 the API tolerates).
* **Minimum gap** - request starts are at least ``min_gap`` seconds apart (default 1.5 s), so a burst and
  concurrent fetches (``asyncio.gather``) are spread out instead of hitting the API at once.
* **Adaptive slowdown** - the limit is per IP and this process cannot see other programs on the same IP.
  After a 429 the pace is halved (up to 4x slower) and everything pauses; after a streak of successful
  requests the pace recovers.

Reservations are made synchronously (no ``await`` inside ``_reserve``), so they are atomic in asyncio and
queued callers are served in order, each with its own future start time.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from typing import Any

# waits (seconds) after the 1st, 2nd, ... consecutive 429 when the API sends no Retry-After header
# the first wait covers one token refill at the API's measured ~0.25 requests/s (4 s) with a margin; the later
# steps cover other programs sharing the IP
BACKOFF_SCHEDULE = (5.0, 12.0, 25.0)
MAX_SLOWDOWN = 4.0
RECOVERY_STREAK = 15  # successful requests before the pace speeds up again
MAX_BLOCK_SECONDS = 60.0


class RateLimiter:
    def __init__(
        self,
        rate_per_minute: float = 12.0,
        burst: int = 4,
        min_gap: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if rate_per_minute <= 0 or burst < 1 or min_gap < 0:
            raise ValueError("rate_per_minute must be > 0, burst >= 1 and min_gap >= 0")
        self.rate_per_minute = rate_per_minute
        self.capacity = float(burst)
        self.min_gap = min_gap
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)  # token count as of `_updated` (which may lie in the future)
        self._updated = clock()
        self._last_start = -math.inf
        self.blocked_until = 0.0
        self.slowdown = 1.0
        self._good_streak = 0
        self._consecutive_429 = 0

    # -- pacing ---------------------------------------------------------------------------------
    @property
    def rate(self) -> float:
        """Current refill rate in tokens per second (reduced while slowed down)."""
        return self.rate_per_minute / 60.0 / self.slowdown

    def _reserve(self, now: float) -> float:
        """Reserve the next request slot and return its start time (>= now)."""
        rate = self.rate
        ref = max(now, self._updated)
        tokens = min(self.capacity, self._tokens + (ref - self._updated) * rate)
        token_time = ref if tokens >= 1.0 else ref + (1.0 - tokens) / rate
        start = max(token_time, self.blocked_until, self._last_start + self.min_gap, now)
        tokens = min(self.capacity, tokens + (start - ref) * rate) - 1.0
        self._tokens = max(0.0, tokens)
        self._updated = start
        self._last_start = start
        return start

    async def acquire(self) -> float:
        """Wait for a slot. Returns the total seconds spent waiting."""
        waited = 0.0
        while True:
            now = self._clock()
            delay = self._reserve(now) - now
            if delay > 0:
                await self._sleep(delay)
                waited += delay
            if self._clock() >= self.blocked_until:  # a 429 may have arrived while this caller slept
                return waited

    # -- feedback from the HTTP layer -------------------------------------------------------------
    def block_for(self, seconds: float) -> None:
        """Pause all requests for ``seconds``; afterwards one request may go out, then normal pacing resumes."""
        self.blocked_until = max(self.blocked_until, self._clock() + seconds)
        # no refill while paused and no burst afterwards: a single token is available when the pause ends
        self._tokens = 1.0
        self._updated = max(self._updated, self.blocked_until)

    def on_rate_limited(self, retry_after: float | None = None) -> float:
        """Register an HTTP 429: block, slow down, and return how long requests are paused."""
        index = min(self._consecutive_429, len(BACKOFF_SCHEDULE) - 1)
        wait = retry_after if retry_after is not None else BACKOFF_SCHEDULE[index]
        wait = min(max(wait, 1.0), MAX_BLOCK_SECONDS)
        self._consecutive_429 += 1
        self._good_streak = 0
        self.slowdown = min(MAX_SLOWDOWN, self.slowdown * 2.0)
        self.block_for(wait)
        return wait

    def on_success(self) -> None:
        """Register a request the API accepted (any non-429 answer)."""
        self._consecutive_429 = 0
        if self.slowdown > 1.0:
            self._good_streak += 1
            if self._good_streak >= RECOVERY_STREAK:
                self.slowdown = max(1.0, self.slowdown / 2.0)
                self._good_streak = 0
