"""SQLite store: the single source of truth.

Process memory is disposable. After `kill -9` at any line, this database must describe a
state we can safely resume from. Three ideas make that true:

1. **Write-ahead intent.** A `calls` row (phase 'intent', with our request_id) is committed
   *before* the POST that might place the call. From that commit on, the call holds its
   tenant slot and its phone, crash or not.
2. **Open calls hold resources.** A call is "open" while it is 'intent' or 'placed'.
   Concurrency (R2) and phone exclusivity (R3) are computed from open calls, so an
   ambiguous call (504, crash mid-POST) keeps blocking until we *know* it is gone.
   That costs throughput, never a violation.
3. **One synchronous transaction per decision.** Every method below that changes state
   reads, decides and writes inside a single BEGIN IMMEDIATE ... COMMIT. No method has an
   `await`, so the asyncio tasks of this process can't interleave inside a decision either.

Call phases:  intent -> placed -> ended
                 |---------------> ended      (webhook arrived before the 201 did)
                 '-> not_placed | rejected     (provably never placed)
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from . import rules
from .config import Config, Lead

log = logging.getLogger(__name__)

# The two definitions everything else hangs on. Written once, used in every query.
OPEN = "('intent', 'placed')"     # the call may be live: it holds its tenant slot and phone
ACCEPTED = "('placed', 'ended')"  # the provider accepted it: it counts as an attempt (R4)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    campaign_id TEXT NOT NULL,
    lead_id     TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    phone       TEXT NOT NULL,
    seq         INTEGER NOT NULL,          -- row position in the uploaded CSV
    state       TEXT NOT NULL CHECK (state IN ('pending', 'retry_wait', 'callback_wait', 'active',
                                               'completed', 'exhausted', 'dnd_blocked')),
    not_before  TEXT,                      -- sim time: retry due / callback_at / backoff
    PRIMARY KEY (campaign_id, lead_id)
);
CREATE INDEX IF NOT EXISTS leads_by_phone ON leads (phone);

CREATE TABLE IF NOT EXISTS calls (
    request_id        TEXT PRIMARY KEY,    -- our idempotency key, committed BEFORE the POST
    campaign_id       TEXT NOT NULL,
    lead_id           TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    phone             TEXT NOT NULL,
    phase             TEXT NOT NULL CHECK (phase IN ('intent', 'placed', 'ended',
                                                     'not_placed', 'rejected')),
    lead_state_before TEXT NOT NULL,       -- where the lead goes back to if never placed
    call_sid          TEXT UNIQUE,
    created_sim       TEXT NOT NULL,
    settle_after_real REAL NOT NULL,       -- wall clock: don't trust a lookup 404 before this
    status            TEXT,
    disposition       TEXT,
    callback_at       TEXT,
    ended_at          TEXT,
    error             TEXT,
    FOREIGN KEY (campaign_id, lead_id) REFERENCES leads (campaign_id, lead_id)
);
CREATE INDEX IF NOT EXISTS calls_by_lead ON calls (campaign_id, lead_id);
CREATE INDEX IF NOT EXISTS calls_by_phase ON calls (phase);
-- R3 enforced by the database itself: at most one open call per phone, and per lead.
CREATE UNIQUE INDEX IF NOT EXISTS one_open_call_per_phone ON calls (phone) WHERE phase IN {OPEN};
CREATE UNIQUE INDEX IF NOT EXISTS one_open_call_per_lead ON calls (campaign_id, lead_id) WHERE phase IN {OPEN};

CREATE TABLE IF NOT EXISTS dnd_phones (    -- R8: global, across campaigns and tenants
    phone     TEXT PRIMARY KEY,
    since_sim TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class CallIntent:
    request_id: str
    campaign_id: str
    lead_id: str
    tenant_id: str
    phone: str
    queue: str  # the lead's state when picked: callback_wait / retry_wait / pending


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Store:
    def __init__(self, path: str | Path, cfg: Config, settle_seconds: float = 3.0) -> None:
        self.cfg = cfg
        self.settle_seconds = settle_seconds
        # isolation_level=None: no implicit transactions; we BEGIN/COMMIT explicitly.
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL: a committed transaction survives the process dying (kill -9), because
        # it is already in the OS's copy of the WAL file. Only an OS crash / power loss could
        # lose the last few commits, and that would also wipe the provider's in-memory state.
        # FULL would fsync on every commit: 2-25 ms of blocked event loop each time on this
        # disk (25 ms = 30 sim-s at 1200x), which is worse for the timing rules.
        self.db.execute("PRAGMA synchronous=NORMAL")
        # The one fsync NORMAL still does is the automatic WAL checkpoint (measured: up to
        # 12 ms stalls on an idle disk). Turn it off: a run adds a few tens of MB of WAL,
        # SQLite folds it in on close, and after kill -9 the next open replays it.
        self.db.execute("PRAGMA wal_autocheckpoint=0")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # ------------------------------------------------------------ setup
    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def initialize(self, leads: list[Lead], provider_run: str) -> None:
        """Load the leads once. One transaction, so a crash here leaves an empty DB."""
        with self.tx():
            for lead in leads:
                camp = self.cfg.campaigns[lead.campaign_id]
                self.db.execute(
                    "INSERT INTO leads (campaign_id, lead_id, tenant_id, phone, seq, state)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (lead.campaign_id, lead.lead_id, camp.tenant_id, lead.phone, lead.seq, rules.PENDING))
            self.db.execute("INSERT INTO meta (key, value) VALUES ('provider_run', ?)", (provider_run,))

    def after_restart(self, now_real: float) -> int:
        """Called once at startup. A POST sent by the killed process can't be newer than
        this moment, so no open intent may trust a lookup 404 sooner than settle_seconds
        from now. Returns how many intents were left open by the previous process."""
        with self.tx():
            return self.db.execute(
                "UPDATE calls SET settle_after_real = MAX(settle_after_real, ?) WHERE phase = 'intent'",
                (now_real + self.settle_seconds,)).rowcount

    # ------------------------------------------------------------ dispatch
    def reserve_dials(self, now: datetime, now_real: float, margins: rules.Margins) -> list[CallIntent]:
        """Pick leads to dial now and durably reserve their slot + phone (the intent).

        The caller sends the POSTs after this returns, i.e. strictly after the commit.
        """
        cfg = self.cfg
        with self.tx():
            open_calls = self.db.execute(f"SELECT tenant_id, phone FROM calls WHERE phase IN {OPEN}").fetchall()
            in_use = Counter(r["tenant_id"] for r in open_calls)
            free = {t.tenant_id: t.max_concurrent_calls - in_use[t.tenant_id] for t in cfg.tenants.values()}
            if all(n <= 0 for n in free.values()):
                return []
            busy = {r["phone"] for r in open_calls}
            dnd = {r["phone"] for r in self.db.execute("SELECT phone FROM dnd_phones")}
            candidates = [
                rules.Candidate(campaign_id=r["campaign_id"], lead_id=r["lead_id"], tenant_id=r["tenant_id"],
                                phone=r["phone"], state=r["state"], not_before=_dt(r["not_before"]),
                                seq=r["seq"], campaign_rank=cfg.campaigns[r["campaign_id"]].rank)
                for r in self.waiting_leads()
            ]
            intents: list[CallIntent] = []
            for c in rules.plan_dispatch(now, candidates, free, busy, dnd, cfg, margins):
                if self.attempts(c.campaign_id, c.lead_id) >= cfg.campaigns[c.campaign_id].max_attempts:
                    # Can't happen (the 3rd non-terminal result sets 'exhausted'); never dial it anyway.
                    log.error("%s/%s waiting with no attempts left; marking exhausted", c.campaign_id, c.lead_id)
                    self.db.execute("UPDATE leads SET state = ? WHERE campaign_id = ? AND lead_id = ?",
                                    (rules.EXHAUSTED, c.campaign_id, c.lead_id))
                    continue
                rid = f"{c.campaign_id}:{c.lead_id}:{uuid.uuid4().hex[:12]}"
                self.db.execute(
                    "INSERT INTO calls (request_id, campaign_id, lead_id, tenant_id, phone, phase,"
                    " lead_state_before, created_sim, settle_after_real)"
                    " VALUES (?, ?, ?, ?, ?, 'intent', ?, ?, ?)",
                    (rid, c.campaign_id, c.lead_id, c.tenant_id, c.phone, c.state,
                     now.isoformat(), now_real + self.settle_seconds))
                cur = self.db.execute(
                    "UPDATE leads SET state = 'active' WHERE campaign_id = ? AND lead_id = ? AND state = ?",
                    (c.campaign_id, c.lead_id, c.state))
                if cur.rowcount != 1:
                    raise RuntimeError(f"lead {c.campaign_id}/{c.lead_id} changed state during reservation")
                intents.append(CallIntent(rid, c.campaign_id, c.lead_id, c.tenant_id, c.phone, c.state))
            return intents

    # ------------------------------------------------------------ outcome of the POST
    def mark_placed(self, request_id: str, call_sid: str) -> bool:
        """The provider accepted the call (201, 200 replay, or found by request_id)."""
        with self.tx():
            cur = self.db.execute(
                "UPDATE calls SET phase = 'placed', call_sid = ? WHERE request_id = ? AND phase = 'intent'",
                (call_sid, request_id))
            if cur.rowcount == 1:
                return True
            row = self.call(request_id)
            if row is None:
                log.error("mark_placed: unknown request_id %s", request_id)
            elif row["phase"] in ("not_placed", "rejected"):
                # Only reachable if a 404 lied to us. Loud, because our slot accounting is now off.
                log.critical("provider placed %s (%s) but we had released it as %s",
                             request_id, call_sid, row["phase"])
            # 'placed'/'ended': already known (e.g. the webhook beat the 201). Nothing to do.
            return False

    def mark_unknown(self, request_id: str, *, settle_after_real: float, reason: str) -> None:
        """Outcome unknown (504s, timeouts, window closed mid-way). The intent keeps holding
        its slot and phone; the reconciler resolves it by looking up the request_id."""
        with self.tx():
            self.db.execute(
                "UPDATE calls SET settle_after_real = ?, error = ? WHERE request_id = ? AND phase = 'intent'",
                (settle_after_real, reason, request_id))

    def release_intent(self, request_id: str, *, phase: str, reason: str,
                       not_before: datetime | None = None, final_state: str | None = None,
                       opted_out_at: datetime | None = None) -> bool:
        """The call was provably never placed: free the slot and phone, and put the lead
        back where it was. `not_before` optionally delays the next attempt (backoff);
        `final_state` instead ends the lead (provider says it is already finished);
        `opted_out_at` records that the provider says this phone is DND (same transaction,
        so a crash can't keep the release and lose the opt-out)."""
        assert phase in ("not_placed", "rejected")
        assert final_state is None or final_state in rules.FINAL_STATES
        with self.tx():
            row = self.call(request_id)
            if row is None or row["phase"] != "intent":
                return False
            self.db.execute("UPDATE calls SET phase = ?, error = ? WHERE request_id = ?",
                            (phase, reason, request_id))
            lead = self.lead(row["campaign_id"], row["lead_id"])
            nb = _dt(lead["not_before"])
            if not_before is not None and (nb is None or not_before > nb):
                nb = not_before
            self.db.execute(
                "UPDATE leads SET state = ?, not_before = ? WHERE campaign_id = ? AND lead_id = ? AND state = 'active'",
                (final_state or row["lead_state_before"], nb.isoformat() if nb else None,
                 row["campaign_id"], row["lead_id"]))
            if opted_out_at is not None:
                self._opt_out(row["phone"], opted_out_at)
            return True

    # ------------------------------------------------------------ outcome of the call
    def apply_result(self, result: dict, *, source: str) -> str:
        """Apply a finished call's result, from a webhook or from polling. Idempotent:
        the second delivery of the same call finds phase 'ended' and changes nothing.

        Returns 'applied' | 'duplicate' | 'unknown' | 'conflict'.
        """
        rid, sid = result.get("request_id"), result.get("call_sid")
        with self.tx():
            call = self.call(rid) if rid else None
            if call is None:
                log.warning("%s result for unknown call request_id=%s call_sid=%s: ignored", source, rid, sid)
                return "unknown"
            if call["phase"] == "ended":
                return "duplicate"
            if call["phase"] not in ("intent", "placed"):
                log.critical("%s result for %s which we had marked %s", source, call["request_id"], call["phase"])
                return "conflict"

            ended_at = _dt(result["ended_at"])
            callback_at = _dt(result.get("callback_at"))
            status, disposition = result.get("status"), result.get("disposition")
            self.db.execute(
                "UPDATE calls SET phase = 'ended', call_sid = COALESCE(call_sid, ?), status = ?,"
                " disposition = ?, callback_at = ?, ended_at = ? WHERE request_id = ?",
                (sid, status, disposition, callback_at.isoformat() if callback_at else None,
                 ended_at.isoformat(), call["request_id"]))

            cid, lid, phone = call["campaign_id"], call["lead_id"], call["phone"]
            lead = self.lead(cid, lid)
            if lead["state"] != rules.ACTIVE:
                log.error("%s/%s had an open call but was in state %s", cid, lid, lead["state"])
            if status == "completed" and disposition not in rules.TERMINAL_DISPOSITIONS \
                    and disposition != "callback_requested":
                log.warning("%s/%s: unexpected disposition %r, treating as a retry", cid, lid, disposition)

            attempts = self.attempts(cid, lid)
            outcome = rules.decide_outcome(
                status=status, disposition=disposition, callback_at=callback_at, ended_at=ended_at,
                attempts=attempts, max_attempts=self.cfg.campaigns[cid].max_attempts,
                phone_is_dnd=self._is_dnd(phone), retry=self.cfg.retry)
            self.db.execute(
                "UPDATE leads SET state = ?, not_before = ? WHERE campaign_id = ? AND lead_id = ?",
                (outcome.state, outcome.not_before.isoformat() if outcome.not_before else None, cid, lid))
            log.info("%s/%s attempt %d ended %s/%s -> %s%s [%s]", cid, lid, attempts, status,
                     disposition or "-", outcome.state,
                     f" until {outcome.not_before:%a %H:%M}" if outcome.not_before else "", source)
            if outcome.opt_out_phone:
                self._opt_out(phone, ended_at)
            return "applied"

    def _opt_out(self, phone: str, since: datetime) -> None:
        """R8: block the phone everywhere. Leads that are still waiting become dnd_blocked;
        completed/exhausted leads keep their state (R9 precedence). No *other* lead can be
        mid-call on this phone: R3 allows one open call per phone."""
        self.db.execute("INSERT OR IGNORE INTO dnd_phones (phone, since_sim) VALUES (?, ?)",
                        (phone, since.isoformat()))
        blocked = self.db.execute(
            "SELECT campaign_id, lead_id FROM leads WHERE phone = ? AND state IN (?, ?, ?)",
            (phone, *rules.WAITING_STATES)).fetchall()
        for r in blocked:
            self.db.execute("UPDATE leads SET state = ? WHERE campaign_id = ? AND lead_id = ?",
                            (rules.DND_BLOCKED, r["campaign_id"], r["lead_id"]))
            log.info("%s/%s -> dnd_blocked (phone opted out)", r["campaign_id"], r["lead_id"])

    # ------------------------------------------------------------ reads
    def call(self, request_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM calls WHERE request_id = ?", (request_id,)).fetchone()

    def lead(self, campaign_id: str, lead_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM leads WHERE campaign_id = ? AND lead_id = ?",
                               (campaign_id, lead_id)).fetchone()

    def attempts(self, campaign_id: str, lead_id: str) -> int:
        """R4: calls the provider accepted."""
        return self.db.execute(
            f"SELECT COUNT(*) FROM calls WHERE campaign_id = ? AND lead_id = ? AND phase IN {ACCEPTED}",
            (campaign_id, lead_id)).fetchone()[0]

    def _is_dnd(self, phone: str) -> bool:
        return self.db.execute("SELECT 1 FROM dnd_phones WHERE phone = ?", (phone,)).fetchone() is not None

    def waiting_leads(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM leads WHERE state IN (?, ?, ?)", rules.WAITING_STATES).fetchall()

    def open_intents(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM calls WHERE phase = 'intent' ORDER BY rowid").fetchall()

    def placed_calls(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM calls WHERE phase = 'placed' ORDER BY rowid").fetchall()

    def unfinished_leads(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM leads WHERE state NOT IN (?, ?, ?)",
                               tuple(rules.FINAL_STATES)).fetchone()[0]

    def final_rows(self) -> list[tuple]:
        """Rows for final_state.csv, sorted by campaign_id, lead_id. 'Most recent ended call'
        is the latest by insertion order: a lead's calls never overlap (R3), so that is
        also the latest by ended_at."""
        rows = self.db.execute(f"""
            SELECT l.campaign_id, l.lead_id, l.state,
                   (SELECT COUNT(*) FROM calls c
                     WHERE c.campaign_id = l.campaign_id AND c.lead_id = l.lead_id
                       AND c.phase IN {ACCEPTED}) AS attempts,
                   (SELECT c.status FROM calls c
                     WHERE c.campaign_id = l.campaign_id AND c.lead_id = l.lead_id AND c.phase = 'ended'
                     ORDER BY c.rowid DESC LIMIT 1) AS last_status,
                   (SELECT c.disposition FROM calls c
                     WHERE c.campaign_id = l.campaign_id AND c.lead_id = l.lead_id AND c.phase = 'ended'
                     ORDER BY c.rowid DESC LIMIT 1) AS last_disposition
            FROM leads l
            ORDER BY l.campaign_id, l.lead_id""").fetchall()
        return [(r["campaign_id"], r["lead_id"], r["state"], r["attempts"],
                 r["last_status"] or "", r["last_disposition"] or "") for r in rows]

    def status(self) -> dict:
        by_campaign: dict[str, dict[str, int]] = {}
        for r in self.db.execute("SELECT campaign_id, state, COUNT(*) AS n FROM leads GROUP BY 1, 2"):
            by_campaign.setdefault(r["campaign_id"], {})[r["state"]] = r["n"]
        open_by_tenant = {r["tenant_id"]: r["n"] for r in self.db.execute(
            f"SELECT tenant_id, COUNT(*) AS n FROM calls WHERE phase IN {OPEN} GROUP BY 1")}
        calls_by_phase = {r["phase"]: r["n"] for r in self.db.execute(
            "SELECT phase, COUNT(*) AS n FROM calls GROUP BY 1")}
        return {
            "leads_by_campaign": by_campaign,
            "open_calls_by_tenant": open_by_tenant,
            "calls_by_phase": calls_by_phase,
            "dnd_phones": self.db.execute("SELECT COUNT(*) FROM dnd_phones").fetchone()[0],
            "unfinished_leads": self.unfinished_leads(),
        }

    def check_invariants(self) -> list[str]:
        """Cross-checks that should always hold. Run at startup, periodically and in tests."""
        problems: list[str] = []
        for r in self.db.execute(f"""
                SELECT l.campaign_id, l.lead_id, l.state,
                       (SELECT COUNT(*) FROM calls c WHERE c.campaign_id = l.campaign_id
                          AND c.lead_id = l.lead_id AND c.phase IN {OPEN}) AS open_calls
                FROM leads l"""):
            if (r["state"] == rules.ACTIVE) != (r["open_calls"] == 1) or r["open_calls"] > 1:
                problems.append(f"{r['campaign_id']}/{r['lead_id']}: state={r['state']} open_calls={r['open_calls']}")
        for r in self.db.execute(f"SELECT tenant_id, COUNT(*) AS n FROM calls WHERE phase IN {OPEN} GROUP BY 1"):
            limit = self.cfg.tenants[r["tenant_id"]].max_concurrent_calls
            if r["n"] > limit:
                problems.append(f"tenant {r['tenant_id']}: {r['n']} open calls > limit {limit}")
        for r in self.db.execute(
                f"SELECT campaign_id, lead_id, COUNT(*) AS n FROM calls WHERE phase IN {ACCEPTED} GROUP BY 1, 2"):
            limit = self.cfg.campaigns[r["campaign_id"]].max_attempts
            if r["n"] > limit:
                problems.append(f"{r['campaign_id']}/{r['lead_id']}: {r['n']} attempts > {limit}")
        for r in self.db.execute(
                f"SELECT c.request_id FROM calls c JOIN dnd_phones d ON d.phone = c.phone WHERE c.phase IN {OPEN}"):
            problems.append(f"open call {r['request_id']} on an opted-out phone")
        return problems
