#!/usr/bin/env python3
"""Walk a sheet of LinkedIn profiles, read the Wiza panel on each, write results back.

Attaches to a Chrome you already launched and logged into (launch-chrome.ps1),
so the extension, your LinkedIn session and your Wiza session are all the real
ones. Nothing is installed or spoofed.

  python wiza_auto.py --config config.yaml
  python wiza_auto.py --input leads.csv --start-cell B2 --dry-run
  python wiza_auto.py --limit 5            # always do a small run first

Results are written and flushed after every single row, so Ctrl-C or a crash
never costs you more than the profile in flight. Re-running resumes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime

import yaml
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import extract as ex
import limits as lim
import lockfile
import progress as prog
import sheet as sh
import tabs as tb
from browser_probe import JS_CLICK, JS_PROBE

LOG_DIR = "logs"


def _make_console_unicode_safe():
    """Stop a name with emoji or CJK from killing the run.

    Windows consoles default to cp1252. Printing a profile called "张伟" or
    "Ali 🚀 Khan" then raises UnicodeEncodeError from inside print(), which
    aborts the run mid-row. Switching stdout to UTF-8 fixes the common case;
    errors="replace" guarantees that even a console that cannot render a
    glyph degrades to "?" instead of throwing.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_make_console_unicode_safe()

# LinkedIn bounces automation into one of these when it wants a human.
CHECKPOINT_MARKERS = ("/checkpoint/", "/authwall", "/uas/login", "/login",
                      "/signup", "captcha")

# Statuses we consider settled; --resume skips these rows on a re-run.
FINAL_STATUSES = {ex.ST_EMAIL, ex.ST_PHONE, ex.ST_EMAIL_PHONE,
                  ex.ST_NOT_FOUND, ex.ST_NO_MATCH, ex.ST_BAD_URL, ex.ST_SKIPPED}


# ---------------------------------------------------------------- console


class Console:
    def __init__(self):
        self.t0 = time.time()

    def _stamp(self):
        el = int(time.time() - self.t0)
        return f"[{el // 60:02d}:{el % 60:02d}]"

    def info(self, msg):
        print(f"{self._stamp()} {msg}", flush=True)

    def row(self, n, total, row_no, url, status, detail=""):
        short = url.replace("https://www.linkedin.com/in/", "").rstrip("/")
        tail = f"  {detail}" if detail else ""
        # "[3 of 5]" is how many of THIS RUN's rows are done; "row 7" is the
        # spreadsheet row. They differ whenever --resume skips settled rows,
        # and conflating them makes a 5-row run look like it ran 7.
        print(f"{self._stamp()} [{n} of {total}] sheet row {row_no:<5} "
              f"{short[:34]:<34} -> {status}{tail}", flush=True)

    def warn(self, msg):
        print(f"{self._stamp()} !  {msg}", flush=True)


C = Console()


# ---------------------------------------------------------------- config


def load_config(path):
    # config.yaml is git-ignored because it points at your real data file.
    # A fresh clone has only config.example.yaml, so fall back to it rather
    # than failing - that way the tests and a dry-run work straight away.
    if not os.path.exists(path):
        example = os.path.join(os.path.dirname(os.path.abspath(path)) or ".",
                               "config.example.yaml")
        if os.path.exists(example):
            C.info(f"No {os.path.basename(path)} yet - using config.example.yaml. "
                   f"Copy it to {os.path.basename(path)} and edit it.")
            path = example
        else:
            sys.exit(f"Config not found: {path}")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for section in ("browser", "input", "output", "timing", "wiza", "run"):
        cfg.setdefault(section, {})
    return cfg


def apply_cli_overrides(cfg, args):
    if args.input:
        cfg["input"]["path"] = args.input
    if args.url_column:
        cfg["input"]["url_column"] = args.url_column
    if args.start_cell:
        letter, row = sh.parse_cell_ref(args.start_cell)
        cfg["input"]["url_column"] = letter
        cfg["input"]["start_row"] = row
    if args.start_row:
        cfg["input"]["start_row"] = args.start_row
    if args.end_row:
        cfg["input"]["end_row"] = args.end_row
    if args.output:
        cfg["output"]["path"] = args.output
    if args.in_place:
        cfg["output"]["in_place"] = True
    if args.limit:
        cfg["run"]["max_profiles"] = args.limit
    if args.no_resume:
        cfg["run"]["resume"] = False
    if args.no_click:
        cfg["wiza"]["find_button"]["enabled"] = False
    if args.cdp_url:
        cfg["browser"]["cdp_url"] = args.cdp_url
    return cfg


# ---------------------------------------------------------------- browser


def connect(cdp_url):
    """Attach to the already-running Chrome. Never launches a browser itself."""
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url)
    except Exception as e:
        pw.stop()
        sys.exit(
            f"\nCould not attach to Chrome at {cdp_url}\n  ({e})\n\n"
            "Start it first, in a separate terminal:\n"
            "    powershell -ExecutionPolicy Bypass -File launch-chrome.ps1\n"
            "then sign in to LinkedIn and Wiza in the window that opens.\n"
        )
    if not browser.contexts:
        pw.stop()
        sys.exit("Chrome is attached but has no browser context open. Open a tab and retry.")
    return pw, browser, browser.contexts[0]


def connection_lost(verdict) -> bool:
    """Is this failure the browser going away, rather than a bad profile?

    When the machine sleeps or the screen locks, Chrome can drop the debug
    connection. Every subsequent row then fails instantly with the same
    message. Recognising that is what stops the run chewing through the rest
    of the sheet recording meaningless errors.
    """
    note = (getattr(verdict, "note", "") or "").lower()
    return any(sig in note for sig in (
        "has been closed", "target page, context or browser",
        "connection closed", "websocket", "browser closed",
        "target closed", "disconnected",
    ))


