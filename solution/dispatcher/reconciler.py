"""Resolve everything we are unsure about by asking the provider.

* Intents whose POST outcome is unknown (the dialer gave up, or we crashed mid-POST):
  GET /v1/calls?request_id=...
      found              -> placed
      404, window open   -> hand it back to the dialer to re-send the SAME request_id
                            (safe: the provider makes at most one call per key)
      404, window closed -> never placed, once a POST of ours can't still be in flight
* Placed calls whose result webhook never came (lost by the provider, or we were down).
  GET /v1/calls/{sid}  ended -> apply the result exactly as a webhook would.

Both paths call the same idempotent store methods as the happy path, so a webhook racing
a poll is harmless: whichever lands second is a no-op.

This loop also is crash recovery: after a restart, every unresolved row in the DB is
simply picked up here. Scheduling doesn't wait for it, because unresolved calls keep
holding their slot and phone until they are resolved.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable

import httpx

from . import rules
from .dialer import Dialer
from .provider import Provider
from .simclock import Clock
from .store import CallIntent, Store

log = logging.getLogger(__name__)


class Reconciler:
    def __init__(self, store: Store, provider: Provider, clock: Clock, dialer: Dialer,
                 notify: Callable[[], None], poll_after: timedelta = timedelta(minutes=8),
                 poll_every_real: float = 1.0) -> None:
        self.store, self.provider, self.clock, self.dialer = store, provider, clock, dialer
        self.notify = notify
        # The longest call the provider simulates is ~6.5 sim minutes, so a placed call older
        # than this without a result has almost certainly lost its webhook.
        self.poll_after = poll_after
        self.poll_every_real = poll_every_real
        self._last_poll: dict[str, float] = {}

    async def run_once(self) -> None:
        for row in self.store.open_intents():
            if row["request_id"] not in self.dialer.in_flight:  # never second-guess a live POST
                await self.resolve_intent(row)

        now = self.clock.now()
        placed = self.store.placed_calls()
        for row in placed:
            if now - datetime.fromisoformat(row["created_sim"]) < self.poll_after:
                continue
            rid = row["request_id"]
            if time.time() - self._last_poll.get(rid, 0.0) < self.poll_every_real:
                continue
            self._last_poll[rid] = time.time()
            await self.poll_call(row)
        live = {row["request_id"] for row in placed}
        self._last_poll = {k: v for k, v in self._last_poll.items() if k in live}

    async def resolve_intent(self, row) -> None:
        rid = row["request_id"]
        try:
            resp = await self.provider.find_call(rid)
        except httpx.HTTPError as exc:
            log.warning("lookup of %s failed (%s); will retry", rid, type(exc).__name__)
            return
        if resp.status_code == 200:
            body = resp.json()
            if self.store.mark_placed(rid, body["call_sid"]):
                log.info("resolved %s: the call WAS placed (sid=%s)", rid, body["call_sid"])
            if body.get("result"):
                self.store.apply_result(body["result"], source="lookup")
            self.notify()
        elif resp.status_code == 404:
            row = self.store.call(rid)  # re-read: a dial that was in flight when this pass started may have changed it
            if row is None or row["phase"] != "intent" or rid in self.dialer.in_flight:
                return
            campaign = self.store.cfg.campaigns[row["campaign_id"]]
            margins = self.dialer.margins.for_queue(row["lead_state_before"])
            if rules.can_start(self.clock.now(), campaign, self.store.cfg, margins):
                # Still inside the window: finish the dial instead of waiting to be sure.
                # Re-sending the same request_id can't create a second call.
                log.info("resuming %s with the same request_id", rid)
                self.dialer.start(CallIntent(rid, row["campaign_id"], row["lead_id"], row["tenant_id"],
                                             row["phone"], row["lead_state_before"]), resumed=True)
            elif time.time() >= row["settle_after_real"]:
                # Window closed. A 404 proves "never placed" only once no POST of ours can still
                # be on its way to the provider.
                if self.store.release_intent(rid, phase="not_placed", reason="not found at provider"):
                    log.info("resolved %s: never placed; lead goes back to the queue", rid)
                    self.notify()
        else:
            log.warning("lookup of %s returned %s; will retry", rid, resp.status_code)

    async def poll_call(self, row) -> None:
        sid = row["call_sid"]
        try:
            resp = await self.provider.get_call(sid)
        except httpx.HTTPError as exc:
            log.warning("poll of %s failed (%s); will retry", sid, type(exc).__name__)
            return
        if resp.status_code == 200:
            body = resp.json()
            if body.get("state") == "ended" and body.get("result"):
                if self.store.apply_result(body["result"], source="poll") == "applied":
                    log.warning("recovered missing webhook for %s (%s)", row["request_id"], sid)
                    self.notify()
        else:
            log.error("poll of %s returned %s", sid, resp.status_code)
