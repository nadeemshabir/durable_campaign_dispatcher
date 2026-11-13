"""R4-R9: what happens to a lead after one of its calls ends."""
from __future__ import annotations

from datetime import timedelta

import pytest

from solution.dispatcher.rules import (CALLBACK_WAIT, COMPLETED, DND_BLOCKED, EXHAUSTED, RETRY_WAIT,
                                       decide_outcome)

from .conftest import ist

ENDED = ist("2026-11-12 17:40")


def outcome(cfg, *, status="completed", disposition=None, callback_at=None, attempts=1, dnd=False):
    return decide_outcome(status=status, disposition=disposition, callback_at=callback_at, ended_at=ENDED,
                          attempts=attempts, max_attempts=3, phone_is_dnd=dnd, retry=cfg.retry)


@pytest.mark.parametrize("disposition", ["interested", "not_interested", "wrong_number", "dnd"])
def test_terminal_dispositions_complete_the_lead(cfg, disposition):
    o = outcome(cfg, disposition=disposition)
    assert o.state == COMPLETED
    assert o.opt_out_phone is (disposition == "dnd")


def test_terminal_beats_everything_else(cfg):
    # R9 precedence: completed > exhausted > dnd_blocked
    assert outcome(cfg, disposition="interested", attempts=3, dnd=True).state == COMPLETED


@pytest.mark.parametrize("status, minutes", [("no_answer", 60), ("busy", 60), ("failed", 15)])
def test_retry_waits_are_measured_from_ended_at(cfg, status, minutes):
    o = outcome(cfg, status=status)
    assert o.state == RETRY_WAIT
    assert o.not_before == ENDED + timedelta(minutes=minutes)


def test_callback_waits_for_callback_at(cfg):
    cb = ist("2026-11-12 20:15")
    o = outcome(cfg, disposition="callback_requested", callback_at=cb)
    assert (o.state, o.not_before) == (CALLBACK_WAIT, cb)


@pytest.mark.parametrize("status, disposition", [
    ("no_answer", None), ("busy", None), ("failed", None), ("completed", "callback_requested")])
def test_third_non_terminal_attempt_exhausts(cfg, status, disposition):
    o = outcome(cfg, status=status, disposition=disposition, callback_at=ist("2026-11-12 19:00"), attempts=3)
    assert o.state == EXHAUSTED


def test_exhausted_beats_dnd_blocked(cfg):
    assert outcome(cfg, status="no_answer", attempts=3, dnd=True).state == EXHAUSTED


def test_opted_out_phone_blocks_a_non_terminal_lead(cfg):
    assert outcome(cfg, status="no_answer", attempts=1, dnd=True).state == DND_BLOCKED


def test_unexpected_disposition_retries_with_the_longest_wait(cfg):
    o = outcome(cfg, disposition="maybe_later")
    assert (o.state, o.not_before) == (RETRY_WAIT, ENDED + timedelta(minutes=60))


def test_callback_without_a_time_retries_with_the_longest_wait(cfg):
    o = outcome(cfg, disposition="callback_requested", callback_at=None)
    assert (o.state, o.not_before) == (RETRY_WAIT, ENDED + timedelta(minutes=60))
