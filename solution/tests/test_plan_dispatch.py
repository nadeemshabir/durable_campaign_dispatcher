"""R2/R3/R6: which waiting leads get dialled, in which order."""
from __future__ import annotations

from datetime import datetime

from solution.dispatcher.rules import CALLBACK_WAIT, PENDING, RETRY_WAIT, Candidate, plan_dispatch

from .conftest import ist

NOW = ist("2026-11-12 17:30")


def cand(cfg, cid: str, lid: str, phone: str | None = None, state: str = PENDING,
         not_before: datetime | None = None, seq: int = 0) -> Candidate:
    camp = cfg.campaigns[cid]
    return Candidate(campaign_id=cid, lead_id=lid, tenant_id=camp.tenant_id, phone=phone or f"+91{lid}",
                     state=state, not_before=not_before, seq=seq, campaign_rank=camp.rank)


def plan(cfg, margins, cands, free=None, busy=(), dnd=(), now=NOW):
    free = free if free is not None else {"acme_insurance": 4, "zen_retail": 3}
    return [c.lead_id for c in plan_dispatch(now, cands, free, set(busy), set(dnd), cfg, margins)]


def test_due_callbacks_then_retries_then_fresh(cfg, margins):
    cands = [
        cand(cfg, "c_acme_renewals", "fresh", seq=0),
        cand(cfg, "c_acme_renewals", "retry", state=RETRY_WAIT, not_before=ist("2026-11-12 17:00")),
        cand(cfg, "c_acme_feedback", "cb_late", state=CALLBACK_WAIT, not_before=ist("2026-11-12 17:20")),
        cand(cfg, "c_acme_renewals", "cb_early", state=CALLBACK_WAIT, not_before=ist("2026-11-12 17:10")),
    ]
    assert plan(cfg, margins, cands, free={"acme_insurance": 2}) == ["cb_early", "cb_late"]
    assert plan(cfg, margins, cands, free={"acme_insurance": 3}) == ["cb_early", "cb_late", "retry"]
    assert plan(cfg, margins, cands) == ["cb_early", "cb_late", "retry", "fresh"]


def test_retries_oldest_due_first(cfg, margins):
    cands = [cand(cfg, "c_acme_renewals", f"r{m}", state=RETRY_WAIT, not_before=ist(f"2026-11-12 17:{m:02d}"))
             for m in (25, 5, 15)]
    assert plan(cfg, margins, cands, free={"acme_insurance": 2}) == ["r5", "r15"]


def test_not_yet_due_is_skipped_until_due_plus_margin(cfg, margins):
    retry = cand(cfg, "c_acme_renewals", "r", state=RETRY_WAIT, not_before=ist("2026-11-12 17:35"))
    assert plan(cfg, margins, [retry]) == []
    assert plan(cfg, margins, [retry], now=ist("2026-11-12 17:35:04")) == []
    assert plan(cfg, margins, [retry], now=ist("2026-11-12 17:35:05")) == ["r"]


def test_tenant_limit_is_shared_across_its_campaigns(cfg, margins):
    cands = ([cand(cfg, "c_acme_renewals", f"ren{i}", seq=i) for i in range(3)]
             + [cand(cfg, "c_acme_feedback", f"fb{i}", seq=i) for i in range(3)]
             + [cand(cfg, "c_zen_winback", f"zen{i}", seq=i) for i in range(5)])
    picked = plan(cfg, margins, cands)
    assert sum(p.startswith(("ren", "fb")) for p in picked) == 4
    assert sum(p.startswith("zen") for p in picked) == 3


def test_fresh_leads_interleave_campaigns_by_upload_position(cfg, margins):
    cands = ([cand(cfg, "c_acme_renewals", f"ren{i}", seq=i) for i in range(4)]
             + [cand(cfg, "c_acme_feedback", f"fb{i}", seq=i) for i in range(4)])
    assert plan(cfg, margins, cands) == ["ren0", "fb0", "ren1", "fb1"]


