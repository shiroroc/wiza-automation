#!/usr/bin/env python3
"""Build a self-contained HTML dashboard of successes and losses.

    python dashboard.py                 # -> out/dashboard.html
    python dashboard.py --open          # ...and open it

Reads the JSONL run logs and renders one page: how many profiles produced a
contact, how many came back empty, and how many need another look.

The generated page contains real contact data, so it is written to out/ and
git-ignored. Only this generator is committed.
"""

from __future__ import annotations

import glob
import html
import json
import os
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime

import extract as ex

LOG_DIR = "logs"
OUT = os.path.join("out", "dashboard.html")

WON = {ex.ST_EMAIL_PHONE, ex.ST_EMAIL, ex.ST_PHONE}
EMPTY = {ex.ST_NOT_FOUND, ex.ST_NO_MATCH, ex.ST_BAD_URL}
RETRY = {ex.ST_TIMEOUT, ex.ST_STALE_PANEL, ex.ST_NO_PANEL, ex.ST_ERROR,
         ex.ST_WIZA_ERROR}

LABEL = {
    ex.ST_EMAIL_PHONE: "Email + phone", ex.ST_EMAIL: "Email only",
    ex.ST_PHONE: "Phone only", ex.ST_NOT_FOUND: "Wiza had nothing",
    ex.ST_NO_MATCH: "No contact matched", ex.ST_BAD_URL: "Not a profile URL",
    ex.ST_TIMEOUT: "Timed out", ex.ST_STALE_PANEL: "Panel was stale",
    ex.ST_NO_PANEL: "Panel not open", ex.ST_ERROR: "Page error",
    ex.ST_WIZA_ERROR: "Wiza refused",
}


def load_runs():
    runs = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "run_*.jsonl"))):
        rows = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        if rows:
            runs.append((os.path.basename(path), rows))
    return runs


def bucket(status):
    return "won" if status in WON else "empty" if status in EMPTY else "retry"


