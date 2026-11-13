"""Load campaigns.json and the lead CSVs into small immutable objects.

Nothing here talks to the network or the database, so everything downstream can be
built from a Config in tests.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # index == datetime.weekday()
E164 = re.compile(r"\+\d{8,15}")

# The fixtures shipped with the assignment, resolved relative to this file so the
# dispatcher works from any working directory.
DEFAULT_FIXTURES = (Path(__file__).resolve().parents[2]
                    / "assignment-b-workflow" / "fixtures" / "campaigns.json")


@dataclass(frozen=True)
class Window:
    """A daily interval [start, end) in local time, on the given weekdays (None = every day)."""
    start: time
    end: time
    days: frozenset[int] | None = None


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    max_concurrent_calls: int


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    tenant_id: str
    window: Window
    max_attempts: int
    leads_file: str
    rank: int  # position in campaigns.json; only used for deterministic tie-breaks


@dataclass(frozen=True)
class RetryPolicy:
    no_answer: timedelta
    busy: timedelta
    failed: timedelta

    def wait_after(self, status: str | None) -> timedelta | None:
        """How long to wait after a call that ended with `status` (None if not a retry status)."""
        return {"no_answer": self.no_answer, "busy": self.busy, "failed": self.failed}.get(status or "")

    @property
    def longest(self) -> timedelta:
        return max(self.no_answer, self.busy, self.failed)


@dataclass(frozen=True)
class Lead:
    campaign_id: str
    lead_id: str
    phone: str  # kept byte-for-byte as uploaded: the provider matches it exactly
    seq: int    # 0-based row position in the CSV (upload order)


@dataclass(frozen=True)
class Config:
    tz: ZoneInfo
    regulatory: Window
    retry: RetryPolicy
    webhook_secret: str
    tenants: dict[str, Tenant]
    campaigns: dict[str, Campaign]  # insertion order == campaigns.json order
    fixtures_dir: Path


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _window(d: dict, days_required: bool) -> Window:
    start, end = _hhmm(d["start"]), _hhmm(d["end"])
    if not start < end:
        # Every window in the fixtures is a same-day interval. Overnight windows would need
        # different maths, so refuse them loudly rather than mis-schedule silently.
        raise ValueError(f"window must satisfy start < end (no overnight windows): {d}")
    days = None
    if days_required or "days" in d:
        unknown = set(d["days"]) - set(DAY_NAMES)
        if unknown:
            raise ValueError(f"unknown day names {sorted(unknown)} in window {d}")
        days = frozenset(DAY_NAMES.index(x) for x in d["days"])
    return Window(start, end, days)


def load_config(path: str | Path = DEFAULT_FIXTURES) -> Config:
    path = Path(path)
    raw = json.loads(path.read_text())
    rp = raw["retry_policy"]
    tenants = {t["tenant_id"]: Tenant(t["tenant_id"], int(t["max_concurrent_calls"]))
               for t in raw["tenants"]}
    campaigns: dict[str, Campaign] = {}
    for rank, c in enumerate(raw["campaigns"]):
        if c["tenant_id"] not in tenants:
            raise ValueError(f"campaign {c['campaign_id']} references unknown tenant {c['tenant_id']}")
        campaigns[c["campaign_id"]] = Campaign(
            campaign_id=c["campaign_id"], tenant_id=c["tenant_id"],
            window=_window(c["window"], days_required=True),
            max_attempts=int(c["max_attempts"]), leads_file=c["leads_file"], rank=rank)
    return Config(
        tz=ZoneInfo(raw.get("timezone", "Asia/Kolkata")),
        regulatory=_window(raw["regulatory_window"], days_required=False),
        retry=RetryPolicy(no_answer=timedelta(minutes=rp["no_answer_minutes"]),
                          busy=timedelta(minutes=rp["busy_minutes"]),
                          failed=timedelta(minutes=rp["failed_minutes"])),
        webhook_secret=raw["webhook_secret"],
        tenants=tenants, campaigns=campaigns, fixtures_dir=path.parent)


def load_leads(cfg: Config) -> list[Lead]:
    """Read every campaign's CSV. customer_name is PII we never need, so it is not kept."""
    leads: list[Lead] = []
    for camp in cfg.campaigns.values():
        seen: set[str] = set()
        with open(cfg.fixtures_dir / camp.leads_file, newline="") as f:
            for seq, row in enumerate(csv.DictReader(f)):
                lead_id, phone = row.get("lead_id") or "", row.get("phone") or ""
                if not lead_id.strip() or not phone.strip():
                    log.warning("skipping %s row %d: missing lead_id or phone", camp.campaign_id, seq + 2)
                    continue
                if lead_id in seen:
                    log.warning("skipping %s row %d: duplicate lead_id %s", camp.campaign_id, seq + 2, lead_id)
                    continue
                if not E164.fullmatch(phone):
                    # Not fatal: the provider identifies the lead by the exact string it was
                    # uploaded with, so we must send it unchanged anyway.
                    log.warning("%s/%s: phone %r is not E.164", camp.campaign_id, lead_id, phone)
                seen.add(lead_id)
                leads.append(Lead(camp.campaign_id, lead_id, phone, seq))
    return leads
