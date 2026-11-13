"""Simulated time must equal the provider's, even when a /v1/clock round trip stalls."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from solution.dispatcher import simclock as simclock_module
from solution.dispatcher.simclock import SimClock

SIM_EPOCH = datetime.fromisoformat("2026-11-12T17:30:00+05:30")
REAL_EPOCH = 1_000_000.0
SPEED = 1200.0


def provider_answer(provider_wall: float) -> dict:
    """What the mock's GET /v1/clock returns if it reads its clock at `provider_wall`."""
    sim_now = SIM_EPOCH + timedelta(seconds=(provider_wall - REAL_EPOCH) * SPEED)
    return {"sim_now": sim_now.isoformat(), "speed": SPEED, "sim_epoch": SIM_EPOCH.isoformat(),
            "real_epoch": REAL_EPOCH}


@pytest.fixture
def wall(monkeypatch):
    """Control time.time() as seen by the clock module."""
    state = {"t": REAL_EPOCH + 100.0}
    monkeypatch.setattr(simclock_module.time, "time", lambda: state["t"])
    return state


def test_same_host_matches_the_provider_formula_exactly(wall):
    c = SimClock()
    c.observe(provider_answer(wall["t"]))
    wall["t"] += 37.5
    assert c.now() == SIM_EPOCH + timedelta(seconds=(wall["t"] - REAL_EPOCH) * SPEED)


def test_a_stalled_round_trip_does_not_move_the_clock(wall):
    # The provider answered instantly, but our loop stalled 2 s before reading the reply.
    # With midpoint anchoring this put us 1 s * 1200 = 20 sim-minutes behind.
    c = SimClock()
    answer = provider_answer(wall["t"])  # the provider reads its clock now...
    wall["t"] += 2.0                     # ...and we read its answer 2 s later
    c.observe(answer)
    assert c.now() == SIM_EPOCH + timedelta(seconds=(wall["t"] - REAL_EPOCH) * SPEED)


def test_unsynced_clock_refuses_to_answer():
    with pytest.raises(RuntimeError):
        SimClock().now()