def main():
    runs = load_runs()
    if not runs:
        sys.exit("No run logs yet - run wiza_auto.py first.")

    allrows = [r for _, rows in runs for r in rows]
    total = len(allrows)
    counts = Counter(r.get("status", "?") for r in allrows)
    won = sum(n for s, n in counts.items() if s in WON)
    empty = sum(n for s, n in counts.items() if s in EMPTY)
    retry = sum(n for s, n in counts.items() if s in RETRY)
    answered = won + empty

    # Per-day totals, so a ramp is visible at a glance.
    by_day = defaultdict(lambda: [0, 0, 0])
    for r in allrows:
        day = (r.get("ts") or "")[:10] or "unknown"
        i = {"won": 0, "empty": 1, "retry": 2}[bucket(r.get("status", ""))]
        by_day[day][i] += 1

    # Measured pace across each run.
    paces = []
    for _, rows in runs:
        ts = sorted(r["ts"] for r in rows if r.get("ts"))
        if len(ts) > 1:
            span = (datetime.fromisoformat(ts[-1])
                    - datetime.fromisoformat(ts[0])).total_seconds()
            if span > 0:
                paces.append((len(rows) - 1) / span * 3600)
    pace = sum(paces) / len(paces) if paces else 0

    def pct(n):
        return f"{n / total * 100:.0f}%" if total else "0%"

    rows_html = []
    for status, n in counts.most_common():
        b = bucket(status)
        rows_html.append(
            f'<tr class="{b}"><td>{html.escape(LABEL.get(status, status))}</td>'
            f'<td class="m">{html.escape(status)}</td>'
            f'<td class="n">{n}</td><td class="n">{pct(n)}</td>'
            f'<td><div class="bar {b}" style="width:{n / total * 100:.1f}%"></div></td></tr>')

    day_html = []
    for day in sorted(by_day, reverse=True):
        w, e, rt = by_day[day]
        day_html.append(
            f'<tr><td class="m">{html.escape(day)}</td><td class="n">{w + e + rt}</td>'
            f'<td class="n won-t">{w}</td><td class="n">{e}</td>'
            f'<td class="n retry-t">{rt}</td></tr>')

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Extraction run summary</title>
<style>
:root{{--bg:#eaecf2;--card:#fbfbfd;--ink:#17182b;--mut:#5c5e77;--line:#d2d5e1;
--won:#1b6b49;--empty:#5c5e77;--retry:#8a5e12;--accent:#34356b;color-scheme:light}}
@media(prefers-color-scheme:dark){{:root{{--bg:#101124;--card:#1a1b31;--ink:#e7e8f2;
--mut:#a2a4bc;--line:#2e3050;--won:#6fcb9f;--empty:#a2a4bc;--retry:#e0b75f;
--accent:#a6a8ec;color-scheme:dark}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);padding:28px 20px;
font:15px/1.6 "Segoe UI",system-ui,sans-serif}}
.wrap{{max-width:860px;margin:0 auto}}
h1{{font-size:1.7rem;margin:0 0 4px;letter-spacing:-.02em}}
.sub{{color:var(--mut);margin:0 0 24px;font-size:.92rem}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:12px;margin-bottom:24px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:14px 16px}}
.tile dt{{font:500 .68rem/1 ui-monospace,monospace;letter-spacing:.12em;
text-transform:uppercase;color:var(--mut);margin-bottom:6px}}
.tile dd{{margin:0;font-size:1.7rem;font-weight:600;font-variant-numeric:tabular-nums}}
.tile small{{display:block;color:var(--mut);font-size:.78rem;font-weight:400;margin-top:2px}}
.won-t{{color:var(--won)}}.retry-t{{color:var(--retry)}}
section{{background:var(--card);border:1px solid var(--line);border-radius:6px;
padding:16px 18px;margin-bottom:18px}}
h2{{font-size:1rem;margin:0 0 12px}}
table{{width:100%;border-collapse:collapse;font-size:.88rem}}
th{{text-align:left;font:400 .66rem/1 ui-monospace,monospace;letter-spacing:.1em;
text-transform:uppercase;color:var(--mut);padding:0 8px 8px 0}}
td{{padding:6px 8px 6px 0;border-top:1px solid var(--line)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;width:64px}}
td.m{{font-family:ui-monospace,monospace;font-size:.8rem;color:var(--mut)}}
.bar{{height:8px;border-radius:2px;min-width:2px}}
.bar.won{{background:var(--won)}}.bar.empty{{background:var(--empty);opacity:.5}}
.bar.retry{{background:var(--retry)}}
tr.won td:first-child{{color:var(--won);font-weight:500}}
footer{{color:var(--mut);font-size:.8rem;margin-top:22px}}
</style></head><body><div class="wrap">
<h1>Extraction run summary</h1>
<p class="sub">{total} profiles across {len(runs)} run(s) &middot;
generated {datetime.now():%d %b %Y, %H:%M}</p>

<dl class="tiles">
<div class="tile"><dt>Contacts found</dt><dd class="won-t">{won}</dd>
<small>{pct(won)} of all profiles</small></div>
<div class="tile"><dt>Hit rate</dt>
<dd>{f"{won / answered * 100:.0f}%" if answered else "-"}</dd>
<small>of {answered} Wiza answered</small></div>
<div class="tile"><dt>Nothing there</dt><dd>{empty}</dd>
<small>{pct(empty)} - Wiza has no data</small></div>
<div class="tile"><dt>Need retry</dt><dd class="retry-t">{retry}</dd>
<small>{pct(retry)} - left blank</small></div>
<div class="tile"><dt>Pace</dt><dd>{pace:.0f}<small>profiles / hour</small></dd></div>
</dl>

<section><h2>Outcomes</h2><table>
<thead><tr><th>Result</th><th>Status</th><th class="n">Count</th>
<th class="n">Share</th><th style="width:34%"></th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table></section>

<section><h2>By day</h2><table>
<thead><tr><th>Date</th><th class="n">Total</th><th class="n">Found</th>
<th class="n">Empty</th><th class="n">Retry</th></tr></thead>
<tbody>{''.join(day_html)}</tbody></table></section>

<footer>Rows needing a retry were left blank on purpose rather than filled with a
guess. Regenerate with <code>python dashboard.py</code>.</footer>
</div></body></html>"""

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(page)

    print(f"  {total} profiles | {won} found ({pct(won)}) | {empty} empty | "
          f"{retry} to retry | {pace:.0f}/hr")
    print(f"  -> {os.path.abspath(OUT)}")
    if "--open" in sys.argv:
        webbrowser.open("file:///" + os.path.abspath(OUT).replace("\\", "/"))


if __name__ == "__main__":
    main()
