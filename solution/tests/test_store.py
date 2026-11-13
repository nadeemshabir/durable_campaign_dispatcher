"""The durable state machine: reservations, results, idempotency and crash safety."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from solution.dispatcher import store as store_module
from solution.dispatcher.config import load_leads
from solution.dispatcher.rules import (ACTIVE, CALLBACK_WAIT, COMPLETED, DND_BLOCKED, EXHAUSTED, PENDING,
                                       RETRY_WAIT)
from solution.dispatcher.store import Store

from .conftest import ended, ist, only_leads

T0 = ist("2026-11-12 17:30")
SHARED_PHONE = "+917695793186"  # ren_0018 and fb_0011


def state(store, cid, lid):
    return store.lead(cid, lid)["state"]


def test_first_tick_fills_each_tenant_exactly_to_its_limit(store, margins):
    intents = store.reserve_dials(T0, 0.0, margins)
    tenants = [i.tenant_id for i in intents]
    assert tenants.count("acme_insurance") == 4 and tenants.count("zen_retail") == 3
    assert store.reserve_dials(T0, 0.0, margins) == []  # nothing frees up by itself
    for i in intents:
        assert state(store, i.campaign_id, i.lead_id) == ACTIVE
        assert store.call(i.request_id)["phase"] == "intent"
    assert store.check_invariants() == []


def test_two_leads_with_one_phone_are_never_open_together(store, margins):
    only_leads(store, ("c_acme_renewals", "ren_0018"), ("c_acme_feedback", "fb_0011"))
    [it] = store.reserve_dials(T0, 0.0, margins)
    assert it.phone == SHARED_PHONE
    assert store.reserve_dials(T0 + timedelta(minutes=30), 0.0, margins) == []


def test_database_itself_refuses_a_second_open_call_on_a_phone(store, margins):
    only_leads(store, ("c_acme_feedback", "fb_0011"))
    store.reserve_dials(T0, 0.0, margins)
    with pytest.raises(sqlite3.IntegrityError):
        with store.tx():
            store.db.execute(
                "INSERT INTO calls (request_id, campaign_id, lead_id, tenant_id, phone, phase,"
                " lead_state_before, created_sim, settle_after_real)"
                " VALUES ('sneaky', 'c_acme_renewals', 'ren_0018', 'acme_insurance', ?, 'intent',"
                " 'pending', ?, 0)", (SHARED_PHONE, T0.isoformat()))
    assert store.check_invariants() == []


def test_reservation_is_all_or_nothing(store, margins, monkeypatch):
    calls = {"n": 0}
    real_uuid4 = store_module.uuid.uuid4

    def flaky_uuid4():
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("process dies mid-reservation")
        return real_uuid4()

    monkeypatch.setattr(store_module.uuid, "uuid4", flaky_uuid4)
    with pytest.raises(RuntimeError):
        store.reserve_dials(T0, 0.0, margins)
    assert store.open_intents() == []
    assert store.status()["leads_by_campaign"]["c_acme_renewals"] == {PENDING: 70}


def test_result_is_applied_once_and_duplicates_are_harmless(store, margins):
    only_leads(store, ("c_acme_renewals", "ren_0001"))
    [it] = store.reserve_dials(T0, 0.0, margins)
    assert store.mark_placed(it.request_id, "CA1")
    result = ended(it, "no_answer", None, ended_at=ist("2026-11-12 17:31"), sid="CA1")
    assert store.apply_result(result, source="webhook") == "applied"
    assert store.apply_result(result, source="webhook") == "duplicate"
    assert store.apply_result(result, source="poll") == "duplicate"
    lead = store.lead("c_acme_renewals", "ren_0001")
    assert lead["state"] == RETRY_WAIT
    assert lead["not_before"] == ist("2026-11-12 18:31").isoformat()
    assert store.attempts("c_acme_renewals", "ren_0001") == 1  # one attempt, not three


def test_webhook_that_beats_the_201(store, margins):
    only_leads(store, ("c_acme_renewals", "ren_0001"))
    [it] = store.reserve_dials(T0, 0.0, margins)
    # The provider delivers the result while our POST is still waiting for its 201.
    assert store.apply_result(ended(it, ended_at=ist("2026-11-12 17:35"), sid="CA9"), source="webhook") == "applied"
    assert store.mark_placed(it.request_id, "CA9") is False  # the late 201 doesn't move it backwards
    call = store.call(it.request_id)
    assert (call["phase"], call["call_sid"]) == ("ended", "CA9")
    assert state(store, "c_acme_renewals", "ren_0001") == COMPLETED
    assert store.check_invariants() == []


def test_not_placed_releases_the_slot_and_requeues_the_lead(store, margins):
    intents = store.reserve_dials(T0, 0.0, margins)
    it = intents[0]
    assert store.release_intent(it.request_id, phase="not_placed", reason="503")
    assert state(store, it.campaign_id, it.lead_id) == PENDING
    assert store.release_intent(it.request_id, phase="not_placed", reason="again") is False
    [again] = store.reserve_dials(T0, 0.0, margins)  # the freed slot is reused at once
    assert again.tenant_id == it.tenant_id
    assert again.request_id != it.request_id  # a fresh idempotency key for a fresh attempt
    assert store.attempts(it.campaign_id, it.lead_id) == 0  # a 503 is not an attempt (R4)


def test_release_restores_a_callback_and_keeps_its_time(store, margins):
    only_leads(store, ("c_acme_feedback", "fb_0026"))
    [it] = store.reserve_dials(T0, 0.0, margins)
    store.mark_placed(it.request_id, "CA1")
    cb = ist("2026-11-12 19:00")
    store.apply_result(ended(it, "completed", "callback_requested", ended_at=ist("2026-11-12 17:36"),
                             callback_at=cb, sid="CA1"), source="webhook")
    assert state(store, "c_acme_feedback", "fb_0026") == CALLBACK_WAIT
    assert store.reserve_dials(ist("2026-11-12 18:59"), 0.0, margins) == []  # not before callback_at
    [it2] = store.reserve_dials(ist("2026-11-12 19:00:05"), 0.0, margins)
    assert it2.queue == CALLBACK_WAIT
    store.release_intent(it2.request_id, phase="not_placed", reason="503")
    lead = store.lead("c_acme_feedback", "fb_0026")
    assert (lead["state"], lead["not_before"]) == (CALLBACK_WAIT, cb.isoformat())


def test_three_non_terminal_attempts_exhaust_the_lead(store, margins):
    only_leads(store, ("c_acme_feedback", "fb_0026"))
    t = T0
    for n in range(1, 4):
        [it] = store.reserve_dials(t, 0.0, margins)
        store.mark_placed(it.request_id, f"CA{n}")
        store.apply_result(ended(it, "no_answer", None, ended_at=t + timedelta(minutes=1), sid=f"CA{n}"),
                           source="webhook")
        t += timedelta(minutes=61, seconds=5)
    assert state(store, "c_acme_feedback", "fb_0026") == EXHAUSTED
    assert store.reserve_dials(t, 0.0, margins) == []
    row = [r for r in store.final_rows() if r[1] == "fb_0026"][0]
    assert row == ("c_acme_feedback", "fb_0026", EXHAUSTED, 3, "no_answer", "")


def test_dnd_completes_the_lead_and_blocks_its_phone_everywhere(store, margins):
    only_leads(store, ("c_acme_renewals", "ren_0018"), ("c_acme_feedback", "fb_0011"))
    [it] = store.reserve_dials(T0, 0.0, margins)
    assert (it.campaign_id, it.lead_id) == ("c_acme_feedback", "fb_0011")  # first in upload order
    store.mark_placed(it.request_id, "CA1")
    store.apply_result(ended(it, "completed", "dnd", ended_at=ist("2026-11-12 17:34"), sid="CA1"),
                       source="webhook")
    rows = {(r[0], r[1]): r for r in store.final_rows()}
    assert rows[("c_acme_feedback", "fb_0011")] == ("c_acme_feedback", "fb_0011", COMPLETED, 1, "completed", "dnd")
    assert rows[("c_acme_renewals", "ren_0018")] == ("c_acme_renewals", "ren_0018", DND_BLOCKED, 0, "", "")
    assert store.reserve_dials(ist("2026-11-13 12:00"), 0.0, margins) == []
    assert store.unfinished_leads() == 0


def test_dnd_crosses_tenants(store, margins):
    # The fixtures share phones only within acme, so give a zen lead the same number.
    only_leads(store, ("c_acme_feedback", "fb_0011"), ("c_zen_winback", "zen_0001"))
    with store.tx():
        store.db.execute("UPDATE leads SET phone = ? WHERE lead_id = 'zen_0001'", (SHARED_PHONE,))
    [it] = store.reserve_dials(T0, 0.0, margins)  # R3 holds across tenants too: only one is dialled
    store.mark_placed(it.request_id, "CA1")
    store.apply_result(ended(it, "completed", "dnd", ended_at=ist("2026-11-12 17:34"), sid="CA1"),
                       source="webhook")
    other = ("c_zen_winback", "zen_0001") if it.lead_id == "fb_0011" else ("c_acme_feedback", "fb_0011")
    assert state(store, *other) == DND_BLOCKED


def test_dnd_blocked_lead_keeps_its_own_call_history_in_the_csv(store, margins):
    only_leads(store, ("c_acme_renewals", "ren_0018"), ("c_acme_feedback", "fb_0011"))
    [first] = store.reserve_dials(T0, 0.0, margins)  # fb_0011: no answer, will retry
    store.mark_placed(first.request_id, "CA1")
    store.apply_result(ended(first, "no_answer", None, ended_at=ist("2026-11-12 17:31"), sid="CA1"), source="webhook")
    [second] = store.reserve_dials(ist("2026-11-12 17:32"), 0.0, margins)  # ren_0018, same phone: opts out
    assert second.lead_id == "ren_0018"
    store.mark_placed(second.request_id, "CA2")
    store.apply_result(ended(second, "completed", "dnd", ended_at=ist("2026-11-12 17:36"), sid="CA2"), source="webhook")
    rows = {(r[0], r[1]): r for r in store.final_rows()}
    # Provider: not terminal, attempts 1 < 3, phone opted out -> dnd_blocked, last call kept.
    assert rows[("c_acme_feedback", "fb_0011")] == ("c_acme_feedback", "fb_0011", DND_BLOCKED, 1, "no_answer", "")


def test_dnd_does_not_override_an_exhausted_lead(store, margins):
    only_leads(store, ("c_acme_feedback", "fb_0011"))
    with store.tx():
        store.db.execute("UPDATE leads SET state = 'exhausted' WHERE lead_id = 'ren_0018'")
    [it] = store.reserve_dials(T0, 0.0, margins)
    store.mark_placed(it.request_id, "CA1")
    store.apply_result(ended(it, "completed", "dnd", ended_at=ist("2026-11-12 17:34"), sid="CA1"),
                       source="webhook")
    assert state(store, "c_acme_renewals", "ren_0018") == EXHAUSTED


def test_output_rows_for_representative_histories(store, margins):
    """Hand-written expected rows, derived from server.py's /admin/leads rules, so a change to
    final_rows' mapping (ordering, precedence, empty values) fails here, not in a live run."""
    leads = [("c_acme_renewals", "ren_0001"), ("c_acme_renewals", "ren_0003"), ("c_zen_winback", "zen_0001")]
    only_leads(store, *leads)
    script = {  # per lead: one (status, disposition) per attempt
        "ren_0001": [("busy", None), ("completed", "interested")],
        "ren_0003": [("no_answer", None), ("completed", "callback_requested"), ("failed", None)],
        "zen_0001": [("completed", "callback_requested"), ("completed", "callback_requested"),
                     ("completed", "callback_requested")],
    }
    t = T0
    for n in range(3):
        for it in store.reserve_dials(t, 0.0, margins):
            status, disp = script[it.lead_id][n]
            store.mark_placed(it.request_id, f"CA-{it.lead_id}-{n}")
            store.apply_result(ended(it, status, disp, ended_at=t + timedelta(minutes=1),
                                     callback_at=t + timedelta(minutes=30), sid=f"CA-{it.lead_id}-{n}"),
                               source="webhook")
        t = ist("2026-11-13 12:00") + timedelta(hours=2 * n)  # next day: windows open, everything due
    got = {r[1]: r for r in store.final_rows() if r[1] in script}
    assert got == {
        "ren_0001": ("c_acme_renewals", "ren_0001", COMPLETED, 2, "completed", "interested"),
        "ren_0003": ("c_acme_renewals", "ren_0003", EXHAUSTED, 3, "failed", ""),
        "zen_0001": ("c_zen_winback", "zen_0001", EXHAUSTED, 3, "completed", "callback_requested"),
    }