def test_busy_phone_is_skipped_without_blocking_the_queue(cfg, margins):
    cands = [cand(cfg, "c_acme_renewals", "cb", phone="+911", state=CALLBACK_WAIT,
                  not_before=ist("2026-11-12 17:00")),
             cand(cfg, "c_acme_renewals", "fresh", phone="+912")]
    assert plan(cfg, margins, cands, free={"acme_insurance": 1}, busy={"+911"}) == ["fresh"]


def test_two_leads_sharing_a_phone_are_never_picked_together(cfg, margins):
    shared = "+917695793186"  # ren_0018 and fb_0011 in the fixtures
    cands = [cand(cfg, "c_acme_renewals", "ren_0018", phone=shared, seq=17),
             cand(cfg, "c_acme_feedback", "fb_0011", phone=shared, seq=0),
             cand(cfg, "c_acme_feedback", "other", seq=1)]
    picked = plan(cfg, margins, cands)
    assert len(picked) == 2 and "other" in picked
    assert ("ren_0018" in picked) != ("fb_0011" in picked)


def test_opted_out_phone_is_never_picked(cfg, margins):
    cands = [cand(cfg, "c_acme_renewals", "a", phone="+91dnd")]
    assert plan(cfg, margins, cands, dnd={"+91dnd"}) == []


def test_closed_campaign_is_skipped_while_the_tenants_other_campaign_dials(cfg, margins):
    at_1930 = ist("2026-11-12 19:30")  # renewals closed at 19:00, feedback open until 20:30
    cands = [cand(cfg, "c_acme_renewals", "ren", seq=0), cand(cfg, "c_acme_feedback", "fb", seq=5)]
    assert plan(cfg, margins, cands, now=at_1930) == ["fb"]


def test_due_callback_outside_window_waits_for_opening(cfg, margins):
    cb = cand(cfg, "c_acme_renewals", "cb", state=CALLBACK_WAIT, not_before=ist("2026-11-12 19:30"))
    assert plan(cfg, margins, [cb], now=ist("2026-11-12 20:00")) == []
    assert plan(cfg, margins, [cb], now=ist("2026-11-13 10:00:05")) == ["cb"]


def test_due_callback_in_the_last_minute_is_still_called_but_a_retry_is_not(cfg, margins):
    at = ist("2026-11-12 18:59:05")  # renewals closes at 19:00
    cb = cand(cfg, "c_acme_renewals", "cb", state=CALLBACK_WAIT, not_before=ist("2026-11-12 18:59"))
    retry = cand(cfg, "c_acme_renewals", "retry", state=RETRY_WAIT, not_before=ist("2026-11-12 18:59"))
    assert plan(cfg, margins, [cb, retry], now=at) == ["cb"]


def test_scheduler_dials_exactly_when_next_eligible_says_now(cfg, margins):
    """plan_dispatch and next_eligible must agree, so a rule change in one can't silently
    diverge from the other (with free slots and no phone conflicts)."""
    from datetime import timedelta

    from solution.dispatcher.rules import next_eligible
    offsets = [None, -120, -5, -4, 0, 30, 600]  # not_before relative to now, in sim seconds
    now = ist("2026-11-12 08:00")
    while now < ist("2026-11-16 22:00"):
        for cid in cfg.campaigns:
            for off in offsets:
                nb = None if off is None else now + timedelta(seconds=off)
                for queue in ([PENDING] if nb is None else [RETRY_WAIT, CALLBACK_WAIT]):
                    c = cand(cfg, cid, "x", state=queue, not_before=nb)
                    picked = bool(plan(cfg, margins, [c], now=now))
                    expected = next_eligible(now, nb, cfg.campaigns[cid], cfg, margins, queue) == now
                    assert picked == expected, (cid, queue, now, off)
        now += timedelta(minutes=17, seconds=31)


def test_no_free_slots_means_no_picks(cfg, margins):
    cands = [cand(cfg, "c_acme_renewals", "a")]
    assert plan(cfg, margins, cands, free={"acme_insurance": 0, "zen_retail": 3}) == []
