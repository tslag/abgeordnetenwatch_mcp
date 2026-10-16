"""AWClient / AWSession: rate limiting, caching, coalescing, retries, error mapping, paging."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from aw.client import AWClient, RateLimiter, normalize_params
from aw.errors import AWApiError, AWBudgetExceeded, AWNotFound, AWRateLimited
from aw.ratelimit import BACKOFF_SCHEDULE


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

    async def test_429_then_success_honours_retry_after_and_slows_down(self, api, client, clock):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            return httpx.Response(200, json={"meta": {"status": "ok", "result": {"total": 1}}, "data": [{"id": 1}]})

        api.add_response("polls", handler)
        s = client.session()
        res = await s.list("poll", {})
        assert res.items == [{"id": 1}] and calls["n"] == 2
        assert clock.now >= 7  # paused for the Retry-After the API sent
        assert client.limiter.slowdown == 2.0  # and halved its pace
        assert s.usage()["throttled_s"] >= 7

    async def test_429_without_retry_after_uses_the_backoff_schedule(self, api, client, clock):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] <= 2:
                return httpx.Response(429)
            return httpx.Response(200, json={"meta": {"status": "ok", "result": {"total": 0}}, "data": []})

        api.add_response("polls", handler)
        await client.session().list("poll", {})
        assert calls["n"] == 3 and clock.now >= BACKOFF_SCHEDULE[0] + BACKOFF_SCHEDULE[1]  # waits grow with each 429

    async def test_repeated_429_gives_up_after_three_answers(self, api, client):
        api.add_response("polls", lambda r: httpx.Response(429))
        with pytest.raises(AWRateLimited, match="even after backing off"):
            await client.session().list("poll", {})
        assert len(api.requests) == 3

    async def test_a_429_pauses_later_requests_of_other_calls_too(self, api, client, clock):
        state = {"n": 0}

        def flaky(request):
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "12"})
            return httpx.Response(200, json={"meta": {"status": "ok", "result": {"total": 0}}, "data": []})

        api.add_response("polls", flaky)
        api.add("topics", [])
        await client.session().list("poll", {})
        assert clock.now >= 12
        await client.session().list("topic", {})  # an unrelated call afterwards is not pushed past the pause again
        assert len(api.requests) == 3 and clock.now < 30

    async def test_throttle_wait_is_reported_in_usage(self, api, make_client, clock):
        c = make_client(limiter=RateLimiter(rate_per_minute=24, burst=1, min_gap=0, clock=clock.monotonic, sleep=clock.sleep))
        for path in ("polls", "topics", "parliaments"):
            api.add(path, [])
        s = c.session()
        await s.list("poll", {})
        await s.list("topic", {})
        await s.list("parliament", {})
        assert s.requests == 3 and s.usage()["throttled_s"] == pytest.approx(5.0)  # two waits of 2.5 s (24/min)

    async def test_no_throttle_key_when_nothing_waited(self, api, client):
        api.add("polls", [])
        s = client.session()
        await s.list("poll", {})
        assert "throttled_s" not in s.usage()

    async def test_envelope_tells_the_model_when_a_call_was_slowed_down(self, client):
        from aw.envelope import envelope

        s = client.session()
        s.throttled = 7.4
        out = envelope("x", {}, session=s)
        assert out["meta"]["throttled_s"] == 7.4
        assert any("Waited 7s" in n for n in out["notes"])
        s.throttled = 0.4
        assert not any("Waited" in n for n in envelope("x", {}, session=s)["notes"])

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
