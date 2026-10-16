"""RateLimiter: token bucket + minimum gap + adaptive slowdown (see aw/ratelimit.py)."""
from __future__ import annotations

import pytest

from aw.ratelimit import BACKOFF_SCHEDULE, MAX_SLOWDOWN, RECOVERY_STREAK, RateLimiter


def make(**kwargs) -> RateLimiter:
    """A limiter on a frozen clock at t=0, so reservations can be checked against exact times."""
    kwargs.setdefault("clock", lambda: 0.0)
    return RateLimiter(**kwargs)


def reserve_many(limiter: RateLimiter, n: int, now: float = 0.0) -> list[float]:
    return [limiter._reserve(now) for _ in range(n)]


def max_in_window(starts: list[float], seconds: float) -> int:
    return max(sum(1 for t in starts if s <= t < s + seconds) for s in starts)


class TestPacing:
    def test_defaults_allow_a_short_spaced_burst_then_one_request_every_5_seconds(self):
        starts = reserve_many(make(), 8)
        assert starts[:5] == pytest.approx([0.0, 1.5, 3.0, 4.5, 6.0])  # burst of 4, spaced by the minimum gap
        assert starts[5:] == pytest.approx([10.0, 15.0, 20.0])  # then 12/min

    def test_defaults_stay_below_what_the_api_rejected(self):
        """Live findings: a gap of 0.9 s was rejected (>= 1.2 s accepted); a constant 24/min ran into 429
        after ~30 requests, the steady state the API allowed was ~15/min with a burst of ~10."""
        starts = reserve_many(make(), 120)
        assert min(b - a for a, b in zip(starts, starts[1:], strict=False)) >= 1.5 - 1e-9
        assert max_in_window(starts, 10) <= 6
        assert max_in_window(starts, 60) <= 17  # burst 4 + 12/min
        steady = starts[10:]  # after the burst
        assert (len(steady) - 1) / (steady[-1] - steady[0]) * 60 == pytest.approx(12.0, abs=0.01)  # well under ~15/min

    def test_a_configured_burst_is_still_spread_by_the_minimum_gap(self):
        starts = reserve_many(make(burst=4, min_gap=0.25), 8)
        assert starts[:4] == pytest.approx([0.0, 0.25, 0.5, 0.75])
        assert starts[4:] == pytest.approx([5.0, 10.0, 15.0, 20.0])
        assert max_in_window(reserve_many(make(burst=4, min_gap=0.25), 120), 10) <= 6

    def test_concurrent_callers_get_distinct_increasing_slots(self):
        starts = reserve_many(make(burst=10), 10)  # even with a big bucket, starts are spread out
        assert starts == sorted(set(starts)) and starts[-1] - starts[0] >= 9 * 1.5 - 1e-9

    def test_idle_time_refills_the_burst_but_not_beyond(self):
        limiter = make(burst=4, min_gap=0.25)
        reserve_many(limiter, 4)
        later = reserve_many(limiter, 6, now=1000.0)
        assert later[:4] == pytest.approx([1000.0, 1000.25, 1000.5, 1000.75])  # burst of 4 again, not 6
        assert later[4] > 1000.75 + 1.5  # the 5th has to wait for a token

    def test_requests_never_start_in_the_past(self):
        limiter = make()
        assert all(t >= 50.0 for t in reserve_many(limiter, 6, now=50.0))

    @pytest.mark.parametrize("kwargs", [{"rate_per_minute": 0}, {"burst": 0}, {"min_gap": -1}])
    def test_invalid_configuration(self, kwargs):
        with pytest.raises(ValueError):
            make(**kwargs)

    def test_rate_and_burst_are_configurable(self):
        starts = reserve_many(make(rate_per_minute=12, burst=1, min_gap=0), 4)
        assert starts == pytest.approx([0.0, 5.0, 10.0, 15.0])  # 12/min = one every 5 s, no burst


class TestBackoff:
    def test_block_pauses_then_resumes_without_a_burst(self):
        limiter = make(burst=4, min_gap=0.25)  # even with a burst configured, none follows a block
        limiter.block_for(30)
        starts = reserve_many(limiter, 3)
        assert starts[0] == pytest.approx(30.0)
        assert starts[1] - starts[0] >= 2.4

    def test_schedule_without_retry_after(self):
        limiter = make()
        waits = [limiter.on_rate_limited() for _ in range(5)]
        assert waits[:3] == list(BACKOFF_SCHEDULE) and waits[3:] == [BACKOFF_SCHEDULE[-1]] * 2

    @pytest.mark.parametrize(("retry_after", "expected"), [(7.0, 7.0), (0.0, 1.0), (500.0, 60.0)])
    def test_retry_after_is_used_but_clamped(self, retry_after, expected):
        assert make().on_rate_limited(retry_after) == expected

    def test_pace_halves_after_each_429_up_to_a_limit(self):
        limiter = make()
        speeds = []
        for _ in range(4):
            limiter.on_rate_limited(1)
            speeds.append(limiter.slowdown)
        assert speeds == [2.0, 4.0, MAX_SLOWDOWN, MAX_SLOWDOWN]

    def test_slowed_pace_is_visible_in_the_schedule(self):
        limiter = make(rate_per_minute=24, burst=1, min_gap=0)
        limiter.on_rate_limited(1)  # slowdown 2 -> 12 requests/min (one every 5 s)
        starts = reserve_many(limiter, 3)
        assert starts[2] - starts[1] == pytest.approx(5.0)

    def test_recovery_after_a_streak_of_successes(self):
        limiter = make()
        limiter.on_rate_limited(1)
        limiter.on_rate_limited(1)  # slowdown 4
        for _ in range(RECOVERY_STREAK - 1):
            limiter.on_success()
        assert limiter.slowdown == 4.0
        limiter.on_success()
        assert limiter.slowdown == 2.0
        for _ in range(RECOVERY_STREAK):
            limiter.on_success()
        assert limiter.slowdown == 1.0

    def test_a_new_429_resets_the_recovery_streak(self):
        limiter = make()
        limiter.on_rate_limited(1)
        for _ in range(RECOVERY_STREAK - 1):
            limiter.on_success()
        limiter.on_rate_limited(1)
        limiter.on_success()
        assert limiter.slowdown == 4.0

    def test_success_resets_the_consecutive_429_count(self):
        limiter = make()
        limiter.on_rate_limited()
        limiter.on_rate_limited()
        limiter.on_success()
        assert limiter.on_rate_limited() == BACKOFF_SCHEDULE[0]


class TestAcquire:
    async def test_sequential_acquires_follow_the_schedule(self, clock):
        limiter = make(clock=clock.monotonic, sleep=clock.sleep)
        waits = [await limiter.acquire() for _ in range(6)]
        assert clock.now == pytest.approx(10.0)  # starts at 0, 1.5, 3, 4.5, 6 and 10
        assert sum(waits) == pytest.approx(10.0)

    async def test_a_429_arriving_while_waiting_pushes_the_caller_back(self, clock):
        limiter = make(rate_per_minute=24, burst=1, min_gap=0, clock=clock.monotonic)
        injected = {"done": False}

        async def sleep_then_get_blocked(seconds: float) -> None:
            await clock.sleep(seconds)
            if not injected["done"]:
                injected["done"] = True
                limiter.block_for(20)  # another request got a 429 meanwhile

        limiter._sleep = sleep_then_get_blocked
        await limiter.acquire()  # immediate (full bucket)
        before = clock.now
        await limiter.acquire()  # first slot at +2.5 s, then the block moves it past +20 s
        assert clock.now - before >= 20
