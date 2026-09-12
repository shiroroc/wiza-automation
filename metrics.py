#!/usr/bin/env python3
"""What the runs actually achieved, from the JSONL logs.

    python metrics.py              # every run
    python metrics.py --last       # the most recent run only

Answers the two questions that decide whether this is worth running:
 - how many profiles per hour, measured rather than planned
 - what share of them produced a contact you did not have before
"""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import Counter
from datetime import datetime

import extract as ex

LOG_DIR = "logs"

# What each status means for the person reading the sheet.
WON = {ex.ST_EMAIL_PHONE, ex.ST_EMAIL, ex.ST_PHONE}
ANSWERED_EMPTY = {ex.ST_NOT_FOUND, ex.ST_NO_MATCH}
NEEDS_RETRY = {ex.ST_TIMEOUT, ex.ST_STALE_PANEL, ex.ST_NO_PANEL,
               ex.ST_ERROR, ex.ST_WIZA_ERROR}


def load(paths):
    rows = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def main():
    paths = sorted(glob.glob(os.path.join(LOG_DIR, "run_*.jsonl")))
    if not paths:
        sys.exit("No run logs yet - logs/run_*.jsonl is empty.")
    if "--last" in sys.argv:
        paths = paths[-1:]

    rows = load(paths)
    if not rows:
        sys.exit("Logs exist but contain no rows.")

    counts = Counter(r.get("status", "?") for r in rows)
    total = len(rows)
    won = sum(counts[s] for s in WON)
    empty = sum(counts[s] for s in ANSWERED_EMPTY)
    retry = sum(counts[s] for s in NEEDS_RETRY)
    answered = won + empty

    stamps = sorted(r["ts"] for r in rows if r.get("ts"))
    span_s = 0.0
    if len(stamps) > 1:
        span_s = (datetime.fromisoformat(stamps[-1])
                 - datetime.fromisoformat(stamps[0])).total_seconds()
    # n rows produce n-1 gaps; the first row's own time is not in the span.
    per_hour = (total - 1) / span_s * 3600 if span_s > 0 else 0.0

    print(f"\n{'=' * 66}\n  Measured from {len(paths)} run log(s), {total} profiles\n{'=' * 66}\n")

    print("  OUTCOMES")
    for status, n in counts.most_common():
        bucket = ("contact found" if status in WON else
                  "answered, nothing there" if status in ANSWERED_EMPTY else
                  "will retry" if status in NEEDS_RETRY else "")
        print(f"    {status:<14} {n:>4}  {n / total * 100:>5.1f}%   {bucket}")

    print("\n  WHAT THAT MEANS")
    print(f"    contacts found        : {won}/{total} ({won / total * 100:.0f}%)")
    if answered:
        print(f"    hit rate when Wiza answered: {won}/{answered} "
              f"({won / answered * 100:.0f}%)")
    print(f"    rows needing a retry  : {retry}/{total} ({retry / total * 100:.0f}%)")

    emails = sum(1 for r in rows if r.get("email"))
    phones = sum(1 for r in rows if r.get("phone"))
    print(f"    emails captured       : {emails}")
    print(f"    phones captured       : {phones}")

    print("\n  THROUGHPUT (measured, not planned)")
    if per_hour:
        print(f"    {per_hour:.0f} profiles/hour")
        print(f"    ~{per_hour * (won / total):.0f} contacts/hour at the current hit rate")
        for cap in (100, 250):
            hours = cap / per_hour
            print(f"    {cap}/day takes ~{hours:.1f}h of wall-clock")
    else:
        print("    not enough rows in one run to measure")

    print("\n  VS DOING IT BY HAND")
    print("    A person doing this manually averages 20-30 profiles/hour")
    print("    sustained, and cannot do anything else meanwhile. The script")
    print("    runs unattended, so the comparison is staff-hours saved, not")
    print("    raw speed - it is deliberately slower per profile than you are.")

    if retry:
        print(f"\n  {retry} row(s) will be retried automatically on the next run -")
        print("  they were left blank rather than filled with a guess.")


if __name__ == "__main__":
    main()
