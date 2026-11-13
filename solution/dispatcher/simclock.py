"""Simulated time.

Every business rule runs on the provider's simulated clock, never on this machine's clock.
We compute it with the provider's own formula (from the README):

    sim_now = sim_epoch + (time.time() - real_epoch) * speed

The provider calls our webhook on localhost, so it shares our host clock. That makes our
sim_now *exactly* the provider's, even if the host clock is stepped (both of us step
together) and however long our event loop stalls. /v1/clock is read only for the three
constants; how long that request took does not matter. (An earlier version anchored to the
midpoint of one round trip; a 2 s stall then put us 19 sim-minutes behind at 1200x.)
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SimClock:
    def __init__(self) -> None:
        self.speed = 1.0
        self._sim_epoch: datetime | None = None
        self._real_epoch = 0.0

    @property
    def synced(self) -> bool:
        return self._sim_epoch is not None

    def now(self) -> datetime:
        if self._sim_epoch is None:
            raise RuntimeError("SimClock used before the first sync with the provider")
        return self._sim_epoch + timedelta(seconds=(time.time() - self._real_epoch) * self.speed)

    def observe(self, info: dict) -> None:
        """Take the three constants of the provider's formula from one /v1/clock answer."""
        self._sim_epoch = datetime.fromisoformat(info["sim_epoch"])
        self._real_epoch = float(info["real_epoch"])
        self.speed = float(info["speed"])

    async def sync(self, provider) -> dict:
        info = await provider.clock()
        self.observe(info)
        return info


class FakeClock:
    """Manually advanced clock for tests: nothing ever sleeps."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, t: datetime) -> None:
        self._now = t

    def advance(self, **kwargs: float) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now
