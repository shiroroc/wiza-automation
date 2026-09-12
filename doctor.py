#!/usr/bin/env python3
"""Preflight: check everything is in place BEFORE you spend any lookups.

    python doctor.py

Checks, in the order they will bite you:
  1. config.yaml parses and every regex in it compiles
  2. your sheet exists, and the URL column actually resolves
  3. Chrome is reachable on the debug port
  4. a LinkedIn tab is open and signed in (not sitting on a login wall)
  5. the Wiza SIDE PANEL is open - this is the one people miss
  6. the panel is readable, and reports what state it is in
  7. today's volume budget and the working-hours window

Costs nothing: it never clicks Reveal and never spends a lookup.
"""

from __future__ import annotations

import os
import re
import sys

from playwright.sync_api import Error as PWError

import extract as ex
import limits as lim
import lockfile
import sheet as sh
import wiza_auto as wa

OK, WARN, BAD = "  OK  ", " WARN ", " FAIL "
results = []


def report(level, title, detail=""):
    results.append((level, title))
    print(f"[{level}] {title}")
    for line in (detail or "").splitlines():
        if line.strip():
            print(f"         {line}")


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    print(f"\n{'=' * 68}\n  Wiza automation preflight\n{'=' * 68}\n")

    # -- 1. config ----------------------------------------------------------
    try:
        cfg = wa.load_config(cfg_path)
    except SystemExit as e:
        report(BAD, "config.yaml", str(e))
        return finish()
    w = cfg.get("wiza") or {}
    bad_rx = []
    for key in ("not_found_patterns", "no_match_patterns", "in_progress_patterns",
                "stop_run_patterns", "email_exclude_patterns"):
        for pat in (w.get(key) or []):
            try:
                re.compile(pat, re.I)
            except re.error as err:
                bad_rx.append(f"{key}: {pat!r} ({err})")
    fb = w.get("find_button") or {}
    for key in ("text_patterns", "exclude_patterns"):
        for pat in (fb.get(key) or []):
            try:
                re.compile(pat, re.I)
            except re.error as err:
                bad_rx.append(f"find_button.{key}: {pat!r} ({err})")
    if bad_rx:
        report(BAD, "config regexes", "\n".join(bad_rx))
    else:
        report(OK, f"config.yaml parses, all patterns compile")

    # -- 2. the sheet -------------------------------------------------------
    icfg, ocfg = cfg["input"], cfg["output"]
    in_path = icfg["path"]
    if not os.path.exists(in_path):
        report(BAD, f"input file not found: {in_path}",
               "Set input.path in config.yaml, or pass --input.")
    else:
        try:
            table = sh.load(in_path, icfg.get("sheet"), icfg.get("header_row"))
            col = table.resolve_column(icfg["url_column"])
            plan = wa.build_plan(table, cfg)
            good = [p for p in plan if p[2]]
            bad = [p for p in plan if not p[2]]
            report(OK, f"sheet: {in_path}",
                   f"url column {icfg['url_column']!r} -> column "
                   f"{sh.col_index_to_letter(col)}, rows {icfg.get('start_row')}"
                   f"..{icfg.get('end_row') or table.last_row}\n"
                   f"{len(good)} usable profile(s), {len(bad)} unreadable")
            if bad:
                report(WARN, f"{len(bad)} row(s) are not personal profiles",
                       f"first: row {bad[0][0]} = {bad[0][1][:60]!r} "
                       "(these get marked bad_url and skipped)")
            if not good:
                report(BAD, "no usable rows", "Check url_column and start_row.")
        except Exception as e:
            report(BAD, f"could not read {in_path}", str(e)[:200])

    out_path = in_path if ocfg.get("in_place") else ocfg.get("path")
    report(OK if not ocfg.get("in_place") else WARN,
           f"results go to: {out_path}",
           "in_place is ON - your source file will be modified"
           if ocfg.get("in_place") else "your source file will not be touched")

    # -- 7. budget (cheap, do it before touching the browser) ---------------
    lcfg = cfg.get("limits") or {}
    budget = lim.DailyBudget(lcfg.get("daily_cap"),
                             lcfg.get("state_file") or "logs/daily_state.json")
    window = lcfg.get("working_hours")
    in_hours = lim.within_working_hours(window)
    report(OK if budget.remaining is None or budget.remaining > 0 else WARN,
           f"daily budget: {budget.used}/{budget.cap or 'unlimited'} used today",
           f"{budget.remaining if budget.remaining is not None else 'no'} remaining")
    report(OK if in_hours else WARN,
           f"working hours: {lim.describe_window(window)}",
           "" if in_hours else "you are OUTSIDE this window - a run would refuse to start")

    # -- 3. Chrome ----------------------------------------------------------
    cdp = cfg["browser"]["cdp_url"]
    try:
        pw, browser, context = wa.connect(cdp)
    except SystemExit:
        report(BAD, f"Chrome not reachable at {cdp}",
               "Start it first:\n"
               "  powershell -ExecutionPolicy Bypass -File launch-chrome.ps1")
        return finish()

    try:
        report(OK, f"attached to Chrome at {cdp}",
               f"{len(context.pages)} tab(s)/target(s) open")

        # -- 3b. is this the RIGHT browser? ---------------------------------
        lock = lockfile.load(os.path.dirname(os.path.abspath(__file__)))
        if not lock:
            report(WARN, "no .wiza-lock.json yet",
                   "Run launch-chrome.ps1 once so the browser, port and profile\n"
                   "are pinned. Without it, nothing verifies WHICH Chrome this is.")
        else:
            ok_lock, msg = lockfile.verify(context, lock,
                                           expected_port=lockfile.port_from_cdp_url(cdp))
            if ok_lock:
                report(OK, "browser identity matches the lock", msg)
            else:
                report(BAD, "WRONG BROWSER - refusing", msg)

        # -- 4. LinkedIn ----------------------------------------------------
        li = [p for p in context.pages if "linkedin.com" in (p.url or "")]
        if not li:
            report(WARN, "no LinkedIn tab is open",
                   "The script will navigate whatever tab it picks, but it is\n"
                   "safer to open a LinkedIn profile yourself first.")
        else:
            url = li[0].url
            if any(m in url.lower() for m in wa.CHECKPOINT_MARKERS):
                report(BAD, "LinkedIn is showing a login wall or checkpoint",
                       f"{url[:90]}\nSign in properly in that window, then re-run.")
            else:
                # A logged-out LinkedIn still serves public profiles, so a run
                # would look like it worked while matching almost nothing.
                states = [wa.linkedin_signed_in(p) for p in li]
                if any(s is True for s in states):
                    good = li[states.index(True)]
                    name = wa.profile_name(good)
                    report(OK, "LinkedIn is signed in",
                           f"{good.url[:80]}" +
                           (f"\nprofile on screen: {name!r}" if name else ""))
                elif any(s is False for s in states):
                    report(BAD, "LinkedIn is NOT signed in",
                           f"{li[states.index(False)].url[:80]}\n"
                           "That is the logged-out view. LinkedIn still serves public\n"
                           "profile pages signed out, so a run would appear to work while\n"
                           "Wiza matched almost nothing.\n"
                           "Sign in properly in the automation window, then re-run:\n"
                           "  powershell -ExecutionPolicy Bypass -File show-browser.ps1")
                else:
                    report(WARN, "could not tell whether LinkedIn is signed in",
                           f"{url[:80]}\nOpen a profile in the automation window to be sure.")

        # -- 5. the side panel ----------------------------------------------
        marker = w.get("frame_url_contains") or "chrome-extension://"
        ext = wa.extension_targets(context, marker)
        if not ext:
            report(BAD, "Wiza's side panel is NOT visible to the script",
                   "This is the most common setup problem.\n"
                   "Open a LinkedIn profile, open the Wiza panel, and PIN it\n"
                   "(the pin icon in its header) so it stays open. Then re-run.")
        else:
            report(OK, f"{len(ext)} extension target(s) visible",
                   "\n".join(t.url[:88] for t in ext))

            # -- 6. can we read it? -----------------------------------------
            page = li[0] if li else context.pages[0]
            found = wa.probe(page, w, context)
            if not found:
                report(BAD, "the panel target exists but nothing could be read from it",
                       "Run: python calibrate.py <a profile url>\n"
                       "then widen wiza.root_hints or set wiza.panel_selector.")
            elif not wa.panel_is_open(found):
                # Readable but 0x0 everywhere: the panel has no layout. Two very
                # different causes, and the fix differs, so say which one.
                if not li:
                    report(WARN, "the Wiza panel is asleep (no LinkedIn tab open)",
                           "The panel only renders while a LinkedIn tab is active, so\n"
                           "with none open it reports no layout. This clears itself the\n"
                           "moment the run opens its first profile - it is not a blocker.\n"
                           "To confirm by hand, open any LinkedIn profile and re-run.")
                else:
                    report(BAD, "the Wiza side panel is CLOSED", wa.PANEL_CLOSED_MSG)
            else:
                cls = ex.Classifier(w)
                roots = [r for _, r in found]
                text = cls.panel_text(roots)
                v = cls.classify(roots)
                state = {
                    ex.PENDING: "no result yet (masked, idle, or still loading)",
                    ex.ST_EMAIL_PHONE: "showing an email AND phone",
                    ex.ST_EMAIL: "showing an email",
                    ex.ST_PHONE: "showing a phone",
                    ex.ST_NOT_FOUND: "showing 'not found'",
                    ex.ST_NO_MATCH: "showing 'couldn't find this contact'",
                    ex.ST_LIMIT: "reporting the plan/credit limit!",
                }.get(v.status, v.status)
                report(OK, f"panel is readable ({len(text)} chars)",
                       f"current state: {state}")

                btns = [b.get("text", "") for _, r in found
                        for b in (r.get("buttons") or [])
                        if b.get("visible") and b.get("text")]
                choice = wa.pick_find_button(found, fb, set())
                if choice:
                    report(OK, f"reveal button recognised: {choice[1]['text']!r}")
                elif btns:
                    report(WARN, "no reveal button matched right now",
                           "Fine if the panel is already revealed. Buttons seen:\n"
                           + "\n".join(f"- {b[:60]!r}" for b in btns[:8]))
                if v.status == ex.ST_LIMIT:
                    report(BAD, "Wiza says the quota is gone", v.note)
    finally:
        # Disconnect only - closing a CDP-attached browser shuts Chrome down,
        # taking the LinkedIn and Wiza sessions with it.
        wa.detach(pw, browser)

    return finish()


def finish():
    bad = [t for lvl, t in results if lvl == BAD]
    warn = [t for lvl, t in results if lvl == WARN]
    print("\n" + "=" * 68)
    if bad:
        print(f"  {len(bad)} BLOCKER(S) - fix these before running:")
        for t in bad:
            print(f"    - {t}")
        sys.exit(1)
    if warn:
        print(f"  Ready, with {len(warn)} warning(s):")
        for t in warn:
            print(f"    - {t}")
    else:
        print("  All checks passed.")
    print("\n  Next:  python wiza_auto.py --dry-run")
    print("  Then:  python wiza_auto.py --limit 5")
    return 0


if __name__ == "__main__":
    main()
