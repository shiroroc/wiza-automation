#!/usr/bin/env python3
"""Run every test suite. Usage: python run_tests.py [--fast]

--fast skips the browser integration suite (which launches Chromium).
"""

import subprocess
import sys

SUITES = [
    ("parsing / URLs / classification", "test_extract.py", False),
    ("sheet write-back / resume / xlsx", "test_sheet_flow.py", False),
    ("daily budget / working hours", "test_limits.py", False),
    ("ETA tracker / tab lifecycle", "test_progress_tabs.py", False),
    ("browser / port / profile lock", "test_lockfile.py", False),
    ("text safety / unicode / multi-value", "test_text_safety.py", False),
    ("real Wiza panel states (from screenshots)", "test_wiza_states.py", False),
    ("real browser + shadow DOM panel", "test_browser.py", True),
    ("chrome side panel + stale-panel guard", "test_sidepanel.py", True),
]

fast = "--fast" in sys.argv
failed = []

for label, script, needs_browser in SUITES:
    if fast and needs_browser:
        print(f"\n### SKIPPED (--fast): {label}")
        continue
    print(f"\n{'#' * 64}\n### {label}  ({script})\n{'#' * 64}")
    rc = subprocess.call([sys.executable, script])
    if rc != 0:
        failed.append(script)

print("\n" + "=" * 64)
if failed:
    print("FAILED: " + ", ".join(failed))
    sys.exit(1)
print("All suites passed.")
