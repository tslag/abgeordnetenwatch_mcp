"""Shared test fixtures: a fake Abgeordnetenwatch API behind httpx.MockTransport and record builders.

Builders mirror the real response shapes that were captured from the live API (API v2.9.0).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastmcp import Client

from aw.cache import LayeredCache, TTLCache
from aw.client import AWClient, RateLimiter, set_client


class FakeClock:
    """Monotonic clock whose sleep() just advances time, so waits cost nothing in tests."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeAPI:
    """Routes GETs to canned data by path and (subset of) query parameters; logs every request."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, dict[str, str], Callable[[httpx.Request], httpx.Response]]] = []
        self.requests: list[httpx.Request] = []

    # -- registration -----------------------------------------------------------------------
    def add(self, path: str, data: Any, *, match: dict[str, Any] | None = None, total: int | None = None, status: int = 200) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            body = {"meta": {"status": "ok", "status_message": "", "result": {"count": len(data) if isinstance(data, list) else 1, "total": total if total is not None else (len(data) if isinstance(data, list) else 1)}}, "data": data}
            return httpx.Response(status, json=body)

        self.routes.append((path, {k: str(v) for k, v in (match or {}).items()}, respond))

    def add_error(self, path: str, message: str, *, match: dict[str, Any] | None = None, status: int = 500) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"meta": {"status": "error", "status_message": message}, "data": []})

        self.routes.append((path, {k: str(v) for k, v in (match or {}).items()}, respond))

    def add_response(self, path: str, handler: Callable[[httpx.Request], httpx.Response], *, match: dict[str, Any] | None = None) -> None:
        self.routes.append((path, {k: str(v) for k, v in (match or {}).items()}, handler))

    # -- transport --------------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v2/")
        params = dict(request.url.params)
        # most specific route wins (most matched params); on a tie the latest registration, so tests can override
        candidates = [
            (len(match), index, handler)
            for index, (route_path, match, handler) in enumerate(self.routes)
            if route_path == path and all(params.get(k) == v for k, v in match.items())
        ]
        if candidates:
            return max(candidates, key=lambda c: (c[0], c[1]))[2](request)
        return httpx.Response(404, json={"meta": {"status": "error", "status_message": f"no fake route for {path} {params}"}, "data": []})

    # -- inspection -------------------------------------------------------------------------
    def calls(self, path: str) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests if r.url.path.removeprefix("/api/v2/") == path]


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_client(api: FakeAPI, clock: FakeClock):
    """Factory for an AWClient wired to the fake API (fake clock, generous rate limit)."""

    def factory(**overrides: Any) -> AWClient:
        kwargs: dict[str, Any] = {
            "transport": httpx.MockTransport(api.handle),
            "cache": LayeredCache(TTLCache()),
            "limiter": RateLimiter(max_calls=10_000, clock=clock.monotonic, sleep=clock.sleep),
            "sleep": clock.sleep,
        }
        kwargs.update(overrides)
        return AWClient(**kwargs)

    return factory


@pytest.fixture
def client(make_client) -> AWClient:
    c = make_client()
    set_client(c)
    yield c
    set_client(None)


@pytest.fixture
async def mcp_client(client):
    """In-memory MCP client talking to the real server wiring, backed by the fake API."""
    from server import build_server

    async with Client(build_server()) as c:
        yield c
