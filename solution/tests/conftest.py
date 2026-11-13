"""Shared fixtures. Every test uses explicit datetimes or a FakeClock: nothing waits on the mock.

Calendar used throughout: Thu 12 Nov 2026 is the simulated start (17:30 IST),
so Fri = 13, Sat = 14, Sun = 15, Mon = 16.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from solution.dispatcher.config import load_config, load_leads
from solution.dispatcher.rules import Margins
from solution.dispatcher.store import Store

IST = ZoneInfo("Asia/Kolkata")


def ist(s: str) -> datetime:
    """'2026-11-12 17:30' -> timezone-aware datetime in IST."""
    return datetime.fromisoformat(s).replace(tzinfo=IST)


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture
def margins():
    return Margins()


@pytest.fixture
def store(tmp_path, cfg):
    s = Store(tmp_path / "test.db", cfg)
    s.initialize(load_leads(cfg), "test-run")
    yield s
    s.close()


def only_leads(store: Store, *keys: tuple[str, str]) -> None:
    """Finish every lead except `keys`, so a test can focus on a few leads."""
    with store.tx():
        store.db.execute("UPDATE leads SET state = 'completed'")
        for cid, lid in keys:
            store.db.execute("UPDATE leads SET state = 'pending' WHERE campaign_id = ? AND lead_id = ?",
                             (cid, lid))


def ended(intent_or_row, status: str = "completed", disposition: str | None = "interested", *,
          ended_at: datetime, callback_at: datetime | None = None, sid: str = "CA-test") -> dict:
    """A provider result payload (webhook body / GET result) for an intent."""
    get = (lambda k: intent_or_row[k]) if not hasattr(intent_or_row, "request_id") \
        else (lambda k: getattr(intent_or_row, k))
    return {
        "event": "call.completed", "call_sid": sid, "request_id": get("request_id"),
        "tenant_id": get("tenant_id"), "campaign_id": get("campaign_id"), "lead_id": get("lead_id"),
        "phone": get("phone"), "status": status, "disposition": disposition,
        "callback_at": callback_at.isoformat() if callback_at else None,
        "started_at": ended_at.isoformat(), "ended_at": ended_at.isoformat(), "duration_sec": 60,
    }