def wait_for_browser(cfg, pw, browser, max_wait_s=900):
    """Pause until the automation browser is usable again.

    Returns (pw, browser, context, page) once reattached, or None if the
    browser never came back inside max_wait_s. Chrome is never launched here -
    if it is genuinely gone, only the user can bring it back, so say so and
    keep checking rather than failing the whole run.
    """
    cdp = cfg["browser"]["cdp_url"]
    detach(pw, browser)

    deadline = time.time() + float(max_wait_s)
    attempt = 0
    C.warn("Lost the browser connection - PAUSING. The run continues by itself "
           "as soon as Chrome is back.")
    C.warn("  If Chrome closed (screen lock, sleep, or a crash), start it again:")
    C.warn("    powershell -ExecutionPolicy Bypass -File launch-chrome.ps1")

    while time.time() < deadline:
        attempt += 1
        try:
            new_pw = sync_playwright().start()
            new_browser = new_pw.chromium.connect_over_cdp(cdp)
            if new_browser.contexts:
                ctx = new_browser.contexts[0]
                page = pick_page(ctx, True)
                left = int(deadline - time.time())
                C.info(f"Reconnected to Chrome after {attempt} attempt(s). "
                       f"Resuming.")
                return new_pw, new_browser, ctx, page
            new_pw.stop()
        except Exception:
            try:
                new_pw.stop()
            except Exception:
                pass

        remaining = int(deadline - time.time())
        if attempt % 6 == 1:
            C.info(f"  still waiting for Chrome... ({remaining // 60}m "
                   f"{remaining % 60}s before giving up)")
        time.sleep(10)

    C.warn("Chrome did not come back. Stopping so the remaining rows stay "
           "untouched for the next run.")
    return None


def detach(pw, browser=None):
    """Let go of the browser WITHOUT closing it.

    browser.close() on a CDP-attached browser does not disconnect - it shuts
    Chrome down. Since we attached to a browser the user launched and signed
    into, closing it would throw away the whole session at the end of every
    run. Stopping the Playwright driver drops the websocket and leaves Chrome
    exactly as we found it.
    """
    try:
        pw.stop()
    except Exception:
        pass


def enforce_lock(context, cfg, pw=None, browser=None):
    """Refuse to drive a browser that is not the one launch-chrome.ps1 started.

    Without this, anything holding the debug port gets driven - including your
    everyday Chrome, signed into your personal LinkedIn.
    """
    if not cfg["browser"].get("enforce_profile_lock", True):
        return
    lock = lockfile.load(os.path.dirname(os.path.abspath(__file__)))
    if not lock:
        # Say this loudly. A missing or unreadable lock means nothing is
        # verifying WHICH browser this is - which is the whole point.
        C.warn("No readable .wiza-lock.json - the browser identity is UNVERIFIED. "
               "Run launch-chrome.ps1 to pin it.")
        return
    port = lockfile.port_from_cdp_url(cfg["browser"].get("cdp_url"))
    ok, msg = lockfile.verify(context, lock, expected_port=port)
    if not ok:
        detach(pw, browser)
        sys.exit(f"\nPROFILE LOCK FAILED\n\n{msg}\n")
    C.info(f"Browser lock {msg}")


def pick_page(context, reuse_tab):
    """Prefer an existing LinkedIn tab so we inherit the window the user set up."""
    if reuse_tab:
        for p in context.pages:
            try:
                if "linkedin.com" in (p.url or ""):
                    return p
            except PWError:
                continue
        for p in context.pages:
            try:
                if not (p.url or "").startswith("devtools://"):
                    return p
            except PWError:
                continue
    return context.new_page()


def extension_targets(context, marker):
    """Pages that are part of an extension rather than the web page.

    Wiza renders into Chrome's side panel. That is a SEPARATE browser target,
    not an iframe of the LinkedIn tab, so page.frames alone never sees it.
    """
    out = []
    if not marker:
        return out
    for p in context.pages:
        try:
            if marker in (p.url or ""):
                out.append(p)
        except PWError:
            continue
    return out


def probe(page, wcfg, context=None):
    """Read every place the Wiza panel could be. Returns [(frame, root_dict), ...].

    Looks in three places, because extensions use all three:
      1. frames of the LinkedIn page   (injected iframe)
      2. the LinkedIn DOM itself       (injected nodes / shadow root)
      3. other extension targets       (the side panel - the one Wiza uses)
    """
    hints = [h.lower() for h in (wcfg.get("root_hints") or [])]
    frame_marker = wcfg.get("frame_url_contains") or "chrome-extension://"
    panel_selector = wcfg.get("panel_selector")
    extra = [m.lower() for m in (wcfg.get("panel_url_contains") or [])]

    # (frame, treat_whole_document_as_panel)
    frames = [(f, False) for f in page.frames]

    if context is not None and wcfg.get("scan_extension_pages", True):
        for ep in extension_targets(context, frame_marker):
            if ep is page:
                continue
            try:
                # EVERY frame inside an extension target is panel content, not
                # just the ones with a chrome-extension:// URL. Wiza's panel is
                # a thin sidepanel.html shell wrapping an iframe on
                # plugin.wiza.co - checking the frame's own URL finds only the
                # shell and misses the entire UI.
                frames.extend((f, True) for f in ep.frames)
            except PWError:
                continue

    found = []
    for frame, in_panel in frames:
        try:
            url = frame.url or ""
        except PWError:
            continue
        low = url.lower()
        is_ext = in_panel or (frame_marker and frame_marker in url) \
            or any(m in low for m in extra)
        # In an extension context the whole document is the panel; in the
        # LinkedIn page we must narrow to the injected subtree or we would
        # scrape the entire profile.
        opts = {
            "panelSelector": panel_selector,
            "rootHints": hints,
            "wholeDocument": bool(is_ext),
        }
        try:
            res = frame.evaluate(JS_PROBE, opts)
        except PWError:
            continue   # frame detached or navigated mid-probe; normal, skip it
        for r in (res or {}).get("roots", []) or []:
            found.append((frame, r))
    return found


