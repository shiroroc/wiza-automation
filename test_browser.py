"""Integration test: drive a REAL browser against a mock Wiza panel.

Run: python test_browser.py

This exercises the parts unit tests cannot reach - shadow-DOM traversal,
button discovery, the JS click bridge, the polling loop and the two-stage
email-then-phone reveal. It uses Playwright's own Chromium and a local file,
so it never touches LinkedIn, Wiza, or your plan.
"""

import os
import sys

import yaml
from playwright.sync_api import sync_playwright

import extract as ex
import wiza_auto as wa

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "tests", "mock_profile.html")

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {label}  got={got!r} want={want!r}")
    else:
        print(f"  ok   {label}")


def main():
    if not os.path.exists(MOCK):
        sys.exit(f"missing fixture: {MOCK}")

    cfg = wa.load_config(os.path.join(HERE, "config.yaml"))
    # Speed the test up; the shipped defaults stay deliberately slow.
    cfg["timing"].update({
        "page_load_timeout_ms": 20000,
        "panel_timeout_ms": 10000,
        "result_timeout_ms": 30000,
        "poll_interval_ms": 300,
        "settle_after_first_hit_ms": 3000,
        "reveal_timeout_ms": 10000,
    })
    cfg["wiza"]["find_button"]["gap_ms"] = 300
    classifier = ex.Classifier(cfg["wiza"])

    base = "file:///" + MOCK.replace("\\", "/")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        print("\n[probe: shadow DOM discovery]")
        page.goto(base + "?outcome=both")
        page.wait_for_timeout(500)
        found = wa.probe(page, cfg["wiza"])
        check("finds the shadow-root panel", len(found) >= 1, True)
        if found:
            r = found[0][1]
            check("panel text captured", "Wiza" in r["text"], True)
            labels = [b["text"] for b in r["buttons"] if b["visible"]]
            check("sees the Find button", any("Find Email" in l for l in labels), True)
            check("page noise excluded from panel",
                  "500+ connections" in r["text"], False)

        print("\n[full flow: email then phone behind a second button]")
        v, _ = wa.process_profile(page, base + "?outcome=both", cfg, classifier)
        check("status", v.status, ex.ST_EMAIL_PHONE)
        check("email", v.email, "jane@acme.com")
        check("phone", v.phone, "+14155550132")

        print("\n[full flow: email only]")
        v, _ = wa.process_profile(page, base + "?outcome=email", cfg, classifier)
        check("status", v.status, ex.ST_EMAIL)
        check("email", v.email, "jane@acme.com")
        check("no phantom phone", v.phone, "")

        print("\n[full flow: nothing found]")
        v, _ = wa.process_profile(page, base + "?outcome=none", cfg, classifier)
        check("status", v.status, ex.ST_NOT_FOUND)
        check("note records the panel wording", "no email" in v.note.lower(), True)

        print("\n[full flow: slow panel still completes]")
        v, _ = wa.process_profile(page, base + "?outcome=slow", cfg, classifier)
        check("status", v.status, ex.ST_EMAIL_PHONE)

        print("\n[no panel on the page at all]")
        page.goto("about:blank")
        blank = "data:text/html,<h1>nothing here</h1>"
        cfg["timing"]["panel_timeout_ms"] = 2000
        v, _ = wa.process_profile(page, blank, cfg, classifier)
        check("status", v.status, ex.ST_NO_PANEL)

        browser.close()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):\n")
        for f in FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("Browser integration tests passed.")


if __name__ == "__main__":
    main()
