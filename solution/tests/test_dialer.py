"""The dial protocol and the reconciler, against a scripted fake provider (no network, no waiting)."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from solution.dispatcher.dialer import Dialer
from solution.dispatcher.provider import Provider, classify_create
from solution.dispatcher.reconciler import Reconciler
from solution.dispatcher.rules import COMPLETED, DND_BLOCKED, PENDING, RETRY_WAIT
from solution.dispatcher.simclock import FakeClock

from .conftest import ended, ist, only_leads

T0 = ist("2026-11-12 17:30")
ACCEPTED = (201, {"call_sid": "CA1", "state": "active"})
UNAVAILABLE = (503, {"error": "service_unavailable", "retryable": True})
GATEWAY_TIMEOUT = (504, {"error": "upstream_timeout", "detail": "outcome unknown"})


class FakeProvider:
    """POST /v1/calls answers from a script: (status, body) tuples, exceptions to raise, or
    callables (for side effects such as moving the clock). Lookups answer from dicts."""

    def __init__(self) -> None:
        self.post_responses: list = []
        self.by_request: dict[str, dict] = {}
        self.by_sid: dict[str, dict] = {}
        self.posts: list[dict] = []
        self.lookups: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/calls":
            self.posts.append(json.loads(request.content))
            r = self.post_responses.pop(0)
            if callable(r):
                r = r()
            if isinstance(r, Exception):
                raise r
            return httpx.Response(r[0], json=r[1])
        if path == "/v1/calls":
            rid = request.url.params["request_id"]
            self.lookups.append(rid)
            body = self.by_request.get(rid)
            return httpx.Response(200, json=body) if body else httpx.Response(404, json={"detail": {}})
        if path.startswith("/v1/calls/"):
            sid = path.rsplit("/", 1)[1]
            self.lookups.append(sid)
            body = self.by_sid.get(sid)
            return httpx.Response(200, json=body) if body else httpx.Response(404, json={"detail": {}})
        return httpx.Response(404)


@pytest.fixture
def env(store, cfg, margins):
    api = FakeProvider()
    provider = Provider("http://provider", transport=httpx.MockTransport(api.handler))
    clock = FakeClock(T0)
    dialer = Dialer(store, provider, clock, cfg, margins, "http://127.0.0.1:8200/webhooks/telephony",
                    notify=lambda: None, max_posts=3, retry_pause=0)
    reconciler = Reconciler(store, provider, clock, dialer, notify=lambda: None)
    only_leads(store, ("c_acme_renewals", "ren_0001"))
    return SimpleNamespace(api=api, clock=clock, dialer=dialer, reconciler=reconciler, store=store,
                           margins=margins)


def reserve_one(env, now_real: float | None = None):
    [it] = env.store.reserve_dials(env.clock.now(), time.time() if now_real is None else now_real, env.margins)
    return it


def dial(env, *responses):
    env.api.post_responses = list(responses)
    it = reserve_one(env)
    asyncio.run(env.dialer.place(it))
    return it


def phase(env, it):
    return env.store.call(it.request_id)["phase"]


def lead_state(env):
    return env.store.lead("c_acme_renewals", "ren_0001")["state"]


def attempts(env):
    return env.store.attempts("c_acme_renewals", "ren_0001")


def reconcile(env):
    """One reconciler pass, plus any dial it resumed."""
    async def go():
        await env.reconciler.run_once()
        await env.dialer.wait_idle()
    asyncio.run(go())


def make_settled(env, it):
    """Pretend the settle period has passed (no real waiting in tests)."""
    with env.store.tx():
        env.store.db.execute("UPDATE calls SET settle_after_real = ? WHERE request_id = ?",
                             (time.time() - 1, it.request_id))


# ---------------------------------------------------------------- happy path
def test_201_records_the_call_as_placed(env):
    it = dial(env, ACCEPTED)
    assert phase(env, it) == "placed" and env.store.call(it.request_id)["call_sid"] == "CA1"
    sent = env.api.posts[0]
    assert sent["request_id"] == it.request_id and sent["phone"] == "+916588139986"
    assert sent["callback_url"].endswith("/webhooks/telephony")
    assert it.request_id not in env.dialer.in_flight


def test_200_replay_of_a_finished_call_applies_its_result(env):
    def replay():
        row = env.store.call(env.api.posts[-1]["request_id"])
        result = ended(row, "completed", "not_interested", ended_at=ist("2026-11-12 17:33"), sid="CA2")
        return 200, {"call_sid": "CA2", "state": "ended", "result": result}
    dial(env, replay)
    assert lead_state(env) == COMPLETED


# ---------------------------------------------------------------- transient failures: same key
def test_503_is_resent_with_the_same_request_id(env):
    it = dial(env, UNAVAILABLE, UNAVAILABLE, ACCEPTED)
    assert [p["request_id"] for p in env.api.posts] == [it.request_id] * 3
    assert phase(env, it) == "placed" and attempts(env) == 1


def test_only_503s_means_nothing_was_placed(env):
    it = dial(env, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE)
    assert phase(env, it) == "not_placed"
    assert lead_state(env) == PENDING and attempts(env) == 0  # R4: 503s are not attempts


def test_504_then_replay_counts_one_attempt(env):
    # The 504 hid a call that WAS placed; re-sending the key returns it (200), never a second call.
    it = dial(env, GATEWAY_TIMEOUT, (200, {"call_sid": "CA5", "state": "active"}))
    assert [p["request_id"] for p in env.api.posts] == [it.request_id] * 2
    assert phase(env, it) == "placed" and attempts(env) == 1


def test_client_timeout_is_resent_with_the_same_request_id(env):
    it = dial(env, httpx.ReadTimeout("slow"), ACCEPTED)
    assert phase(env, it) == "placed"
    assert len({p["request_id"] for p in env.api.posts}) == 1


# ---------------------------------------------------------------- unresolved -> lookup
def test_unresolved_504s_are_settled_by_lookup_found(env):
    it = dial(env, GATEWAY_TIMEOUT, GATEWAY_TIMEOUT, GATEWAY_TIMEOUT)
    assert phase(env, it) == "intent"  # still holding its slot and phone
    env.api.by_request[it.request_id] = {"call_sid": "CA5", "request_id": it.request_id, "state": "active"}
    reconcile(env)  # "found" is conclusive at once
    assert phase(env, it) == "placed" and attempts(env) == 1


def test_not_found_while_window_open_is_resent_with_the_same_key(env):
    it = dial(env, httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    env.api.post_responses = [ACCEPTED]
    reconcile(env)  # 404, window open: finish the dial instead of waiting
    assert phase(env, it) == "placed" and attempts(env) == 1
    assert {p["request_id"] for p in env.api.posts} == {it.request_id}


def test_not_found_after_window_closed_is_released_only_once_settled(env):
    it = dial(env, httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    env.clock.set(ist("2026-11-12 19:30"))  # renewals closed at 19:00
    reconcile(env)
    assert phase(env, it) == "intent"  # a 404 right after an unanswered POST proves nothing
    make_settled(env, it)
    reconcile(env)
    assert phase(env, it) == "not_placed" and lead_state(env) == PENDING
    assert len(env.api.posts) == 3  # nothing was sent after the window closed


@pytest.mark.parametrize("found", [True, False])
def test_intent_orphaned_by_a_crash_inside_the_window_completes_promptly(env, found):
    it = reserve_one(env, now_real=time.time() - 60)  # reserved a minute ago, then we died
    if found:
        env.api.by_request[it.request_id] = {"call_sid": "CA7", "request_id": it.request_id, "state": "active"}
    else:
        env.api.post_responses = [ACCEPTED]  # never sent before the crash: send it now, same key
    reconcile(env)
    assert phase(env, it) == "placed" and attempts(env) == 1
    assert all(p["request_id"] == it.request_id for p in env.api.posts)


def test_intent_orphaned_by_a_crash_after_the_window_is_released(env):
    it = reserve_one(env, now_real=time.time() - 60)
    env.clock.set(ist("2026-11-12 19:30"))
    reconcile(env)
    assert phase(env, it) == "not_placed" and env.api.posts == []


def test_reconciler_never_second_guesses_a_post_in_flight(env):
    it = reserve_one(env, now_real=time.time() - 60)
    env.dialer.in_flight.add(it.request_id)
    reconcile(env)
    assert env.api.lookups == [] and phase(env, it) == "intent"


# ---------------------------------------------------------------- windows
def test_window_closing_before_the_first_send_means_nothing_is_sent(env):
    env.clock.set(ist("2026-11-12 18:57:00"))
    it = reserve_one(env)
    env.clock.set(ist("2026-11-12 18:58:10"))  # the event loop stalled; now inside the end margin
    asyncio.run(env.dialer.place(it))
    assert env.api.posts == [] and phase(env, it) == "not_placed"


def test_a_due_callback_is_sent_in_the_last_minute(env):
    with env.store.tx():  # ren_0001 asked to be called back at 18:59; renewals closes at 19:00
        env.store.db.execute("UPDATE leads SET state = 'callback_wait', not_before = ? WHERE lead_id = 'ren_0001'",
                             (ist("2026-11-12 18:59").isoformat(),))
    env.clock.set(ist("2026-11-12 18:59:05"))
    it = dial(env, ACCEPTED)
    assert it.queue == "callback_wait" and phase(env, it) == "placed"


def test_window_closing_after_a_503_stops_resending_and_releases(env):
    env.clock.set(ist("2026-11-12 18:57:00"))

    def unavailable_then_late():
        env.clock.set(ist("2026-11-12 18:58:10"))
        return UNAVAILABLE
    it = dial(env, unavailable_then_late)
    assert len(env.api.posts) == 1 and phase(env, it) == "not_placed"


def test_window_closing_after_a_504_stops_resending_and_looks_it_up(env):
    env.clock.set(ist("2026-11-12 18:57:00"))

    def timeout_then_late():
        env.clock.set(ist("2026-11-12 18:58:10"))
        return GATEWAY_TIMEOUT
    it = dial(env, timeout_then_late)
    assert len(env.api.posts) == 1          # no new call is started after the window closes...
    assert phase(env, it) == "intent"        # ...but the first one may exist: settle by lookup
    assert "window closed" in env.store.call(it.request_id)["error"]  # mark_unknown wrote the reason


# ---------------------------------------------------------------- rejections
def test_rule_rejection_backs_off_instead_of_hammering(env):
    it = dial(env, (429, {"error": "tenant_concurrency_exceeded", "detail": "active=4 max=4"}))
    assert phase(env, it) == "rejected" and len(env.api.posts) == 1
    lead = env.store.lead("c_acme_renewals", "ren_0001")
    assert lead["state"] == PENDING and lead["not_before"] == (T0 + timedelta(minutes=10)).isoformat()
    assert env.store.reserve_dials(T0 + timedelta(minutes=5), time.time(), env.margins) == []


def test_rejection_after_an_ambiguous_post_is_never_resent(env):
    # A rejection ends the intent at once, so nothing is left for the reconciler to re-send.
    # (The provider answers 200 for a key that already has a call, so a 4xx means none exists.)
    it = dial(env, GATEWAY_TIMEOUT, (409, {"error": "concurrent_dial_same_phone"}))
    assert phase(env, it) == "rejected" and len(env.api.posts) == 2
    reconcile(env)
    reconcile(env)
    assert len(env.api.posts) == 2 and env.api.lookups == []
    lead = env.store.lead("c_acme_renewals", "ren_0001")
    assert lead["state"] == PENDING and lead["not_before"] == (T0 + timedelta(minutes=10)).isoformat()


def test_dnd_violation_blocks_the_phone(env):
    dial(env, (422, {"error": "dnd_violation", "detail": "phone opted out"}))
    assert lead_state(env) == DND_BLOCKED


def test_provider_saying_the_lead_is_finished_stops_us_calling(env):
    dial(env, (422, {"error": "dial_after_terminal", "detail": "lead already interested"}))
    assert lead_state(env) == COMPLETED


# ---------------------------------------------------------------- lost webhooks
def test_lost_webhook_is_recovered_by_polling(env):
    it = dial(env, ACCEPTED)
    result = ended(it, "busy", None, ended_at=ist("2026-11-12 17:30:10"), sid="CA1")
    env.api.by_sid["CA1"] = {"call_sid": "CA1", "state": "ended", "result": result}

    env.clock.advance(minutes=2)  # a webhook may still be on its way: don't poll yet
    asyncio.run(env.reconciler.run_once())
    assert env.api.lookups == []

    env.clock.advance(minutes=7)  # well past the longest possible call
    asyncio.run(env.reconciler.run_once())
    assert env.api.lookups == ["CA1"] and lead_state(env) == RETRY_WAIT


@pytest.mark.parametrize("code, body, kind", [
    (201, {"call_sid": "CA1"}, "accepted"),
    (200, {"call_sid": "CA1", "result": {"x": 1}}, "accepted"),
    (201, {}, "unknown"),
    (503, {"error": "service_unavailable"}, "not_placed"),
    (504, {"error": "upstream_timeout"}, "unknown"),
    (500, {}, "unknown"),
    (409, {"error": "concurrent_dial_same_phone"}, "rejected"),
    (422, {"detail": {"error": "unknown_lead"}}, "rejected"),
])
def test_classify_create(code, body, kind):
    assert classify_create(code, body).kind == kind
