"""Wiring: one asyncio process running four loops over one SQLite database.

    webhook server  (uvicorn, same event loop)  -> store.apply_result
    scheduler loop  -> store.reserve_dials -> dialer (POST in background tasks)
    reconciler loop -> resolves unknown intents and lost webhooks
    clock loop      -> asks the provider for its clock every 5 s to notice a provider restart

Startup is the same whether this is a fresh run or a restart after kill -9: the database
says where we are; unresolved calls keep their slots until the reconciler settles them.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn

from . import rules
from .config import DEFAULT_FIXTURES, load_config, load_leads
from .dialer import Dialer
from .provider import Provider
from .reconciler import Reconciler
from .report import write_final_state
from .simclock import SimClock
from .store import Store
from .webhooks import create_app

log = logging.getLogger("dispatcher")


@dataclass
class Settings:
    provider_url: str = "http://127.0.0.1:8099"
    host: str = "127.0.0.1"
    port: int = 8200
    callback_url: str | None = None  # default: http://{host}:{port}/webhooks/telephony
    fixtures: Path = DEFAULT_FIXTURES
    db_path: Path = Path("dispatcher.db")
    out_path: Path = Path("final_state.csv")
    margins: rules.Margins = field(default_factory=rules.Margins)
    tick_real: float = 0.02            # scheduler wakes at least this often (also on every change)
    reconcile_every_real: float = 0.2
    resync_every_real: float = 5.0


def provider_run_id(clock_info: dict) -> str:
    """Identifies one run of the provider process (its epoch never changes while it runs)."""
    return f"{clock_info['sim_epoch']}@{clock_info['real_epoch']}"


class SimTimeFilter(logging.Filter):
    """Stamp every log line with the simulated time, which is what the rules care about."""

    def __init__(self, clock: SimClock) -> None:
        super().__init__()
        self.clock = clock

    def filter(self, record: logging.LogRecord) -> bool:
        record.sim = self.clock.now().strftime("%a %H:%M:%S") if self.clock.synced else "--- --:--:--"
        return True


class Dispatcher:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.cfg = load_config(settings.fixtures)
        self.clock = SimClock()
        self.provider = Provider(settings.provider_url)
        self.callback_url = settings.callback_url or f"http://{settings.host}:{settings.port}/webhooks/telephony"
        self.store: Store | None = None
        self.server: uvicorn.Server | None = None
        self._wake_scheduler = asyncio.Event()
        self._wake_reconciler = asyncio.Event()
        self._done = asyncio.Event()

    def notify(self) -> None:
        """Something changed (slot freed, result applied): re-plan now instead of at the next tick."""
        self._wake_scheduler.set()
        self._wake_reconciler.set()

    # ------------------------------------------------------------ lifecycle
    async def run(self) -> int:
        tasks: list[asyncio.Task] = []
        try:
            info = await self._connect()
            log.info("provider %s: sim_now=%s speed=%sx", self.s.provider_url, info["sim_now"], info["speed"])
            self.store = self._open_store(info)
            orphans = self.store.after_restart(time.time())
            if orphans:
                log.info("%d dial(s) were interrupted by the last shutdown; resolving them", orphans)
            for problem in self.store.check_invariants():
                log.error("invariant broken at startup: %s", problem)
            self.dialer = Dialer(self.store, self.provider, self.clock, self.cfg, self.s.margins,
                                 self.callback_url, self.notify)
            self.reconciler = Reconciler(self.store, self.provider, self.clock, self.dialer, self.notify)
            # Webhooks first, so results redelivered while we were down land somewhere.
            tasks.append(await self._start_webhook_server())
            tasks += [asyncio.create_task(self._schedule_loop(), name="scheduler"),
                      asyncio.create_task(self._reconcile_loop(), name="reconciler"),
                      asyncio.create_task(self._clock_loop(), name="clock")]
            done_waiter = asyncio.create_task(self._done.wait(), name="done")
            tasks.append(done_waiter)
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if not self._done.is_set():
                for t in tasks:
                    if t.done() and not t.cancelled() and t.exception():
                        raise t.exception()
                log.warning("stopped before all leads were final")
                return 1
            out = write_final_state(self.store.final_rows(), self.s.out_path)
            log.info("all leads final: %s", self.store.status()["leads_by_campaign"])
            log.info("wrote %s", out.resolve())
            return 0
        finally:
            if self.server is not None:
                self.server.should_exit = True  # graceful stop for uvicorn; cancel the rest
            for t in tasks:
                if t.get_name() != "webhooks":
                    t.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=5)
            await self.provider.aclose()
            if self.store is not None:
                self.store.close()

    async def _connect(self) -> dict:
        warned = False
        while True:
            try:
                return await self.clock.sync(self.provider)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:  # down, or a bad answer
                if not warned:
                    log.warning("waiting for the provider at %s (%s)", self.s.provider_url, type(exc).__name__)
                    warned = True
                await asyncio.sleep(0.5)

    def _open_store(self, info: dict) -> Store:
        """The same command is right for the first start and for every restart:
        resume if the DB belongs to the provider that is running now, else start fresh.

        A provider run is identified by its epoch. Its calls live only in its memory, so a DB
        written against another provider run describes nothing real and is discarded. A DB
        for the *current* provider is never discarded: that would forget live calls."""
        db = Path(self.s.db_path)
        run_id = self.run_id = provider_run_id(info)
        store = Store(db, self.cfg)
        existing = store.get_meta("provider_run")
        if existing is not None and existing != run_id:
            store.close()
            for suffix in ("", "-wal", "-shm"):
                Path(f"{db}{suffix}").unlink(missing_ok=True)
            log.warning("%s was written against an earlier provider run (%s); starting fresh", db, existing)
            store = Store(db, self.cfg)
            existing = None
        if existing is None:
            leads = load_leads(self.cfg)
            store.initialize(leads, run_id)
            log.info("new run: loaded %d leads into %s", len(leads), db)
        else:
            st = store.status()
            log.info("resuming from %s: %d leads unfinished, open calls %s", db,
                     st["unfinished_leads"], st["open_calls_by_tenant"] or "none")
        return store

    async def _start_webhook_server(self) -> asyncio.Task:
        app = create_app(self.store, self.cfg.webhook_secret, self.notify, self._status_extra)
        config = uvicorn.Config(app, host=self.s.host, port=self.s.port, log_level="warning",
                                access_log=False, lifespan="off")
        self.server = uvicorn.Server(config)
        task = asyncio.create_task(self.server.serve(), name="webhooks")
        while not self.server.started:
            if task.done():
                task.result()
                raise RuntimeError("webhook server stopped during startup")
            await asyncio.sleep(0.01)
        log.info("webhooks on %s", self.callback_url)
        return task

    def _status_extra(self) -> dict:
        now = self.clock.now()
        next_dial: dict[str, str] = {}
        for lead in self.store.waiting_leads():
            camp = self.cfg.campaigns[lead["campaign_id"]]
            nb = lead["not_before"]
            at = rules.next_eligible(now, datetime.fromisoformat(nb) if nb else None, camp, self.cfg,
                                     self.s.margins, queue=lead["state"])
            if at is not None and (camp.campaign_id not in next_dial or at.isoformat() < next_dial[camp.campaign_id]):
                next_dial[camp.campaign_id] = at.isoformat()
        return {"sim_now": now.isoformat(), "done": self._done.is_set(),
                "posts_in_flight": len(self.dialer.in_flight), "next_dial_by_campaign": next_dial}

    # ------------------------------------------------------------ loops
    # Every store call that writes is one transaction, so after an unexpected error the DB is still
    # consistent: log it and keep going rather than let a loop die silently.
    async def _schedule_loop(self) -> None:
        while True:
            try:
                if self.store.unfinished_leads() == 0:
                    self._done.set()
                    return
                for intent in self.store.reserve_dials(self.clock.now(), time.time(), self.s.margins):
                    log.info("dialling %s/%s (%s)", intent.campaign_id, intent.lead_id, intent.queue)
                    self.dialer.start(intent)
            except Exception:
                log.exception("scheduler pass failed; retrying")
                await asyncio.sleep(0.5)
            await self._wait(self._wake_scheduler, self.s.tick_real)

    async def _reconcile_loop(self) -> None:
        last_check = time.monotonic()
        while True:
            try:
                await self.reconciler.run_once()
                if time.monotonic() - last_check > 5.0:
                    last_check = time.monotonic()
                    for problem in self.store.check_invariants():
                        log.error("invariant broken: %s", problem)
            except Exception:
                log.exception("reconciler pass failed; retrying")
                await asyncio.sleep(0.5)
            await self._wait(self._wake_reconciler, self.s.reconcile_every_real)

    async def _clock_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.resync_every_real)
            try:
                info = await self.clock.sync(self.provider)
            except Exception as exc:  # provider briefly down or a malformed answer: keep going
                log.warning("clock check failed (%s); will retry", type(exc).__name__)
                continue
            if provider_run_id(info) != self.run_id:
                # The provider restarted and lost every call it knew about. Our DB no longer
                # describes reality, so continuing could break rules. Stop loudly instead;
                # starting the dispatcher again begins a fresh run against the new provider.
                log.critical("the provider was restarted mid-run; stopping")
                return

    @staticmethod
    async def _wait(event: asyncio.Event, timeout: float) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        event.clear()