def test_restart_does_not_let_old_posts_be_written_off_early(store, margins):
    [it, *_] = store.reserve_dials(T0, 100.0, margins)  # reserved long ago...
    assert store.after_restart(now_real=5000.0) == 7     # ...but the old process died just now
    assert store.call(it.request_id)["settle_after_real"] == 5000.0 + store.settle_seconds


def test_reservations_survive_a_crash(tmp_path, cfg, margins):
    path = tmp_path / "crash.db"
    s1 = Store(path, cfg)
    s1.initialize(load_leads(cfg), "run")
    assert len(s1.reserve_dials(T0, 100.0, margins)) == 7
    s1.db.close()  # the process dies right after the commit, before sending any POST

    s2 = Store(path, cfg)
    assert len(s2.open_intents()) == 7          # nothing forgotten...
    assert s2.reserve_dials(T0 + timedelta(minutes=1), 101.0, margins) == []  # ...and no double booking
    assert s2.check_invariants() == []
    s2.close()


def test_final_rows_match_the_output_contract(store, margins):
    only_leads(store, ("c_acme_renewals", "ren_0001"))
    [it] = store.reserve_dials(T0, 0.0, margins)
    store.mark_placed(it.request_id, "CA1")
    store.apply_result(ended(it, "completed", "interested", ended_at=ist("2026-11-12 17:40"), sid="CA1"),
                       source="webhook")
    rows = store.final_rows()
    assert rows == sorted(rows, key=lambda r: (r[0], r[1]))
    assert ("c_acme_renewals", "ren_0001", COMPLETED, 1, "completed", "interested") in rows
    never_called = [r for r in rows if r[1] == "ren_0002"][0]
    assert never_called[3:] == (0, "", "")


def test_invariant_check_catches_inconsistencies(store):
    with store.tx():
        store.db.execute("UPDATE leads SET state = 'active' WHERE lead_id = 'ren_0001'")
    problems = store.check_invariants()
    assert len(problems) == 1 and "ren_0001" in problems[0]
