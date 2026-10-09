"""Rate-limited, cached, request-budgeted client for the Abgeordnetenwatch API v2.

* Sliding-window limiter (default 24/min) keeps well below the API's 30 req/min per IP.
* Responses are cached (memory, optionally sqlite); identical in-flight requests are coalesced.
* Every tool call works through an :class:`AWSession` that enforces a per-call request budget and
  reports how many requests were really sent vs. served from cache.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from .cache import MISS, DiskCache, LayeredCache, TTLCache
from .endpoints import BASE_URL, ENDPOINTS, TTL_ENTITY
from .errors import AWApiError, AWBudgetExceeded, AWNotFound, AWRateLimited

USER_AGENT = "abgeordnetenwatch-mcp/1.0 (read-only MCP server; CC0 data)"
MAX_PAGE = 1000  # API maximum for range_end / pager_limit


class RateLimiter:
    """Allow at most ``max_calls`` requests in any ``period``-second window."""

    def __init__(
        self,
        max_calls: int = 24,
        period: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.max_calls = max_calls
        self.period = period
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    def block_for(self, seconds: float) -> None:
        """Stop all requests for a while (after an HTTP 429)."""
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    await self._sleep(self._blocked_until - now)
                    continue
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                await self._sleep(self.period - (now - self._calls[0]) + 0.01)


@dataclass
class ListResult:
    items: list[dict]
    total: int
    cached: bool = False
    truncated: bool = False  # True when total > len(items) because of max_items


def normalize_params(params: Mapping[str, Any] | None) -> dict[str, str]:
    """Flatten tool-level filter values into query-string values."""
    out: dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "1" if value else "0"
        elif isinstance(value, (list, tuple, set, frozenset)):
            out[key] = "[" + ",".join(str(v) for v in value) + "]"
        else:
            out[key] = str(value)
    return out


class AWClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: LayeredCache | None = None,
        limiter: RateLimiter | None = None,
        base_url: str = BASE_URL,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._base_url = base_url
        self._http: httpx.AsyncClient | None = None
        self.cache = cache or LayeredCache(TTLCache())
        self.limiter = limiter or RateLimiter()
        self._sleep = sleep
        self._inflight: dict[str, asyncio.Task] = {}

    # -- plumbing -------------------------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                transport=self._transport,
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                follow_redirects=True,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def session(self, budget: int = 8) -> AWSession:
        return AWSession(self, budget)

    async def _fetch(self, path: str, params: dict[str, str]) -> dict:
        """One logical request: limiter, network retries, 429 handling, error mapping."""
        last_exc: Exception | None = None
        rate_limited_once = False
        for attempt in range(3):
            await self.limiter.acquire()
            try:
                response = await self._client().get(path, params=params)
            except httpx.TransportError as exc:  # timeouts, connection resets, ...
                last_exc = exc
                await self._sleep(0.5 * (attempt + 1))
                continue

            if response.status_code == 429:
                wait = min(_retry_after(response) or 20.0, 20.0)
                self.limiter.block_for(wait)
                if rate_limited_once:
                    raise AWRateLimited("Abgeordnetenwatch API is rate limiting (HTTP 429); retry in about a minute.")
                rate_limited_once = True
                continue
            if response.status_code in (502, 503, 504):
                last_exc = AWApiError(f"HTTP {response.status_code} from the API", response.status_code)
                await self._sleep(0.5 * (attempt + 1))
                continue
            return _parse(response, path)
        if rate_limited_once and last_exc is None:
            raise AWRateLimited("Abgeordnetenwatch API is rate limiting (HTTP 429); retry in about a minute.")
        raise AWApiError(f"API unreachable: {last_exc}") from last_exc

    async def request(self, path: str, params: dict[str, str], ttl: float, session: AWSession | None = None) -> tuple[dict, bool]:
        """Return (json, was_cached). Spends session budget only for real network requests."""
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hit = self.cache.get(key)
        if hit is not MISS:
            return hit, True
        task = self._inflight.get(key)
        if task is not None:
            return await asyncio.shield(task), True
        if session is not None:
            session._spend()
        task = asyncio.ensure_future(self._fetch(path, params))
        self._inflight[key] = task
        try:
            payload = await asyncio.shield(task)
        finally:
            self._inflight.pop(key, None)
        self.cache.set(key, payload, ttl)
        return payload, False


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


def _parse(response: httpx.Response, path: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        raise AWApiError(f"Non-JSON answer from the API (HTTP {response.status_code})", response.status_code) from None
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    if response.status_code == 404:
        raise AWNotFound(path)
    if response.status_code >= 400 or meta.get("status") == "error":
        message = meta.get("status_message") or f"HTTP {response.status_code}"
        if message.startswith("There is no"):  # e.g. "There is no politician entity with id 99999999"
            raise AWNotFound(message)
        raise AWApiError(message, response.status_code)
    return payload


@dataclass
class AWSession:
    """Per-tool-call view of the client: budget enforcement and usage accounting."""

    client: AWClient
    budget: int = 8
    requests: int = 0
    cached: int = 0
    _notes: list[str] = field(default_factory=list)

    def _spend(self) -> None:
        if self.requests >= self.budget:
            raise AWBudgetExceeded(
                f"This call would need more than {self.budget} API requests (API limit: 30 per minute). "
                "Narrow the query (period, topic, dates)."
            )
        self.requests += 1

    def usage(self) -> dict:
        return {"requests": self.requests, "cached": self.cached}

    async def list(self, entity: str, params: Mapping[str, Any] | None = None, ttl: float = TTL_ENTITY) -> ListResult:
        payload, was_cached = await self.client.request(ENDPOINTS[entity], normalize_params(params), ttl, self)
        if was_cached:
            self.cached += 1
        data = payload.get("data") or []
        total = (payload.get("meta", {}).get("result", {}) or {}).get("total", len(data))
        return ListResult(items=data, total=total, cached=was_cached)

    async def get_one(self, entity: str, entity_id: int | str, params: Mapping[str, Any] | None = None, ttl: float = TTL_ENTITY) -> dict:
        path = f"{ENDPOINTS[entity]}/{entity_id}"
        payload, was_cached = await self.client.request(path, normalize_params(params), ttl, self)
        if was_cached:
            self.cached += 1
        data = payload.get("data")
        if not isinstance(data, dict):
            raise AWNotFound(path)
        return data

    async def list_all(
        self,
        entity: str,
        params: Mapping[str, Any] | None = None,
        ttl: float = TTL_ENTITY,
        max_items: int = 3000,
    ) -> ListResult:
        """Page through a list (1000 per request) until everything or ``max_items`` is fetched."""
        items: list[dict] = []
        all_cached = True
        total = 0
        base = dict(params or {})
        while True:
            page_size = min(MAX_PAGE, max_items - len(items))
            page = await self.list(entity, {**base, "range_start": len(items), "range_end": page_size}, ttl)
            all_cached = all_cached and page.cached
            total = page.total
            items.extend(page.items)
            if len(items) >= total or len(page.items) < page_size or len(items) >= max_items:
                break
        return ListResult(items=items, total=total, cached=all_cached, truncated=len(items) < total)


# --- process-wide instance ----------------------------------------------------------------------

_client: AWClient | None = None


def get_client() -> AWClient:
    """Lazy singleton configured from the environment (AW_CACHE_PATH, AW_RATE_LIMIT)."""
    global _client
    if _client is None:
        disk_path = os.environ.get("AW_CACHE_PATH")
        rate = int(os.environ.get("AW_RATE_LIMIT", "24"))
        cache = LayeredCache(TTLCache(), DiskCache(disk_path) if disk_path else None)
        _client = AWClient(cache=cache, limiter=RateLimiter(max_calls=rate))
    return _client


def set_client(client: AWClient | None) -> None:
    """Replace the singleton (tests)."""
    global _client
    _client = client
