"""AWClient / AWSession: rate limiting, caching, coalescing, retries, error mapping, paging."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from aw.client import AWClient, RateLimiter, normalize_params
from aw.errors import AWApiError, AWBudgetExceeded, AWNotFound, AWRateLimited


class TestRateLimiter:
    async def test_blocks_after_max_calls_until_window_passes(self):
        now = [0.0]
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        limiter = RateLimiter(max_calls=3, period=60.0, clock=lambda: now[0], sleep=fake_sleep)
        for _ in range(3):
            await limiter.acquire()
        assert slept == []
        await limiter.acquire()  # 4th call inside the window must wait for the first to expire
        assert len(slept) == 1 and 59.9 < slept[0] < 60.1

    async def test_block_for_delays_everything(self):
        now = [0.0]

        async def fake_sleep(seconds: float) -> None:
            now[0] += seconds

        limiter = RateLimiter(max_calls=10, clock=lambda: now[0], sleep=fake_sleep)
        limiter.block_for(20)
        await limiter.acquire()
        assert now[0] >= 20


class TestNormalizeParams:
    def test_values(self):
        assert normalize_params({"a": True, "b": False, "c": [1, 2, 3], "d": None, "e": "x", "f": 5}) == {"a": "1", "b": "0", "c": "[1,2,3]", "e": "x", "f": "5"}

    def test_none_input(self):
        assert normalize_params(None) == {}


class TestCachingAndCoalescing:
    async def test_second_identical_request_is_served_from_cache(self, api, client):
        api.add("parliaments", [{"id": 1}])
        s = client.session()
        await s.list("parliament", {"range_end": 5})
        await s.list("parliament", {"range_end": 5})
        assert len(api.requests) == 1
        assert s.usage() == {"requests": 1, "cached": 1}

    async def test_param_order_does_not_matter_for_the_cache(self, api, client):
        api.add("polls", [])
        s = client.session()
        await s.list("poll", {"a": 1, "b": 2})
        await s.list("poll", {"b": 2, "a": 1})
        assert len(api.requests) == 1

    async def test_concurrent_identical_requests_share_one_http_call(self, api, client):
        api.add("topics", [{"id": 1}])
        s1, s2 = client.session(), client.session()
        await asyncio.gather(s1.list("topic", {}), s2.list("topic", {}))
        assert len(api.requests) == 1
        assert s1.requests + s2.requests == 1 and s1.cached + s2.cached == 1


class TestErrors:
    async def test_error_status_becomes_api_error_with_message(self, api, client):
        api.add_error("votes", "Array is not a valid value for poll.", status=500)
        with pytest.raises(AWApiError, match="not a valid value"):
            await client.session().list("vote", {"poll[in]": [1, 2]})

    async def test_there_is_no_entity_maps_to_not_found(self, api, client):
        api.add_error("politicians/9", "There is no politician entity with id 9", status=500)
        with pytest.raises(AWNotFound):
            await client.session().get_one("politician", 9)

    async def test_http_404_is_not_found(self, api, client):
        with pytest.raises(AWNotFound):
            await client.session().list("poll", {})  # no route -> 404

    async def test_non_json_body(self, api, client):
        api.add_response("polls", lambda r: httpx.Response(200, text="<html>nope</html>"))
        with pytest.raises(AWApiError, match="Non-JSON"):
            await client.session().list("poll", {})

    async def test_429_then_success(self, api, client):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"meta": {"status": "ok", "result": {"total": 1}}, "data": [{"id": 1}]})

        api.add_response("polls", handler)
        res = await client.session().list("poll", {})
        assert res.items == [{"id": 1}] and calls["n"] == 2

    async def test_repeated_429_raises_rate_limited(self, api, client):
        api.add_response("polls", lambda r: httpx.Response(429))
        with pytest.raises(AWRateLimited):
            await client.session().list("poll", {})

    async def test_gateway_error_is_retried(self, api, client):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, json={"meta": {"status": "ok", "result": {"total": 0}}, "data": []})

        api.add_response("polls", handler)
        await client.session().list("poll", {})
        assert calls["n"] == 3

    async def test_persistent_gateway_error_gives_api_error(self, api, client):
        api.add_response("polls", lambda r: httpx.Response(503, text="down"))
        with pytest.raises(AWApiError, match="unreachable"):
            await client.session().list("poll", {})

    async def test_transport_error_is_retried_then_reported(self, make_client):
        def boom(request):
            raise httpx.ConnectError("no route")

        c = make_client(transport=httpx.MockTransport(boom))
        with pytest.raises(AWApiError, match="unreachable"):
            await c.session().list("poll", {})


class TestBudget:
    async def test_budget_counts_network_requests_only(self, api, client):
        api.add("parliaments", [])
        api.add("topics", [])
        api.add("polls", [])
        s = client.session(budget=2)
        await s.list("parliament", {})
        await s.list("parliament", {})  # cached: free
        await s.list("topic", {})
        with pytest.raises(AWBudgetExceeded):
            await s.list("poll", {})
        assert s.requests == 2


class TestListAll:
    async def test_pages_through_all_results(self, api, client):
        items = [{"id": i} for i in range(2300)]

        def handler(request):
            start = int(request.url.params["range_start"])
            size = int(request.url.params["range_end"])
            chunk = items[start:start + size]
            return httpx.Response(200, json={"meta": {"status": "ok", "result": {"count": len(chunk), "total": len(items)}}, "data": chunk})

        api.add_response("votes", handler)
        res = await client.session(budget=10).list_all("vote", {"mandate": 1})
        assert len(res.items) == 2300 and res.total == 2300 and not res.truncated
        assert [c["range_start"] for c in api.calls("votes")] == ["0", "1000", "2000"]

    async def test_max_items_marks_truncation(self, api, client):
        items = [{"id": i} for i in range(50)]

        def handler(request):
            start, size = int(request.url.params["range_start"]), int(request.url.params["range_end"])
            chunk = items[start:start + size]
            return httpx.Response(200, json={"meta": {"status": "ok", "result": {"count": len(chunk), "total": 50}}, "data": chunk})

        api.add_response("votes", handler)
        res = await client.session().list_all("vote", {}, max_items=20)
        assert len(res.items) == 20 and res.truncated


class TestGetOne:
    async def test_returns_entity_dict(self, api, client):
        api.add("politicians/5", {"id": 5, "label": "X"})
        assert (await client.session().get_one("politician", 5))["label"] == "X"

    async def test_list_payload_is_not_an_entity(self, api, client):
        api.add("politicians/5", [])
        with pytest.raises(AWNotFound):
            await client.session().get_one("politician", 5)


def test_client_is_constructible_without_arguments():
    assert isinstance(AWClient(), AWClient)
