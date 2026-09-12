"""Tests for the daily budget and working-hours window.

The budget must survive restarts - that is its whole point - so these tests
build a real state file and re-open it. Run: python test_limits.py
"""

import os
import shutil
import sys
import tempfile
from datetime import datetime

import limits as lim

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {label}  got={got!r} want={want!r}")
    else:
        print(f"  ok   {label}")


def at(y, m, d, h):
    return lambda: datetime(y, m, d, h, 30)


tmp = tempfile.mkdtemp(prefix="wiza_limits_")
try:
    state = os.path.join(tmp, "nested", "daily_state.json")

    print("\n[daily budget]")
    b = lim.DailyBudget(10, state, clock=at(2026, 9, 12, 10))
    check("starts empty", b.used, 0)
    check("remaining", b.remaining, 10)
    check("not exhausted", b.exhausted, False)

    for _ in range(4):
        b.record()
    check("counts up", b.used, 4)
    check("creates nested dirs", os.path.exists(state), True)

    # THE point: a restart must not hand you a fresh budget.
    b2 = lim.DailyBudget(10, state, clock=at(2026, 9, 12, 15))
    check("survives a restart", b2.used, 4)
    check("remaining after restart", b2.remaining, 6)

    for _ in range(6):
        b2.record()
    check("exhausted at the cap", b2.exhausted, True)
    check("remaining floors at zero", b2.remaining, 0)

    # Next day: fresh budget.
    b3 = lim.DailyBudget(10, state, clock=at(2026, 9, 13, 9))
    check("resets the next calendar day", b3.used, 0)
    check("not exhausted the next day", b3.exhausted, False)

    print("\n[no cap configured]")
    nb = lim.DailyBudget(None, os.path.join(tmp, "nocap.json"))
    check("cap 0 means unlimited", nb.exhausted, False)
    check("remaining is None", nb.remaining, None)
    nb.record()
    check("still counts for reporting", nb.used, 1)

    print("\n[corrupt / missing state file]")
    bad = os.path.join(tmp, "bad.json")
    open(bad, "w", encoding="utf-8").write("{not json at all")
    cb = lim.DailyBudget(5, bad, clock=at(2026, 9, 12, 10))
    check("corrupt state does not crash, starts at zero", cb.used, 0)
    cb.record()
    check("and repairs itself", lim.DailyBudget(5, bad, clock=at(2026, 9, 12, 11)).used, 1)

    print("\n[working hours]")
    check("inside window", lim.within_working_hours([9, 20], at(2026, 9, 12, 13)), True)
    check("at the opening hour", lim.within_working_hours([9, 20], at(2026, 9, 12, 9)), True)
    check("at the closing hour is OUT",
          lim.within_working_hours([9, 20], at(2026, 9, 12, 20)), False)
    check("before opening", lim.within_working_hours([9, 20], at(2026, 9, 12, 4)), False)
    check("late night", lim.within_working_hours([9, 20], at(2026, 9, 12, 23)), False)
    check("null window allows anything",
          lim.within_working_hours(None, at(2026, 9, 12, 3)), True)

    # A window that wraps midnight, for anyone working odd shifts on purpose.
    check("wrapping window, late side",
          lim.within_working_hours([20, 6], at(2026, 9, 12, 23)), True)
    check("wrapping window, early side",
          lim.within_working_hours([20, 6], at(2026, 9, 12, 2)), True)
    check("wrapping window, outside",
          lim.within_working_hours([20, 6], at(2026, 9, 12, 12)), False)

    print("\n[description]")
    check("describes a window", lim.describe_window([9, 20]), "09:00-20:00 local")
    check("describes no window", lim.describe_window(None), "any time")

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):\n")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("Limit tests passed.")
