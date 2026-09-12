"""Integration test for the side-panel architecture.

This is the assumption the whole thing rests on: Wiza renders into a
chrome-extension:// target that is NOT a frame of the LinkedIn tab. If
Playwright cannot see that target, the runner finds nothing and every row comes
back no_panel. So: load a real unpacked extension, open its page in its own
target, and prove the probe reads it.

Also proves the stale-panel guard - the bug where the panel still shows the
previous person and their email gets written onto this person's row.

Run: python test_sidepanel.py
"""

import os
import shutil
import sys

from playwright.sync_api import sync_playwright

import extract as ex
import wiza_auto as wa

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.join(HERE, "tests", "mock_extension")

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {label}  got={got!r} want={want!r}")
    else:
        print(f"  ok   {label}")


def profile_page(name):
    """A minimal stand-in for the LinkedIn page: it only needs to carry the h1."""
    return ("data:text/html;charset=utf-8,"
            f"<main><h1>{name}</h1><p>500%2B connections</p></main>")


def extension_id(context, timeout=10000):
    """Read the extension's id off its service worker."""
    sw = context.service_workers[0] if context.service_workers else None
    if sw is None:
        try:
            sw = context.wait_for_event("serviceworker", timeout=timeout)
        except Exception:
            return None
    return sw.url.split("/")[2]


def main():
    cfg = wa.load_config(os.path.join(HERE, "config.yaml"))
    cfg["timing"].update({
        "page_load_timeout_ms": 20000, "panel_timeout_ms": 10000,
        "result_timeout_ms": 25000, "poll_interval_ms": 250,
        "settle_after_first_hit_ms": 2000, "reveal_timeout_ms": 8000,
    })
    cfg["wiza"]["find_button"]["gap_ms"] = 250
    classifier = ex.Classifier(cfg["wiza"])

    user_dir = os.path.join(os.environ.get("TEMP", "."), "wiza_ext_test_profile")
    shutil.rmtree(user_dir, ignore_errors=True)   # start from a clean profile

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_dir,
            headless=False,           # extensions need a real browser
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"],
        )
        try:
            ext_id = extension_id(context)
            print(f"\n  extension id: {ext_id}")
            check("extension loaded", bool(ext_id), True)
            if not ext_id:
                raise SystemExit("could not load the mock extension")

            panel_url = f"chrome-extension://{ext_id}/panel.html"
            page = context.pages[0] if context.pages else context.new_page()

            print("\n[can Playwright see a chrome-extension:// target at all?]")
            panel = context.new_page()
            panel.goto(panel_url + "?outcome=both&name=Asha+Patel")
            panel.wait_for_timeout(400)

            targets = wa.extension_targets(context, "chrome-extension://")
            check("extension target is enumerable", len(targets) >= 1, True)
            check("its url is the panel", any("panel.html" in t.url for t in targets), True)

            print("\n[probe reads the panel from the OTHER target]")
            page.goto(profile_page("Asha Patel, Ph.D."))
            page.wait_for_timeout(300)
            # context passed -> the side panel is in scope
            found = wa.probe(page, cfg["wiza"], context)
            texts = " ".join(r.get("text", "") for _, r in found)
            check("panel found via context scan", "Reveal contact info" in texts, True)
            check("masked preview is visible in the text", "***@example.com" in texts, True)

            v = classifier.classify([r for _, r in found])
            check("masked data is NOT harvested", (v.emails, v.phones), ([], []))

            # Without the context argument the side panel is invisible - this is
            # exactly the bug the scan exists to fix.
            found_noctx = wa.probe(page, cfg["wiza"], None)
            t2 = " ".join(r.get("text", "") for _, r in found_noctx)
            check("without context scan the panel is missed",
                  "Reveal contact info" in t2, False)

            print("\n[full flow through the real Wiza state machine]")
            panel.goto(panel_url + "?outcome=both&name=Asha+Patel")
            panel.wait_for_timeout(300)
            verdict, sig = wa.process_profile(
                page, profile_page("Asha Patel, Ph.D."), cfg, classifier,
                context, None)
            check("status", verdict.status, ex.ST_EMAIL_PHONE)
            check("email", verdict.email, "a.patel@globex.test")
            # The tel: href wins over the display text: "+14155550132" is
            # canonical E.164, which is what you want in a CRM column.
            check("phone (canonical E.164 from the tel: link)",
                  verdict.phone, "+14155550132")
            check("signature captured for the next profile", bool(sig), True)

            print("\n[email found, phone genuinely absent]")
            panel.goto(panel_url + "?outcome=email&name=Asha+Patel")
            panel.wait_for_timeout(300)
            v2, _ = wa.process_profile(
                page, profile_page("Asha Patel, Ph.D."), cfg, classifier,
                context, None)
            check("status", v2.status, ex.ST_EMAIL)
            check("phone empty", v2.phone, "")

            print("\n[no-match screen: no name shown, must still settle]")
            panel.goto(panel_url + "?outcome=nomatch")
            panel.wait_for_timeout(300)
            v3, _ = wa.process_profile(
                page, profile_page("Sam Farhan"), cfg, classifier,
                context, "a-different-previous-signature")
            check("status", v3.status, ex.ST_NO_MATCH)

            print("\n[THE WRONG-ROW BUG: panel stuck on the previous person]")
            # Panel shows Asha. The page we are 'on' is Noor Jain. The guard
            # must refuse to write Asha's email onto Noor's row.
            panel.goto(panel_url + "?outcome=both&name=Asha+Patel")
            panel.wait_for_timeout(1200)
            page.goto(profile_page("Noor Jain"))
            cfg["timing"]["result_timeout_ms"] = 6000   # don't wait forever
            v4, _ = wa.process_profile(
                page, profile_page("Noor Jain"), cfg, classifier, context, None)
            check("refuses the mismatched panel", v4.status, ex.ST_STALE_PANEL)
            check("writes no email at all", v4.email, "")
            check("row will be retried", ex.ST_STALE_PANEL in wa.FINAL_STATUSES, False)

            print("\n[THE REAL BUG: page name unreadable -> must NOT accept]")
            # Seen in live testing: LinkedIn's <title> had not updated yet,
            # profile_name() returned "", and the guard treated "unknown name"
            # as "no check needed" - writing Jordan Rivera's email onto Noor
            # Jain's row. An unknown name must block, never pass.
            NO_NAME = "data:text/html;charset=utf-8,<main><p>no name here</p></main>"
            panel.goto(panel_url + "?outcome=both&name=Asha+Patel")
            panel.wait_for_timeout(1200)
            page.goto(NO_NAME)
            cfg["timing"]["result_timeout_ms"] = 6000
            vx, _ = wa.process_profile(page, NO_NAME, cfg, classifier, context, None)
            check("unreadable page name is refused", vx.status, ex.ST_STALE_PANEL)
            check("writes NOTHING", (vx.email, vx.phone), ("", ""))
            check("and says why", "cannot tell whose panel" in vx.note, True)
            cfg["timing"]["result_timeout_ms"] = 25000

            print("\n[same panel, correct person -> accepted]")
            panel.goto(panel_url + "?outcome=both&name=Noor+Jain")
            panel.wait_for_timeout(300)
            cfg["timing"]["result_timeout_ms"] = 25000
            v5, _ = wa.process_profile(
                page, profile_page("Noor Jain"), cfg, classifier, context, None)
            check("accepted for the matching person", v5.status, ex.ST_EMAIL_PHONE)

        finally:
            context.close()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):\n")
        for f in FAILURES:
            print(" - " + f)
        sys.exit(1)
    print("Side-panel + stale-guard tests passed.")


if __name__ == "__main__":
    main()