def linkedin_signed_in(page):
    """True / False / None(unknown) - is this tab an authenticated LinkedIn?

    A logged-out LinkedIn still serves public profile pages, so the run would
    appear to work while Wiza matched almost nothing. Checking the URL is not
    enough: the give-away is in the DOM.
    """
    try:
        url = (page.url or "").lower()
    except PWError:
        return None
    if "linkedin.com" not in url:
        return None

    # Signed-out markers first - they are unambiguous.
    for sel in ("a[href*='/uas/login']", "a[href*='linkedin.com/login']",
                ".nav__button-secondary", "a[data-tracking-control-name*='guest']",
                "form.login__form", "button[data-id='sign-in-form__submit-btn']"):
        try:
            if page.query_selector(sel):
                return False
        except PWError:
            continue
    try:
        body = (page.inner_text("body", timeout=4000) or "")[:4000].lower()
    except PWError:
        body = ""
    for phrase in ("welcome to your professional network", "join now",
                   "sign in to see", "new to linkedin?"):
        if phrase in body:
            return False

    # Authenticated markers: the global nav only renders when signed in.
    for sel in (".global-nav__me", "img.global-nav__me-photo",
                "[data-test-global-nav]", "a[href*='/feed/']",
                ".global-nav__primary-link"):
        try:
            if page.query_selector(sel):
                return True
        except PWError:
            continue
    return None


_TITLE_COUNT = re.compile(r"^\(\d+\+?\)\s*")
# FRAGILE: a repo-wide punctuation pass once turned the en dash here into "-",
# producing "[|--]" - an invalid character range that crashes at import.
# test_text_safety.py compiles every module regex, so that fails the suite now.
_TITLE_SUFFIX = re.compile(r"\s*[|–—\-]\s*LinkedIn\s*$", re.I)
_NOT_A_NAME = {"", "feed", "linkedin", "home", "my network", "jobs", "messaging",
               "notifications", "search", "log in or sign up"}


PANEL_CLOSED_MSG = (
    "The Wiza side panel exists but is CLOSED - nothing in it is on screen.\n"
    "  A closed side panel still answers as a browser target, so its old text\n"
    "  can still be read, but no button in it can be clicked.\n"
    "  Open the Wiza panel in the automation window and PIN it (pin icon in\n"
    "  its header), then try again:\n"
    "    powershell -ExecutionPolicy Bypass -File show-browser.ps1"
)


def panel_is_open(found):
    """Is the panel actually rendered, or just a closed target we can still read?"""
    if not found:
        return False
    return any(r.get("laidOut") for _, r in found)


def profile_name(page):
    """The name LinkedIn shows for the profile we are on.

    The document title is the primary source, not an h1: current LinkedIn
    profile pages render NO h1 at all, so the old selector silently returned
    "" - which quietly switched off the stale-panel guard that stops one
    person's contact details landing on another person's row.
    """
    try:
        title = page.title() or ""
    except PWError:
        title = ""
    name = _TITLE_SUFFIX.sub("", _TITLE_COUNT.sub("", title)).strip()
    if name and name.lower() not in _NOT_A_NAME:
        return name

    for sel in (".text-heading-xlarge", "main h1", "h1"):
        try:
            el = page.query_selector(sel)
            if el:
                txt = (el.inner_text() or "").strip()
                if txt:
                    return txt.splitlines()[0].strip()
        except PWError:
            continue
    return ""


# "* 3rd+", "1st", "2nd" - the connection-degree badge shares a class with the
# headline on current LinkedIn, so it has to be rejected explicitly.
# Escapes again: written literally this became "[\s*-\-]", which still compiles
# but silently means the RANGE * to -, matching "+" and "," as well.
_DEGREE_ONLY = re.compile(
    r"^[\s·•\-]*\d(?:st|nd|rd|th)\+?[\s·•\-]*$", re.I)

# Chrome/LinkedIn chrome that sits above the name in the page's text.
_NAV_NOISE = ("skip to", "notification", "home", "my network", "jobs",
              "messaging", "me", "for business", "sales nav", "get 50%",
              "try premium", "search", "linkedin")


def profile_headline(page):
    """The line under the name. Read positionally, so class renames cannot break it."""
    for sel in (".text-body-medium.break-words", ".text-body-medium"):
        try:
            el = page.query_selector(sel)
            if el:
                txt = " ".join((el.inner_text() or "").split())
                # "* 3rd+" is the connection-degree badge, not a headline.
                if txt and len(txt) < 400 and not _DEGREE_ONLY.match(txt):
                    return txt
        except PWError:
            continue

    # Fallback: the headline is the line immediately after the name.
    name = profile_name(page)
    if not name:
        return ""
    try:
        body = page.inner_text("body", timeout=5000) or ""
    except PWError:
        return ""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    for i, ln in enumerate(lines[:80]):
        if ln == name and i + 1 < len(lines):
            nxt = lines[i + 1]
            if not any(n in nxt.lower() for n in _NAV_NOISE) and len(nxt) < 400:
                return nxt
    return ""


def pick_recover_button(found, wcfg):
    """The panel's own Reload/Retry button. Pressing it costs no lookup.

    Unlike the reveal button, this one is NOT filtered on visibility. The
    condition being recovered from is a panel with no layout, where every
    element measures 0x0 - so requiring a visible rect would rule out the
    button precisely when it is needed. The click is dispatched directly on
    the element, which does not need layout to work.
    """
    pats = [re.compile(p, re.I) for p in (wcfg.get("recover_button_patterns") or [])]
    if not pats:
        return None
    for frame, r in found:
        for b in r.get("buttons", []) or []:
            if b.get("disabled"):
                continue
            label = (b.get("text") or "").strip()
            if label and any(rx.search(label) for rx in pats):
                return frame, b
    return None


def pick_find_button(found, fb_cfg, already_clicked):
    """Choose the next reveal button to press, or None.

    Strong patterns beat the deny-list. The real button's element text is
    "Reveal contact info Unlimited reveals on your plan" - the sibling line is
    swept up by innerText - and the deny entry for "reveals on your plan" was
    therefore killing the one button we exist to press. Deny-listing is for
    resolving ambiguity, not for overriding an unmistakable match.
    """
    strong = [re.compile(p, re.I) for p in (fb_cfg.get("strong_patterns") or [])]
    patterns = [re.compile(p, re.I) for p in (fb_cfg.get("text_patterns") or [])]
    denied = [re.compile(p, re.I) for p in (fb_cfg.get("exclude_patterns") or [])]

    candidates = []
    for frame, r in found:
        for b in r.get("buttons", []) or []:
            if not b.get("visible") or b.get("disabled"):
                continue
            label = (b.get("text") or "").strip()
            if not label or label.lower() in already_clicked:
                continue
            if any(rx.search(label) for rx in strong):
                return frame, b          # unmistakable - press it
            candidates.append((frame, b, label))

    for frame, b, label in candidates:
        # "Forget lead" deletes the lead; several other labels match by accident.
        if any(rx.search(label) for rx in denied):
            continue
        if any(rx.search(label) for rx in patterns):
            return frame, b
    return None


