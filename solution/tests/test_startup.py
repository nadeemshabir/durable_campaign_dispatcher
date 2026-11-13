"""One command for the first start and every restart: resume against the same provider run,
start fresh against a new one, never wipe state that belongs to the running provider."""
from __future__ import annotations

from solution.dispatcher.app import Dispatcher, Settings
from solution.dispatcher.rules import COMPLETED, PENDING

RUN_A = {"sim_now": "2026-11-12T17:31:00+05:30", "speed": 600.0,
         "sim_epoch": "2026-11-12T17:30:00+05:30", "real_epoch": 1794397432.6}
RUN_B = {**RUN_A, "real_epoch": 1794399999.9}  # the provider was restarted


def open_store(tmp_path, info):
    d = Dispatcher(Settings(db_path=tmp_path / "state.db", out_path=tmp_path / "out.csv"))
    return d._open_store(info)


def test_first_start_loads_every_lead(tmp_path):
    store = open_store(tmp_path, RUN_A)
    assert store.unfinished_leads() == 200
    store.close()


def test_restart_against_the_same_provider_resumes(tmp_path):
    store = open_store(tmp_path, RUN_A)
    with store.tx():
        store.db.execute("UPDATE leads SET state = 'completed' WHERE lead_id = 'ren_0001'")
    store.db.close()  # kill -9

    store = open_store(tmp_path, RUN_A)  # the exact same command again
    assert store.lead("c_acme_renewals", "ren_0001")["state"] == COMPLETED
    store.close()


def test_start_against_a_new_provider_run_begins_fresh(tmp_path):
    store = open_store(tmp_path, RUN_A)
    with store.tx():
        store.db.execute("UPDATE leads SET state = 'completed' WHERE lead_id = 'ren_0001'")
    store.close()

    store = open_store(tmp_path, RUN_B)
    assert store.lead("c_acme_renewals", "ren_0001")["state"] == PENDING
    assert store.get_meta("provider_run").endswith("1794399999.9")
    store.close()
