"""Rate-limited, cached, request-budgeted client for the Abgeordnetenwatch API v2.

* A pacing limiter (token bucket + minimum gap + adaptive slowdown, see ``ratelimit``) keeps requests
  well below the API's limits - the API reacts to bursts, not only to the per-minute total.
* Responses are cached (memory, optionally sqlite); identical in-flight requests are coalesced.
* Every tool call works through an :class:`AWSession` that enforces a per-call request budget and
  reports how many requests were really sent vs. served from cache.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from .cache import MISS, DiskCache, LayeredCache, TTLCache
from .endpoints import BASE_URL, ENDPOINTS, TTL_ENTITY
from .errors import AWApiError, AWBudgetExceeded, AWNotFound, AWRateLimited
from .ratelimit import RateLimiter

__all__ = ["AWClient", "AWSession", "ListResult", "RateLimiter", "get_client", "set_client", "normalize_params"]

USER_AGENT = "abgeordnetenwatch-mcp/1.0 (read-only MCP server; CC0 data)"
MAX_PAGE = 1000  # API maximum for range_end / pager_limit
MAX_ATTEMPTS = 5  # per logical request, counting network errors, gateway errors and 429s
MAX_429_RETRIES = 2  # after the 3rd HTTP 429 in a row the call gives up
RATE_LIMITED_MESSAGE = (
    "Abgeordnetenwatch API is rate limiting (HTTP 429) even after backing off. The limit is per IP address "
    "and may be shared with other programs; retry in about a minute."
)


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

    async def _fetch(self, path: str, params: dict[str, str]) -> tuple[dict, float]:
        """One logical request: pacing, network retries, 429 backoff, error mapping.

        Returns ``(payload, seconds_spent_waiting_for_the_rate_limit)``.
        """
        last_exc: Exception | None = None
        rate_limited = 0
        waited = 0.0
        for attempt in range(MAX_ATTEMPTS):
            waited += await self.limiter.acquire()
            try:
                response = await self._client().get(path, params=params)
            except httpx.TransportError as exc:  # timeouts, connection resets, ...
                last_exc = exc
                await self._sleep(0.5 * (attempt + 1))
                continue

            if response.status_code == 429:
                self.limiter.on_rate_limited(_retry_after(response))  # pauses everything, halves the pace
                rate_limited += 1
                if rate_limited > MAX_429_RETRIES:
                    raise AWRateLimited(RATE_LIMITED_MESSAGE)
                continue
            if response.status_code in (502, 503, 504):
                last_exc = AWApiError(f"HTTP {response.status_code} from the API", response.status_code)
                await self._sleep(0.5 * (attempt + 1))
                continue
            self.limiter.on_success()
            return _parse(response, path), waited
        if rate_limited and last_exc is None:
            raise AWRateLimited(RATE_LIMITED_MESSAGE)
        raise AWApiError(f"API unreachable: {last_exc}") from last_exc

    async def request(self, path: str, params: dict[str, str], ttl: float, session: AWSession | None = None) -> tuple[dict, bool]:
        """Return (json, was_cached). Spends session budget only for real network requests."""
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hit = self.cache.get(key)
        if hit is not MISS:
            return hit, True
        task = self._inflight.get(key)
        if task is not None:
            payload, _ = await asyncio.shield(task)
            return payload, True
        if session is not None:
            session._spend()
        task = asyncio.ensure_future(self._fetch(path, params))
        self._inflight[key] = task
        try:
            payload, waited = await asyncio.shield(task)
        finally:
            self._inflight.pop(key, None)
        if session is not None:
            session.throttled += waited
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
    throttled: float = 0.0  # seconds this call spent waiting for the rate limiter
    _notes: list[str] = field(default_factory=list)

    def _spend(self) -> None:
        if self.requests >= self.budget:
            raise AWBudgetExceeded(
                f"This call would need more than {self.budget} API requests (the API allows about 30 per minute). "
                "Narrow the query (period, topic, dates)."
            )
        self.requests += 1

    def usage(self) -> dict:
        usage: dict[str, Any] = {"requests": self.requests, "cached": self.cached}
        if self.throttled >= 0.5:
            usage["throttled_s"] = round(self.throttled, 1)
        return usage

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
    """Lazy singleton configured from the environment (AW_CACHE_PATH, AW_RATE_LIMIT, AW_RATE_BURST)."""
    global _client
    if _client is None:
        disk_path = os.environ.get("AW_CACHE_PATH")
        rate = float(os.environ.get("AW_RATE_LIMIT", "12"))
        burst = int(os.environ.get("AW_RATE_BURST", "4"))
        cache = LayeredCache(TTLCache(), DiskCache(disk_path) if disk_path else None)
        _client = AWClient(cache=cache, limiter=RateLimiter(rate_per_minute=rate, burst=burst))
    return _client


def set_client(client: AWClient | None) -> None:
    """Replace the singleton (tests)."""
    global _client
    _client = client