def click_configured_selectors(page, fb_cfg):
    """Exact CSS selectors from config take priority over text matching."""
    clicked = []
    for sel in fb_cfg.get("selectors") or []:
        for frame in page.frames:
            try:
                el = frame.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=5000)
                    clicked.append(sel)
                    break
            except PWError:
                continue
    return clicked


def at_checkpoint(page):
    try:
        url = (page.url or "").lower()
    except PWError:
        return False
    return any(m in url for m in CHECKPOINT_MARKERS)


# ---------------------------------------------------------------- one profile


def jitter(cfg):
    """A short random pause before an individual action."""
    rng = (cfg["timing"].get("action_jitter_ms") or [0, 0])
    lo, hi = float(rng[0]) / 1000.0, float(rng[1]) / 1000.0
    if hi > 0:
        time.sleep(random.uniform(lo, hi))


def dwell(page, cfg):
    """Linger on the profile the way a person reading it would.

    Two reasons this is not padding: a real visit has a dwell time and some
    scrolling, and the side panel gets a moment to bind to the new page before
    we start reading it.
    """
    t = cfg["timing"]
    rng = t.get("dwell_range_s") or [0, 0]
    if t.get("scroll_profile", True):
        try:
            for _ in range(random.randint(1, 3)):
                page.mouse.wheel(0, random.randint(280, 900))
                time.sleep(random.uniform(0.3, 1.1))
        except PWError:
            pass
    lo, hi = float(rng[0]), float(rng[1])
    if hi > 0:
        time.sleep(random.uniform(lo, hi))




def process_profile(page, url, cfg, classifier, context=None, prev_sig=None,
                    sheet_name="", prev_contact=None):
    """Visit one profile and tag the result with who it was actually about."""
    verdict, sig = _visit(page, url, cfg, classifier, context, prev_sig, sheet_name,
                          prev_contact)
    verdict.profile_url = url
    # Only trust the page for identity if we actually got there.
    if verdict.status not in (ex.ST_BAD_URL,) and "load" not in (verdict.note or ""):
        verdict.name = profile_name(page)
        verdict.headline = profile_headline(page)
    return verdict, sig


def panel_signature(roots):
    """Cheap fingerprint of what the panel is currently showing."""
    text = "\n".join(r.get("text", "") for _, r in roots)
    return hashlib.md5(text.encode("utf-8", "replace")).hexdigest() if text else ""


