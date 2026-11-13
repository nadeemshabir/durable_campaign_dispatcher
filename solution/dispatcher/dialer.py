"""The dial protocol: POST an intent that is already committed, and record exactly what
the responses prove.

One intent = one request_id, re-sent until the provider gives a definitive answer:

    201 / 200      -> placed (attempt counts). 200 is the provider replaying a call that an
                      earlier POST with this request_id already created.
    503            -> this POST placed nothing: pause briefly, re-send the same request_id
    504 / timeout  -> this POST may have placed the call: re-send the same request_id, which
                      either places it now (201) or returns the one that exists (200)
    other 4xx      -> rejected (the provider logged a violation): release with backoff

Re-sending can never create a second call, because the provider performs at most one call
per request_id. And while we re-send, the intent keeps holding its tenant slot and phone.

If we run out of tries, or the window closes, and some POST *might* have placed the call,
the intent stays unresolved and the reconciler settles it by looking up the request_id
(and, while the window is still open, hands it back here to be re-sent). That is the
same path a crash mid-POST takes after restart.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Callable

import httpx

from . import rules
from .config import Config
from .provider import Provider, classify_create
from .simclock import Clock
from .store import CallIntent, Store

log = logging.getLogger(__name__)

# If the provider says a lead is already finished, believe it and stop calling: retrying
# would only add violations. Unreachable unless our own records are wrong.
FINAL_ON_REJECT = {"dial_after_terminal": rules.COMPLETED, "max_attempts_exceeded": rules.EXHAUSTED}


class Dialer:
    def __init__(self, store: Store, provider: Provider, clock: Clock, cfg: Config,
                 margins: rules.Margins, callback_url: str, notify: Callable[[], None], *,
                 reject_backoff: timedelta = timedelta(minutes=10), post_timeout: float = 0.5,
                 max_posts: int = 5, retry_pause: float = 0.05) -> None:
        self.store, self.provider, self.clock, self.cfg = store, provider, clock, cfg
        self.margins, self.callback_url, self.notify = margins, callback_url, notify
        self.reject_backoff = reject_backoff
        # A normal POST answers in a few ms. The provider's 504s take 2 s (20 sim min at 600x)
        # to arrive; giving up after post_timeout and re-sending the same request_id is safe
        # and keeps the reserved slot from sitting idle.
        self.post_timeout, self.max_posts, self.retry_pause = post_timeout, max_posts, retry_pause
        self.in_flight: set[str] = set()  # request_ids this process is still POSTing
        self._tasks: set[asyncio.Task] = set()

    def start(self, intent: CallIntent, *, resumed: bool = False) -> None:
        """POST in the background so one slow request never stalls dispatching for other
        leads. The slot is already reserved in the DB. `resumed`: an earlier attempt (maybe
        by a process that was killed) may already have placed this call."""
        self.in_flight.add(intent.request_id)
        task = asyncio.create_task(self.place(intent, resumed=resumed), name=f"dial {intent.request_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Wait for every POST task started so far (used by the tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def place(self, intent: CallIntent, *, resumed: bool = False) -> None:
        rid = intent.request_id
        campaign = self.cfg.campaigns[intent.campaign_id]
        margins = self.margins.for_queue(intent.queue)  # the same window rule it was picked by
        payload = {"request_id": rid, "campaign_id": intent.campaign_id, "lead_id": intent.lead_id,
                   "phone": intent.phone, "callback_url": self.callback_url}
        self.in_flight.add(rid)
        maybe_placed = resumed  # could any POST so far have created the call?
        why = "resumed after an unresolved attempt" if resumed else ""
        try:
            for _ in range(self.max_posts):
                sent_at = self.clock.now()
                if not rules.can_start(sent_at, campaign, self.cfg, margins):
                    # Never *start* a call outside the window, not even on a re-send.
                    why = "window closed before send" if not why else f"window closed while re-sending ({why})"
                    break
                try:
                    resp = await self.provider.create_call(payload, timeout=self.post_timeout)
                except httpx.HTTPError as exc:
                    maybe_placed, why = True, f"POST {type(exc).__name__}"
                    log.warning("POST %s: %s; re-sending the same request_id", rid, type(exc).__name__)
                    continue
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                p = classify_create(resp.status_code, body)

                if p.kind == "accepted":
                    if resp.status_code == 201:
                        self._check_lag(rid, sent_at, body.get("created_at"))
                    if self.store.mark_placed(rid, p.call_sid):
                        log.info("placed %s/%s sid=%s", intent.campaign_id, intent.lead_id, p.call_sid)
                    if p.result:
                        self.store.apply_result(p.result, source="post")
                    return
                if p.kind == "rejected":
                    # A rejection is final, even after an ambiguous POST: the provider answers 200
                    # for a key that already has a call, so a 4xx means this key has none.
                    log.error("POST %s REJECTED by provider: %s", rid, p.detail)
                    self.release_rejected(rid, p.error, p.detail)
                    return
                if p.kind == "unknown":  # 504 or other 5xx
                    maybe_placed, why = True, p.detail
                    log.warning("POST %s: %s; re-sending the same request_id", rid, p.detail)
                    continue
                why = p.detail  # 503: this POST placed nothing
                log.warning("POST %s: %s; re-sending shortly", rid, p.detail)
                await asyncio.sleep(self.retry_pause)
            else:
                why = f"no definitive answer after {self.max_posts} POSTs ({why})"

            if maybe_placed:
                log.warning("POST %s unresolved (%s); will look it up", rid, why)
                self.store.mark_unknown(rid, settle_after_real=time.time() + self.store.settle_seconds,
                                        reason=why)
            else:
                log.info("POST %s placed nothing (%s); lead goes back to the queue", rid, why)
                self.store.release_intent(rid, phase="not_placed", reason=why)
        except Exception:
            # Leave the intent as it is: it keeps its slot, and the reconciler resolves it.
            log.exception("dial %s failed unexpectedly", rid)
        finally:
            self.in_flight.discard(rid)
            self.notify()

    def release_rejected(self, rid: str, code: str, detail: str) -> bool:
        """The provider refused the call (and logged a violation). Don't turn one violation
        into many: back off, believe it when it says the lead is finished, and block the
        phone if it says the phone opted out. One transaction."""
        now = self.clock.now()
        return self.store.release_intent(rid, phase="rejected", reason=detail,
                                         not_before=now + self.reject_backoff,
                                         final_state=FINAL_ON_REJECT.get(code),
                                         opted_out_at=now if code == "dnd_violation" else None)

    def _check_lag(self, rid: str, sent_at: datetime, created_at: str | None) -> None:
        """The provider stamps its own sim time on the call it accepted. If that is far from
        our sim time at send, our clock or our latency is off and the window margins can't be
        trusted: say so loudly (this is how a clock bug would show up first)."""
        if not created_at:
            return
        lag = (datetime.fromisoformat(created_at) - sent_at).total_seconds()
        if lag < -self.margins.start.total_seconds() or lag > self.margins.end.total_seconds():
            log.warning("provider accepted %s %+.0f sim-s from our send time: clock/latency trouble", rid, lag)
