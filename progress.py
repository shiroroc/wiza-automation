"""Run progress and time-to-finish estimate.

A 300-row run takes hours. Knowing whether that means "go get lunch" or "leave
it overnight" is the difference between the tool being usable and being babysat,
so the estimate is available from row one rather than only once enough rows have
gone by to average.

Before any rows complete, the estimate comes from the configured pacing (which
is what actually dominates the runtime). After a few rows it switches to
measured wall-clock, which naturally absorbs the long pauses and slow panels.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta


def fmt_duration(seconds) -> str:
    """'3h 10m', '14m', '45s' - never '0:03:10.243'."""
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def estimate_seconds_per_profile(cfg) -> float:
    """What one row should cost, from config alone, before any have run."""
    t = cfg.get("timing") or {}
    gap = (float(t.get("min_delay_s", 12)) + float(t.get("max_delay_s", 30))) / 2.0
    dwell_rng = t.get("dwell_range_s") or [0, 0]
    dwell = (float(dwell_rng[0]) + float(dwell_rng[1])) / 2.0
    # Page load, panel bind, reveal round-trip. Conservative but realistic.
    work = 9.0
    per = gap + dwell + work

    every = int(t.get("long_pause_every") or 0)
    if every > 0:
        rng = t.get("long_pause_range_s") or [90, 210]
        per += ((float(rng[0]) + float(rng[1])) / 2.0) / every
    return per


class Progress:
    """Tracks completed rows and projects a finish time."""

    def __init__(self, total: int, cfg=None, clock=None):
        self.total = max(0, int(total))
        self.done = 0
        self._clock = clock or time.time
        self._start = self._clock()
        self._recent = deque(maxlen=20)
        self._last_mark = self._start
        self._prior = estimate_seconds_per_profile(cfg or {})

    # -- recording ----------------------------------------------------------

    def tick(self) -> None:
        """Call once per finished row, after its inter-profile pause."""
        now = self._clock()
        self._recent.append(now - self._last_mark)
        self._last_mark = now
        self.done += 1

    # -- projections --------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return self._clock() - self._start

    @property
    def remaining_rows(self) -> int:
        return max(0, self.total - self.done)

    @property
    def per_profile(self) -> float:
        """Seconds per row: measured once there is enough signal, else planned."""
        if self.done >= 3:
            return self.elapsed / self.done
        if self._recent:
            return sum(self._recent) / len(self._recent)
        return self._prior

    @property
    def eta_seconds(self) -> float:
        return self.remaining_rows * self.per_profile

    @property
    def finish_at(self) -> datetime:
        return datetime.now() + timedelta(seconds=self.eta_seconds)

    @property
    def rate_per_hour(self) -> float:
        per = self.per_profile
        return 3600.0 / per if per > 0 else 0.0

    @property
    def percent(self) -> float:
        return (self.done / self.total * 100.0) if self.total else 100.0

    # -- display ------------------------------------------------------------

    def bar(self, width: int = 22) -> str:
        filled = int(round(self.percent / 100.0 * width))
        return "#" * filled + "." * (width - filled)

    def summary(self) -> str:
        """One line, safe to print every few rows."""
        if self.remaining_rows == 0:
            return (f"[{self.bar()}] {self.done}/{self.total} done "
                    f"in {fmt_duration(self.elapsed)}")
        return (f"[{self.bar()}] {self.done}/{self.total} "
                f"({self.percent:.0f}%)  ~{self.rate_per_hour:.0f}/hr  "
                f"{fmt_duration(self.eta_seconds)} left  "
                f"finishes ~{self.finish_at:%H:%M}")

    def opening(self) -> str:
        """Printed before the first row, from the planned pacing."""
        return (f"{self.total} profile(s) to visit at ~{self.rate_per_hour:.0f}/hr "
                f"-> about {fmt_duration(self.eta_seconds)}, "
                f"finishing around {self.finish_at:%H:%M}")
