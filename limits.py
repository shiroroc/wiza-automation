"""Daily volume budget and working-hours window.

The per-run `--limit` flag is not enough on its own: restarting the script would
reset it, and three restarts in an afternoon is exactly the pattern that gets an
account flagged. This keeps a rolling per-calendar-day count on disk so the
budget survives restarts, crashes and separate terminal sessions.

Nothing here is a guarantee. LinkedIn does not publish thresholds and they vary
by account age, type and history. This just makes it hard to blow through a
sensible budget by accident.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime


class DailyBudget:
    def __init__(self, cap, state_path, clock=None):
        self.cap = int(cap) if cap else 0
        self.path = state_path
        self._clock = clock or datetime.now
        self._state = self._load()

    # -- persistence --------------------------------------------------------

    def _today(self):
        return self._clock().strftime("%Y-%m-%d")

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == self._today():
                return {"date": data["date"], "count": int(data.get("count", 0))}
        except (OSError, ValueError, KeyError):
            pass
        return {"date": self._today(), "count": 0}

    def _save(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f)
        os.replace(tmp, self.path)

    # -- budget -------------------------------------------------------------

    @property
    def used(self) -> int:
        # A run can straddle midnight; roll over rather than trust the old count.
        if self._state["date"] != self._today():
            self._state = {"date": self._today(), "count": 0}
        return self._state["count"]

    @property
    def remaining(self):
        return None if not self.cap else max(0, self.cap - self.used)

    @property
    def exhausted(self) -> bool:
        return bool(self.cap) and self.used >= self.cap

    def record(self, n: int = 1) -> None:
        self._state = {"date": self._today(), "count": self.used + n}
        self._save()


def within_working_hours(window, clock=None) -> bool:
    """window: [start_hour, end_hour) in local time, or None to allow always.

    A window like [20, 6] wraps past midnight and is handled.
    """
    if not window:
        return True
    start, end = int(window[0]), int(window[1])
    hour = (clock or datetime.now)().hour
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end      # wraps midnight


def describe_window(window) -> str:
    if not window:
        return "any time"
    return f"{int(window[0]):02d}:00-{int(window[1]):02d}:00 local"


class RateWindow:
    """A rolling-window cap, for Wiza's documented fair-use limits.

    Wiza's "unlimited" plans are not literally unlimited. Their help centre
    states the caps plainly: 500 contact reveals in 24 hours, and 50 in any
    5-minute period. Exceeding them blocks further enrichment until the window
    resets. Encoding them means a future speed-up cannot breach them by
    accident - at ~93/hour we sit at roughly 8 per 5 minutes, well inside.
    """

    def __init__(self, max_events, window_s, label, clock=None):
        self.max_events = int(max_events or 0)
        self.window_s = float(window_s)
        self.label = label
        self._clock = clock or time.time
        self._events = deque()

    def _prune(self):
        cutoff = self._clock() - self.window_s
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def record(self):
        self._events.append(self._clock())

    @property
    def used(self):
        self._prune()
        return len(self._events)

    def would_exceed(self):
        return bool(self.max_events) and self.used >= self.max_events

    def wait_seconds(self):
        """How long until there is room again."""
        self._prune()
        if not self.would_exceed():
            return 0.0
        oldest = self._events[0]
        return max(0.0, (oldest + self.window_s) - self._clock())
