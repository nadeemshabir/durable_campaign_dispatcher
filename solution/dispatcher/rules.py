"""Pure business rules: calling windows, what to do after a call, and who to dial next.

No I/O and no clock reads: every function takes `now` explicitly, so all of this is
unit-tested with plain datetimes (see solution/tests).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from .config import Campaign, Config, RetryPolicy, Window

# ---------------------------------------------------------------- lead states
PENDING = "pending"              # never dialled
RETRY_WAIT = "retry_wait"        # last call no_answer / busy / failed; not_before = ended_at + wait
CALLBACK_WAIT = "callback_wait"  # customer asked for a callback; not_before = callback_at
ACTIVE = "active"                # has exactly one open call (intent sent or call live)
COMPLETED = "completed"          # terminal disposition received
EXHAUSTED = "exhausted"          # max_attempts accepted calls, none terminal
DND_BLOCKED = "dnd_blocked"      # phone opted out via another lead

WAITING_STATES = (CALLBACK_WAIT, RETRY_WAIT, PENDING)  # the R6 priority order is in priority_key, not here
FINAL_STATES = frozenset({COMPLETED, EXHAUSTED, DND_BLOCKED})

TERMINAL_DISPOSITIONS = frozenset({"interested", "not_interested", "wrong_number", "dnd"})


@dataclass(frozen=True)
class Margins:
    """Safety margins in *simulated* time.

    start: wait this long after any boundary (window opens, retry due, callback due),
           so we are never early, even by a rounding error.
    end:   don't *start* a call this close to a window's end. Between our window check and
           the provider accepting the call there is latency: on a heavily loaded machine it
           was usually under ~130 sim-s (110 ms real at 1200x). 2 minutes covers almost all of
           it; the provider's grace absorbs the rest. Rare ~1 s stalls of the whole machine
           (500-1,140 sim-s) are beyond any margin.
    callback_end: the end margin for a *due callback*. Missing the window costs that customer
           a whole day (the callback was promised for now), while a fresh or retry call loses
           nothing by waiting for the next window. So callbacks may start up to 30 s before
           the end, still tolerating ~150 sim-s of latency with the provider's grace.
    """
    start: timedelta = timedelta(seconds=5)
    end: timedelta = timedelta(minutes=2)
    callback_end: timedelta = timedelta(seconds=30)

    def for_queue(self, queue: str) -> Margins:
        """The margins that apply to a lead waiting in `queue` (its state when picked)."""
        return replace(self, end=self.callback_end) if queue == CALLBACK_WAIT else self


# ---------------------------------------------------------------- R1: windows
def _day_interval(day: date, w: Window, tz) -> tuple[datetime, datetime] | None:
    if w.days is not None and day.weekday() not in w.days:
        return None
    return datetime.combine(day, w.start, tz), datetime.combine(day, w.end, tz)


def _start_bounds(day: date, campaign: Campaign, cfg: Config,
                  margins: Margins) -> tuple[datetime, datetime] | None:
    """[first, last) instants on `day` at which a call may be started: the campaign window
    intersected with the regulatory window, shrunk by the margins."""
    cw = _day_interval(day, campaign.window, cfg.tz)
    rw = _day_interval(day, cfg.regulatory, cfg.tz)
    if cw is None or rw is None:
        return None
    first = max(cw[0], rw[0]) + margins.start
    last = min(cw[1], rw[1]) - margins.end
    return (first, last) if first < last else None


def can_start(t: datetime, campaign: Campaign, cfg: Config, margins: Margins) -> bool:
    """R1: may a call for this campaign be started at instant t?"""
    local = t.astimezone(cfg.tz)
    bounds = _start_bounds(local.date(), campaign, cfg, margins)
    return bounds is not None and bounds[0] <= local < bounds[1]


def next_start(t: datetime, campaign: Campaign, cfg: Config, margins: Margins,
               horizon_days: int = 14) -> datetime | None:
    """Earliest instant >= t at which a call for this campaign may be started."""
    local = t.astimezone(cfg.tz)
    for d in range(horizon_days + 1):
        bounds = _start_bounds(local.date() + timedelta(days=d), campaign, cfg, margins)
        if bounds is None:
            continue
        candidate = max(local, bounds[0])
        if candidate < bounds[1]:
            return candidate
    return None  # campaign never opens (e.g. no allowed days)


def is_due(now: datetime, not_before: datetime | None, margins: Margins) -> bool:
    """R5/R6: has the lead's retry or callback time passed (with margin)?"""
    return not_before is None or now >= not_before + margins.start