def _visit(page, url, cfg, classifier, context=None, prev_sig=None, sheet_name="",
           prev_contact=None):
    """Navigate, coax the panel, and return (Verdict, panel_signature).

    The signature is the panel text as we left it; the next profile uses it to
    tell "the panel repainted for the new person" from "the panel has not
    caught up and is still showing the last one".
    """
    t = cfg["timing"]
    wcfg = cfg["wiza"]
    fb = wcfg.get("find_button") or {}
    verify_name = bool(cfg["run"].get("verify_profile_name", True))

    # The side panel only tracks the ACTIVE tab. Without this the panel can
    # quietly stop updating and every row afterwards reads as stale.
    if cfg["browser"].get("focus_tab", True):
        try:
            page.bring_to_front()
        except PWError:
            pass
    jitter(cfg)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=t["page_load_timeout_ms"])
    except PWTimeout:
        return ex.Verdict(ex.ST_ERROR, note="page load timeout"), prev_sig
    except PWError as e:
        return ex.Verdict(ex.ST_ERROR, note=f"navigation failed: {str(e)[:120]}"), prev_sig

    if at_checkpoint(page):
        return ex.Verdict(ex.ST_ERROR, note="checkpoint"), prev_sig

    poll = t["poll_interval_ms"] / 1000.0

    # Wait for LinkedIn to actually render the person's name BEFORE touching
    # the panel. Wiza scrapes the page itself, and if it binds to a page that
    # has not painted yet it captures a blank name - which is exactly what
    # produces "Name can't be blank" when Reveal is pressed. Waiting here
    # removes the cause instead of retrying the symptom.
    name_deadline = time.time() + float(t.get("name_wait_ms", 15000)) / 1000.0
    expected = ""
    while time.time() < name_deadline:
        expected = profile_name(page)
        if expected:
            break
        time.sleep(poll)

    dwell(page, cfg)
    # Re-read inside the poll loop as well: LinkedIn's <title> often lags
    # domcontentloaded by several seconds, and an empty name must never be
    # treated as "no check needed".
    if verify_name and not expected:
        expected = profile_name(page)

    # -- phase 1: wait for the panel to exist at all ------------------------
    panel_deadline = time.time() + t["panel_timeout_ms"] / 1000.0
    found = []
    while time.time() < panel_deadline:
        found = probe(page, wcfg, context)
        if found:
            break
        time.sleep(poll)
    if not found:
        return ex.Verdict(
            ex.ST_NO_PANEL,
            note="Wiza panel never appeared - is the side panel open and signed in?"
        ), prev_sig

    # Present but closed: readable, entirely unclickable. Fail loudly rather
    # than spending the whole run timing out on a panel nobody can see.
    if not panel_is_open(found):
        return ex.Verdict(ex.ST_NO_PANEL, note="side panel is closed"), prev_sig

    # -- phase 2: click reveal, then wait for a result ----------------------
    if fb.get("enabled", True):
        click_configured_selectors(page, fb)

    clicked_labels = set()
    max_clicks = int(fb.get("max_clicks", 2) or 0)
    gap = float(fb.get("gap_ms", 2500)) / 1000.0
    settle = float(t.get("settle_after_first_hit_ms", 6000)) / 1000.0
    reveal_wait = float(t.get("reveal_timeout_ms", 20000)) / 1000.0

    result_deadline = time.time() + t["result_timeout_ms"] / 1000.0
    best = None
    settle_from = None      # set once there is nothing left to click
    awaiting_until = None   # set while a reveal we triggered is still in flight
    saw_panel_change = False
    repeated_contact = False   # panel handed back the previous row's details
    negative_since = None      # when we first saw a definite "nothing here"
    confirm_negative = float(t.get("confirm_not_found_ms", 2500)) / 1000.0
    recovered = 0
    max_recover = int(wcfg.get("max_recover_clicks", 3) or 0)
    ever_matched_name = not verify_name

    def more_clicks_available(found_now):
        if not fb.get("enabled", True) or len(clicked_labels) >= max_clicks:
            return None
        return pick_find_button(found_now, fb, clicked_labels)

    def press(choice):
        frame, btn = choice
        label = (btn.get("text") or "").strip()
        jitter(cfg)
        try:
            if frame.evaluate(JS_CLICK, btn["token"]) == "ok":
                clicked_labels.add(label.lower())
                time.sleep(gap)
                return True
        except PWError:
            pass   # panel re-rendered under us; the next probe picks it up
        return False

    while time.time() < result_deadline:
        # LinkedIn's <title> can lag the navigation by seconds, so keep asking
        # until we know who this page is about.
        if verify_name and not (expected or sheet_name):
            expected = profile_name(page)

        found = probe(page, wcfg, context)
        # Wiza leaves a dead sidepanel.html shell behind ("Wiza couldn't load")
        # alongside the live iframe. The shell has no layout; the live panel
        # does. Reading both merges a stale error over a perfectly good answer,
        # so once anything is actually rendered, ignore what is not.
        rendered = [(f, r) for f, r in found if r.get("laidOut")]
        if rendered:
            found = rendered
        roots = [r for _, r in found]
        sig = panel_signature(found)
        if prev_sig and sig and sig != prev_sig:
            saw_panel_change = True

        v = classifier.classify(roots)

        if v.status == ex.ST_LIMIT:
            return v, sig

        if at_checkpoint(page):
            return ex.Verdict(ex.ST_ERROR, note="checkpoint mid-profile"), sig

        # --- is this panel actually about the person we navigated to? ------
        # The side panel repaints a beat after navigation, so a verdict read
        # too early belongs to the PREVIOUS profile. Refusing it here is the
        # difference between a blank cell and silently wrong data.
        fresh = True
        if verify_name:
            # Two independent answers to "who is this row about": the page
            # itself, and the name the sheet carries. Either one matching is
            # enough; having neither means we cannot judge and must not write.
            candidates = [n for n in (expected, sheet_name) if n]
            panel_txt = classifier.panel_text(roots)
            if not candidates:
                # We do not know whose page this is, so nothing the panel says
                # can be trusted. Treating an unreadable name as "check
                # skipped" is how another person's email lands on this row.
                fresh = False
            elif any(ex.name_matches(n, panel_txt) for n in candidates):
                ever_matched_name = True
            elif v.no_match:
                # The "couldn't find this contact" screen shows no name by
                # design, so prove freshness by the panel having repainted.
                fresh = bool(saw_panel_change or not prev_sig)
            else:
                fresh = False

        # Second, independent staleness check. Wiza repaints its header with the
        # new person BEFORE its contact area catches up, so the name can match
        # while the email still belongs to the previous profile. Contact details
        # identical to the last row are that bug, not a coincidence - refuse
        # them and keep polling.
        if fresh and prev_contact and (v.emails or v.phones):
            # OVERLAP, not exact equality. Wiza repaints the panel field by
            # field: the name and phone can already belong to this person while
            # the email still belongs to the last one. An exact (emails, phones)
            # comparison misses that, because the changed phone makes the pair
            # differ - which is how one person's email reached another's row.
            # Two different people sharing an address here is not a real case.
            prev_emails, prev_phones = prev_contact
            if (set(e.lower() for e in v.emails) & set(e.lower() for e in prev_emails)
                    or set(v.phones) & set(prev_phones)):
                fresh = False
                repeated_contact = True

        if v.done and fresh:
            # Keep the richest verdict we have seen for this profile.
            if best is None or len(v.emails) + len(v.phones) > len(best.emails) + len(best.phones):
                best = v

            # Both halves in hand: nothing more to wait for.
            if v.status == ex.ST_EMAIL_PHONE:
                return v, sig

            # A definite "nothing here" is the END of this profile. Confirm it
            # is stable rather than a flicker, then write it and move on - no
            # further reveal clicks, no retry. Pressing Reveal again after
            # Wiza has already said "no contact" is pure waste.
            if v.status in (ex.ST_NOT_FOUND, ex.ST_NO_MATCH):
                if negative_since is None:
                    negative_since = time.time()
                    time.sleep(poll)
                    continue
                if time.time() - negative_since >= confirm_negative:
                    return v, sig
                time.sleep(poll)
                continue
            negative_since = None

            # Wiza commonly reveals the email first and hides the phone behind a
            # second button. Returning now would silently drop the phone, so
            # spend the remaining clicks before settling.
            choice = more_clicks_available(found)
            if choice and press(choice):
                awaiting_until = time.time() + reveal_wait
                settle_from = None
                continue

            # A reveal we already triggered may still be loading - give it the
            # full reveal window before settling for the partial result.
            if awaiting_until and time.time() < awaiting_until:
                time.sleep(poll)
                continue

            if settle_from is None:
                settle_from = time.time()
            if time.time() - settle_from >= settle:
                return best, sig
            time.sleep(poll)
            continue

        # The panel is showing its own failure screen. Press Reload and carry
        # on - this costs no lookup, and without it one blip fails every
        # remaining row in the run rather than just this one.
        if getattr(v, "panel_error", False) and recovered < max_recover:
            rec = pick_recover_button(found, wcfg)
            if rec:
                C.warn(f"Wiza panel error - pressing {rec[1]['text']!r}")
                if press(rec):
                    recovered += 1
                    clicked_labels.discard((rec[1].get("text") or "").strip().lower())
                    time.sleep(2.0)
                    continue

        # Nothing usable yet. Only press reveal once the panel is showing the
        # right person - clicking while it still displays the previous lead
        # would spend a lookup on the wrong profile.
        if fresh:
            choice = more_clicks_available(found)
            if choice and press(choice):
                awaiting_until = time.time() + reveal_wait
                continue

        time.sleep(poll)

    final_sig = panel_signature(found)
    if best is not None:
        return best, final_sig

    panel_text = "\n---\n".join(r.get("text", "") for _, r in found)
    if verify_name and not expected:
        v = ex.Verdict(
            ex.ST_STALE_PANEL,
            note="could not read the profile name from the page - "
                 "nothing written, because we cannot tell whose panel this is",
        )
    elif repeated_contact:
        v = ex.Verdict(
            ex.ST_STALE_PANEL,
            note="panel kept showing the previous profile's contact details - "
                 "nothing written",
        )
        v.panel_text = panel_text
        return v, final_sig
    elif verify_name and not ever_matched_name:
        v = ex.Verdict(
            ex.ST_STALE_PANEL,
            note=f"panel never showed {(expected or sheet_name)!r} - "
                 f"nothing written to avoid a wrong row",
        )
    else:
        v = ex.Verdict(ex.ST_TIMEOUT, note="no result before timeout")
    v.panel_text = panel_text
    return v, final_sig



