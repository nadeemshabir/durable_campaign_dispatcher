"""R1 window maths: campaign window ∩ regulatory window, [start, end), days, margins."""
from __future__ import annotations

import dataclasses
from datetime import datetime, time, timedelta, timezone

import pytest

from solution.dispatcher.config import Window
from solution.dispatcher.rules import Margins, can_start, next_eligible, next_start

from .conftest import ist

ZERO = Margins(start=timedelta(0), end=timedelta(0))


def test_sim_start_is_inside_every_campaign_window(cfg, margins):
    for camp in cfg.campaigns.values():
        assert can_start(ist("2026-11-12 17:30"), camp, cfg, margins), camp.campaign_id


@pytest.mark.parametrize("t, ok", [
    ("2026-11-12 09:59:59", False),
    ("2026-11-12 10:00:00", True),   # start is inclusive
    ("2026-11-12 18:59:59", True),
    ("2026-11-12 19:00:00", False),  # end is exclusive
])
def test_window_is_half_open(cfg, t, ok):
    assert can_start(ist(t), cfg.campaigns["c_acme_renewals"], cfg, ZERO) is ok


def test_margins_keep_us_away_from_both_edges(cfg, margins):
    ren = cfg.campaigns["c_acme_renewals"]
    assert not can_start(ist("2026-11-12 10:00:04"), ren, cfg, margins)
    assert can_start(ist("2026-11-12 10:00:05"), ren, cfg, margins)
    assert can_start(ist("2026-11-12 18:57:59"), ren, cfg, margins)
    assert not can_start(ist("2026-11-12 18:58:00"), ren, cfg, margins)  # 2 min before close


def test_due_callbacks_may_start_closer_to_the_end(cfg, margins):
    ren = cfg.campaigns["c_acme_renewals"]
    cb = margins.for_queue("callback_wait")
    assert cb.end == timedelta(seconds=30) and margins.for_queue("retry_wait") == margins
    assert can_start(ist("2026-11-12 18:59:29"), ren, cfg, cb)
    assert not can_start(ist("2026-11-12 18:59:30"), ren, cfg, cb)
    assert not can_start(ist("2026-11-12 18:59:00"), ren, cfg, margins)  # fresh/retry: 2 min


@pytest.mark.parametrize("t, renewals, feedback, winback", [
    ("2026-11-14 12:00", True, True, False),   # Saturday: zen is Mon-Fri
    ("2026-11-15 12:00", False, True, False),  # Sunday: only feedback runs every day
    ("2026-11-16 12:00", True, True, True),    # Monday
])
def test_allowed_days(cfg, margins, t, renewals, feedback, winback):
    c = cfg.campaigns
    assert can_start(ist(t), c["c_acme_renewals"], cfg, margins) is renewals
    assert can_start(ist(t), c["c_acme_feedback"], cfg, margins) is feedback
    assert can_start(ist(t), c["c_zen_winback"], cfg, margins) is winback


@pytest.mark.parametrize("campaign, t, expected", [
    ("c_zen_winback", "2026-11-13 08:00", "2026-11-13 09:30:05"),      # before opening, same day
    ("c_zen_winback", "2026-11-13 12:34:56", "2026-11-13 12:34:56"),   # open: now
    ("c_zen_winback", "2026-11-12 17:58:30", "2026-11-13 09:30:05"),   # inside end margin: next day
    ("c_zen_winback", "2026-11-13 18:00", "2026-11-16 09:30:05"),      # Friday close: skip the weekend
    ("c_acme_renewals", "2026-11-14 19:00", "2026-11-16 10:00:05"),    # Saturday close: skip Sunday
    ("c_acme_feedback", "2026-11-14 21:00", "2026-11-15 11:00:05"),    # feedback runs on Sunday
    ("c_acme_feedback", "2026-11-15 02:00", "2026-11-15 11:00:05"),    # after midnight
])
def test_next_start(cfg, margins, campaign, t, expected):
    assert next_start(ist(t), cfg.campaigns[campaign], cfg, margins) == ist(expected)


def test_regulatory_window_clips_a_wider_campaign_window(cfg, margins):
    wide = dataclasses.replace(cfg.campaigns["c_acme_feedback"], window=Window(time(7, 0), time(23, 0), None))
    assert not can_start(ist("2026-11-12 08:59:59"), wide, cfg, ZERO)
    assert can_start(ist("2026-11-12 09:00:00"), wide, cfg, ZERO)
    assert can_start(ist("2026-11-12 20:59:59"), wide, cfg, ZERO)
    assert not can_start(ist("2026-11-12 21:00:00"), wide, cfg, ZERO)
    assert not can_start(ist("2026-11-12 20:58:30"), wide, cfg, margins)
    assert next_start(ist("2026-11-12 22:00"), wide, cfg, margins) == ist("2026-11-13 09:00:05")


def test_campaign_that_never_opens(cfg, margins):
    never = dataclasses.replace(cfg.campaigns["c_zen_winback"],
                                window=Window(time(9, 30), time(18, 0), frozenset()))
    assert next_start(ist("2026-11-12 10:00"), never, cfg, margins) is None


def test_times_in_other_zones_are_converted(cfg, margins):
    ren = cfg.campaigns["c_acme_renewals"]
    # 04:30:05 UTC == 10:00:05 IST
    assert can_start(datetime(2026, 11, 12, 4, 30, 5, tzinfo=timezone.utc), ren, cfg, margins)
    assert not can_start(datetime(2026, 11, 12, 4, 29, 0, tzinfo=timezone.utc), ren, cfg, margins)


def test_can_start_and_next_start_agree_over_a_week(cfg, margins):
    t, end = ist("2026-11-12 00:00:00"), ist("2026-11-19 00:00:00")
    while t < end:
        for camp in cfg.campaigns.values():
            assert can_start(t, camp, cfg, margins) == (next_start(t, camp, cfg, margins) == t), (camp, t)
        t += timedelta(minutes=7, seconds=13)


def test_next_eligible_for_retries_and_callbacks(cfg, margins):
    ren = cfg.campaigns["c_acme_renewals"]
    now = ist("2026-11-12 17:30")
    assert next_eligible(now, None, ren, cfg, margins) == now                              # fresh lead
    assert next_eligible(now, ist("2026-11-12 18:30"), ren, cfg, margins) == ist("2026-11-12 18:30:05")
    assert next_eligible(now, ist("2026-11-12 17:00"), ren, cfg, margins) == now           # already due
    # R6: a callback outside the window is due at the next opening...
    cb = "callback_wait"
    assert next_eligible(now, ist("2026-11-12 19:30"), ren, cfg, margins, cb) == ist("2026-11-13 10:00:05")
    assert next_eligible(now, ist("2026-11-14 20:00"), ren, cfg, margins, cb) == ist("2026-11-16 10:00:05")
    # ...and one asked for in the last minute of a window is still called today.
    assert next_eligible(now, ist("2026-11-12 18:59"), ren, cfg, margins, cb) == ist("2026-11-12 18:59:05")