def next_eligible(now: datetime, not_before: datetime | None, campaign: Campaign,
                  cfg: Config, margins: Margins, queue: str = PENDING) -> datetime | None:
    """When a lead waiting in `queue` may next be dialled: once it is due AND inside the
    window. A callback that falls outside the window lands on the next opening (R6).

    The scheduler dials a lead exactly when this returns `now` (it uses the same two
    checks, is_due and can_start, without the search for a future time)."""
    m = margins.for_queue(queue)
    t = now if is_due(now, not_before, m) else not_before + m.start
    return next_start(t, campaign, cfg, m)


# ---------------------------------------------------------------- R5-R9: after a call
@dataclass(frozen=True)
class Outcome:
    state: str
    not_before: datetime | None = None
    opt_out_phone: bool = False  # R8: block this phone everywhere


def decide_outcome(*, status: str | None, disposition: str | None, callback_at: datetime | None,
                   ended_at: datetime, attempts: int, max_attempts: int, phone_is_dnd: bool,
                   retry: RetryPolicy) -> Outcome:
    """Next state of a lead after one of its calls ended. `attempts` includes this call.

    The order of the checks is the R9 precedence: completed > exhausted > dnd_blocked.
    """
    if disposition in TERMINAL_DISPOSITIONS:                       # R7
        return Outcome(COMPLETED, opt_out_phone=(disposition == "dnd"))
    if attempts >= max_attempts:                                   # R4 / R9
        return Outcome(EXHAUSTED)
    if phone_is_dnd:                                               # R8 (defensive; cannot happen while R3 holds)
        return Outcome(DND_BLOCKED)
    if disposition == "callback_requested" and callback_at is not None:
        return Outcome(CALLBACK_WAIT, not_before=callback_at)      # R6
    wait = retry.wait_after(status)                                # R5, measured from ended_at
    if wait is None:
        # Unknown status/disposition, or a callback with no time: retry, but as
        # conservatively as the policy allows.
        wait = retry.longest
    return Outcome(RETRY_WAIT, not_before=ended_at + wait)


# ---------------------------------------------------------------- R2/R3/R6: who next
@dataclass(frozen=True)
class Candidate:
    campaign_id: str
    lead_id: str
    tenant_id: str
    phone: str
    state: str  # one of WAITING_STATES
    not_before: datetime | None
    seq: int
    campaign_rank: int


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def priority_key(c: Candidate) -> tuple:
    """R6: due callbacks, then due retries (each oldest-due first), then fresh leads.

    Fresh leads are interleaved across a tenant's campaigns by upload position, so one
    campaign can't starve another; campaign rank and lead id make ties deterministic.
    """
    if c.state == PENDING:
        return (2, c.seq, c.campaign_rank, c.lead_id)
    rank = 0 if c.state == CALLBACK_WAIT else 1
    return (rank, c.not_before or _EPOCH, c.campaign_rank, c.lead_id)


def plan_dispatch(now: datetime, candidates: Iterable[Candidate], free_slots: dict[str, int],
                  busy_phones: set[str], dnd_phones: set[str], cfg: Config,
                  margins: Margins) -> list[Candidate]:
    """Choose which waiting leads to dial right now.

    free_slots:  tenant_id -> max_concurrent_calls minus that tenant's open calls (R2)
    busy_phones: phones with an open call in any campaign or tenant (R3)
    """
    free = dict(free_slots)
    busy = set(busy_phones)
    is_open = {(cid, queue): can_start(now, camp, cfg, margins.for_queue(queue))
               for cid, camp in cfg.campaigns.items() for queue in WAITING_STATES}
    picks: list[Candidate] = []
    for c in sorted(candidates, key=priority_key):
        if free.get(c.tenant_id, 0) <= 0 or not is_open[(c.campaign_id, c.state)]:
            continue
        if c.phone in busy or c.phone in dnd_phones:
            continue  # skip, don't block: the next lead may still use the slot
        if not is_due(now, c.not_before, margins):
            continue
        picks.append(c)
        busy.add(c.phone)  # two leads sharing a phone can't both be picked in one pass
        free[c.tenant_id] -= 1
    return picks