# ---------------------------------------------------------------- run


def build_plan(table, cfg):
    """Resolve which rows to visit. Returns [(row_no, raw_value, url_or_None)]."""
    icfg = cfg["input"]
    url_col = table.resolve_column(icfg["url_column"])
    start = int(icfg.get("start_row") or 1)
    end = icfg.get("end_row") or table.last_row

    # Optional: the name this row is supposed to be about. A second, independent
    # answer to "is the panel showing the right person" - and the only one
    # available when the URL is an opaque member id and the page title lags.
    name_cols = []
    for ref in (icfg.get("name_columns") or []):
        try:
            name_cols.append(table.resolve_column(ref))
        except (KeyError, ValueError):
            pass

    plan = []
    for row_no in range(start, int(end) + 1):
        raw = table.get(row_no, url_col)
        if not raw:
            continue
        sheet_name = " ".join(table.get(row_no, c) for c in name_cols).strip()
        plan.append((row_no, raw, ex.normalize_linkedin_url(raw), sheet_name))
    return plan


def resolve_output_columns(table, cfg):
    """Map logical field -> column index, creating columns and headers as needed.

    A column of "last" is placed after everything else, so it stays the final
    column however wide the sheet grows.
    """
    cols_cfg = cfg["output"].get("columns") or {}
    headers = cfg["output"].get("headers") or {}
    out = {}
    deferred = []

    for field, ref in cols_cfg.items():
        if not ref:
            continue
        if str(ref).strip().lower() == "last":
            deferred.append(field)
            continue
        idx = table.resolve_column(ref, create=True)
        out[field] = idx
        table.set_header(idx, headers.get(field, field))

    for field in deferred:
        header = headers.get(field, field)
        try:
            # Keep the existing column if this sheet has already been through
            # a run, rather than appending a second copy each time.
            idx = table.resolve_column(header)
        except (KeyError, ValueError):
            idx = max([table.width] + [i + 1 for i in out.values()])
            table.ensure_width(idx + 1)
        out[field] = idx
        table.set_header(idx, header)

    return out


def write_result(table, row_no, cols, verdict, url, not_found_text="not found"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # When Wiza answered definitively, an empty field means "Wiza has nothing",
    # so say that in the cell. After a timeout or an error we do NOT know, and
    # a blank cell is the honest answer - it also lets --resume retry the row.
    blank = not_found_text if verdict.definite else ""
    values = {
        "email": verdict.email or blank,
        "phone": verdict.phone or blank,
        "status": verdict.status,
        "all_emails": "; ".join(verdict.emails),
        "all_phones": "; ".join(verdict.phones),
        "checked_at": now,
        "note": verdict.note,
        "name": getattr(verdict, "name", ""),
        "headline": getattr(verdict, "headline", ""),
        "profile_url": getattr(verdict, "profile_url", url),
        # Stamped only when we actually have an answer (or the row is
        # unusable). An infra failure - browser died, panel shut, page never
        # loaded - is not an answer, and stamping it would drop the row from
        # every future run.
        "searched": "yes" if verdict.status in ex.SEARCH_STAMPED else "",
    }
    for field, idx in cols.items():
        if field in values:
            table.set(row_no, idx, values[field])


def human_pause(cfg, done_count):
    t = cfg["timing"]
    lo, hi = float(t["min_delay_s"]), float(t["max_delay_s"])
    every = int(t.get("long_pause_every") or 0)
    if every and done_count and done_count % every == 0:
        r = t.get("long_pause_range_s") or [60, 120]
        delay = random.uniform(float(r[0]), float(r[1]))
        C.info(f"--- long pause {delay:.0f}s after {done_count} profiles ---")
    else:
        delay = random.uniform(lo, hi)
    time.sleep(delay)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--input", help="override input.path")
    ap.add_argument("--url-column", help="column letter or header name holding the profile")
    ap.add_argument("--start-cell", help="e.g. B2 - sets url column and start row at once")
    ap.add_argument("--start-row", type=int)
    ap.add_argument("--end-row", type=int)
    ap.add_argument("--output", help="override output.path")
    ap.add_argument("--in-place", action="store_true", help="write back into the input file")
    ap.add_argument("--limit", type=int, help="stop after N profiles this run")
    ap.add_argument("--no-resume", action="store_true", help="redo rows that already have a status")
    ap.add_argument("--retry-failed", action="store_true",
                    help="revisit rows already marked Searched? whose result was "
                         "a failure (timeout, stale_panel, wiza_error)")
    ap.add_argument("--no-click", action="store_true", help="never press a reveal button")
    ap.add_argument("--ignore-hours", action="store_true",
                    help="run outside limits.working_hours just this once. The "
                         "window exists because a consistent odd-hours pattern is "
                         "an easy automation signal; a one-off is far less of one.")
    ap.add_argument("--cdp-url")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve rows and print the plan; never opens a browser")
    args = ap.parse_args()

    cfg = apply_cli_overrides(load_config(args.config), args)
    icfg, ocfg, rcfg = cfg["input"], cfg["output"], cfg["run"]

    in_path = icfg["path"]
    if not os.path.exists(in_path):
        sys.exit(f"Input file not found: {in_path}")

    out_path = in_path if ocfg.get("in_place") else ocfg["path"]
    header_row = icfg.get("header_row")
    resume = bool(rcfg.get("resume", True))
    retry_failed = bool(getattr(args, "retry_failed", False))
    not_found_text = ocfg.get("not_found_text", "not found") or ""

    # Resume reads the previous OUTPUT so earlier results are preserved.
    if not ocfg.get("in_place") and resume and os.path.exists(out_path):
        table = sh.load(out_path, icfg.get("sheet"), header_row)
        C.info(f"Resuming from existing output: {out_path}")
    else:
        table = sh.load(in_path, icfg.get("sheet"), header_row)
        C.info(f"Loaded {in_path}")

    plan = build_plan(table, cfg)
    cols = resolve_output_columns(table, cfg)
    status_col = cols.get("status")

    # Filter out rows already settled.
    searched_col = cols.get("searched")
    todo = []
    skipped_failed = 0
    for row_no, raw, url, sheet_name in plan:
        if resume:
            # The Searched? stamp wins: once a profile has been opened we do
            # not open it again, whatever the outcome was.
            if searched_col is not None and table.get(row_no, searched_col):
                if not retry_failed:
                    continue
                prior = table.get(row_no, status_col) if status_col is not None else ""
                if prior in FINAL_STATUSES:
                    continue
                skipped_failed += 1      # deliberately revisiting this one
            elif status_col is not None:
                prior = table.get(row_no, status_col)
                if prior and prior in FINAL_STATUSES:
                    continue
        todo.append((row_no, raw, url, sheet_name))

    if retry_failed and skipped_failed:
        C.info(f"--retry-failed: revisiting {skipped_failed} previously failed row(s)")

    already_done = len(plan) - len(todo)      # genuinely settled on a past run

    cap = rcfg.get("max_profiles")
    if cap:
        todo = todo[: int(cap)]

    # --- volume budget -----------------------------------------------------
    lcfg = cfg.get("limits") or {}
    budget = lim.DailyBudget(lcfg.get("daily_cap"),
                             lcfg.get("state_file")
                             or os.path.join(LOG_DIR, "daily_state.json"))
    window = lcfg.get("working_hours")

    outside_hours = not lim.within_working_hours(window)
    if outside_hours and args.ignore_hours:
        # Deliberate override. The risk is a REPEATED odd-hours pattern, not a
        # single late session, so say that rather than just going quiet.
        C.warn(f"--ignore-hours: running outside {lim.describe_window(window)}. "
               "A one-off is minor; the same odd hour every night is the pattern "
               "that gets noticed.")
    elif outside_hours and not args.dry_run:
        sys.exit(
            f"\nOutside the configured working hours "
            f"({lim.describe_window(window)}).\n"
            "An account that views profiles at 4am every day is one of the\n"
            "easier automation signals to spot.\n\n"
            "  wait, or\n"
            "  pass --ignore-hours to override just this run, or\n"
            "  change limits.working_hours in config.yaml to widen the window\n"
        )

    if budget.cap:
        C.info(f"Daily budget: {budget.used}/{budget.cap} used today "
               f"({lim.describe_window(window)})")
        if budget.exhausted and not args.dry_run:
            sys.exit(
                f"\nToday's budget of {budget.cap} profiles is already spent.\n"
                "Resume tomorrow - the sheet remembers where it stopped.\n"
            )
        if budget.remaining is not None and len(todo) > budget.remaining:
            C.warn(f"Trimming this run to {budget.remaining} rows to stay "
                   f"inside today's budget.")
            todo = todo[: budget.remaining]

    # Separate the reasons. Lumping "capped for this run" in with "already
    # done" reads as though most of the file was finished when none of it was.
    held_back = len(plan) - already_done - len(todo)
    parts = [f"{len(plan)} rows in range", f"{len(todo)} to process now"]
    if already_done:
        parts.append(f"{already_done} already settled")
    if held_back:
        parts.append(f"{held_back} held back by this run's limit/budget")
    C.info(", ".join(parts))
    C.info(f"Writing to: {out_path}")

    if args.dry_run:
        print("\n--- DRY RUN: first 20 planned profiles ---")
        for row_no, raw, url, *_ in todo[:20]:
            print(f"  row {row_no:<5} {raw[:44]:<44} -> {url or 'UNPARSEABLE'}")
        bad = [r for r in todo if not r[2]]
        if bad:
            print(f"\n  {len(bad)} row(s) could not be read as a LinkedIn profile, "
                  f"e.g. row {bad[0][0]}: {bad[0][1]!r}")
        print("\nNo browser was opened. Drop --dry-run to run for real.")
        return

    if not todo:
        C.info("Nothing to do.")
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"run_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
    classifier = ex.Classifier(cfg["wiza"])

    pw, browser, context = connect(cfg["browser"]["cdp_url"])
    enforce_lock(context, cfg, pw, browser)
    page = pick_page(context, cfg["browser"].get("reuse_tab", True))
    C.info(f"Attached to Chrome ({len(context.pages)} tab(s) open)")

    # A logged-out LinkedIn serves public profiles happily, so the run would
    # look healthy while Wiza matched nothing. Catch it before burning rows.
    if cfg["run"].get("require_linkedin_login", True):
        states = [linkedin_signed_in(p) for p in context.pages
                  if "linkedin.com" in (p.url or "")]
        if states and all(s is False for s in states):
            detach(pw, browser)
            sys.exit(
                "\nLinkedIn is NOT signed in in the automation browser.\n\n"
                "Every LinkedIn tab open there is showing the logged-out view.\n"
                "Public profiles still render, so this would run and find almost\n"
                "nothing. Sign in first:\n"
                "  powershell -ExecutionPolicy Bypass -File show-browser.ps1\n"
            )

    keeper = tb.TabKeeper(context, cfg)
    if cfg["browser"].get("close_stale_profile_tabs", True):
        swept = keeper.close_stale_profile_tabs(page)
        if swept:
            C.info(f"Closed {swept} leftover LinkedIn profile tab(s) from earlier")

    tracker = prog.Progress(len(todo), cfg)
    C.info(tracker.opening())

    counts, processed, fatal = {}, 0, None
    panel_sig = None   # what the side panel showed at the end of the last profile
    last_contact = None  # the previous row's accepted email/phone, to spot a
                         # panel that has not refreshed its data yet
    try:
        for i, (row_no, raw, url, sheet_name) in enumerate(todo, 1):
            if not url:
                v = ex.Verdict(ex.ST_BAD_URL, note=f"unreadable value: {raw[:60]}")
                write_result(table, row_no, cols, v, raw, not_found_text)
                sh.save(table, out_path)
                counts[v.status] = counts.get(v.status, 0) + 1
                C.row(i, len(todo), row_no, raw, v.status)
                continue

            # A fresh tab per profile, in the same window. The previous tab is
            # not closed until this one has taken over, so the side panel always
            # has a live tab to stay bound to.
            page = keeper.next_tab(page)

            # Extra headroom: a browser-recovery pass consumes an attempt,
            # and we must not run out of them while waiting for Chrome.
            attempts = int(rcfg.get("retry_on_error", 1)) + 4
            for attempt in range(1, attempts + 1):
                v, panel_sig = process_profile(page, url, cfg, classifier,
                                               context, panel_sig, sheet_name,
                                               last_contact)

                if v.note == "checkpoint" or v.note == "checkpoint mid-profile":
                    C.warn("LinkedIn is asking for a human (login wall / checkpoint).")
                    if rcfg.get("pause_on_checkpoint", True) and sys.stdin.isatty():
                        input("    Solve it in the Chrome window, then press Enter here... ")
                        continue
                    fatal = "checkpoint"
                    break

                if v.status == ex.ST_LIMIT:
                    fatal = v.note
                    break

                # The browser went away - screen lock, sleep, or a crash. Pause
                # and wait for it rather than racing through the rest of the
                # sheet recording errors against rows nobody ever looked at.
                if connection_lost(v):
                    revived = wait_for_browser(
                        cfg, pw, browser,
                        max_wait_s=float(rcfg.get("browser_wait_s", 900)))
                    if revived is None:
                        fatal = "browser did not come back"
                        break
                    pw, browser, context, page = revived
                    keeper = tb.TabKeeper(context, cfg)
                    panel_sig = None      # the panel restarted; trust nothing
                    continue              # re-attempt this same row

                # Retry IN-RUN only for transient navigation failures. A
                # timeout or a stale panel means the panel was not co-operating
                # seconds ago and will not be co-operating seconds from now -
                # retrying doubles the cost of every failure (measured: 140s a
                # row) for almost no gain. Those rows are retried on the NEXT
                # run by --resume, when conditions have actually changed.
                if v.status in (ex.ST_ERROR,) and attempt < attempts:
                    C.warn(f"row {row_no} {v.status} ({v.note}) - retrying")
                    time.sleep(5)
                    continue
                break

            if fatal:
                break

            write_result(table, row_no, cols, v, url, not_found_text)
            sh.save(table, out_path)      # flush every row: crash-safe
            if v.emails or v.phones:
                last_contact = (tuple(v.emails), tuple(v.phones))

            processed += 1
            budget.record(1)          # persisted, so a restart cannot reset it
            counts[v.status] = counts.get(v.status, 0) + 1

            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "row": row_no, "url": url, "status": v.status,
                    "email": v.email, "phone": v.phone, "note": v.note,
                }) + "\n")

            detail = v.email or v.phone or v.note
            C.row(i, len(todo), row_no, url, v.status, detail[:50])

            if cfg["run"].get("save_panel_text_on_failure") and getattr(v, "panel_text", ""):
                dump = os.path.join(LOG_DIR, f"panel_row{row_no}.txt")
                with open(dump, "w", encoding="utf-8") as df:
                    df.write(v.panel_text)

            # This profile is done with, so the tab it replaced can go now.
            keeper.retire_previous(page)
            keeper.reap_leaked(page, cfg["browser"].get("max_open_profile_tabs", 3))

            if i < len(todo):
                human_pause(cfg, processed)

            tracker.tick()
            # A progress line often enough to be useful, rare enough to read.
            if i % 10 == 0 or i == len(todo):
                C.info(tracker.summary())

    except KeyboardInterrupt:
        C.warn("Interrupted - saving progress.")
    finally:
        sh.save(table, out_path)
        try:
            keeper.close_all_ours(keep=page)   # leave the window as we found it
        except Exception:
            pass
        detach(pw, browser)                    # disconnect; do NOT close Chrome

    print("\n" + "=" * 62)
    if fatal:
        C.warn(f"RUN STOPPED: {fatal}")
        if "credit" in str(fatal).lower() or "limit" in str(fatal).lower():
            C.warn("Wiza is reporting the quota is gone. Nothing here can work around that.")
    print(f"  processed this run : {processed} in {prog.fmt_duration(tracker.elapsed)}")
    if processed:
        print(f"  pace               : ~{tracker.rate_per_hour:.0f}/hour")
    if tracker.remaining_rows:
        print(f"  still to do        : {tracker.remaining_rows} "
              f"(~{prog.fmt_duration(tracker.eta_seconds)} more)")
    if keeper.closed_count:
        print(f"  tabs cleaned up    : {keeper.closed_count}")
    for k in sorted(counts):
        print(f"  {k:<19}: {counts[k]}")
    if budget.cap:
        print(f"  daily budget       : {budget.used}/{budget.cap} used today")
    print(f"  sheet              : {out_path}")
    print(f"  log                : {log_path}")
    print("  re-run the same command to resume where this stopped.")


if __name__ == "__main__":
    main()
