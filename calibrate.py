#!/usr/bin/env python3
"""Discover how the Wiza panel is built, so you can fill in config.yaml.

The extension's DOM is not something this script can know in advance, and Wiza
ships UI changes. So: open one profile, look at what is actually there, and
diff the panel before and after you click Find by hand.

    python calibrate.py https://www.linkedin.com/in/someone/

It prints the candidate panel roots, every button it can see, and - after you
press Find yourself - exactly which text appeared. That tells you what to put in
wiza.find_button.text_patterns and wiza.not_found_patterns.

This costs one lookup from your plan. It does not click anything on its own.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import yaml
from playwright.sync_api import Error as PWError

from browser_probe import JS_PROBE
from wiza_auto import (connect, detach, extension_targets, load_config,
                       pick_page)

OUT_DIR = "calibration"


def snapshot(page, wcfg, label, context=None):
    """Probe the page's frames AND every extension target (the side panel).

    Wiza renders into Chrome's side panel, which is its own browser target -
    not a frame of the LinkedIn tab - so page.frames alone would show nothing.
    """
    hints = [h.lower() for h in (wcfg.get("root_hints") or [])]
    marker = wcfg.get("frame_url_contains") or "chrome-extension://"

    frames = list(page.frames)
    if context is not None:
        for ep in extension_targets(context, marker):
            if ep is not page:
                try:
                    frames.extend(ep.frames)
                except PWError:
                    pass

    frames_out = []
    for frame in frames:
        try:
            url = frame.url or ""
        except PWError:
            continue
        is_ext = marker and marker in url

        entry = {"url": url, "is_extension_frame": bool(is_ext), "roots": []}
        opts = {
            "panelSelector": wcfg.get("panel_selector"),
            "rootHints": hints,
            # For calibration, read extension frames whole so we see everything.
            "wholeDocument": bool(is_ext),
        }
        try:
            res = frame.evaluate(JS_PROBE, opts)
            entry["roots"] = (res or {}).get("roots", []) or []
        except PWError as e:
            entry["error"] = str(e)[:200]
        frames_out.append(entry)

    return {"label": label, "at": datetime.now().isoformat(timespec="seconds"),
            "frames": frames_out}


def summarize(snap):
    print(f"\n{'=' * 70}\n  SNAPSHOT: {snap['label']}\n{'=' * 70}")
    any_root = False
    for f in snap["frames"]:
        roots = f.get("roots") or []
        tag = " [EXTENSION FRAME]" if f["is_extension_frame"] else ""
        if not roots and not f.get("error"):
            continue
        print(f"\n  frame: {f['url'][:90]}{tag}")
        if f.get("error"):
            print(f"    ! probe error: {f['error']}")
        for i, r in enumerate(roots):
            any_root = True
            text = (r.get("text") or "").strip()
            print(f"    root #{i}: {len(text)} chars of text, "
                  f"{len(r.get('buttons') or [])} clickable(s)")
            if r.get("mailtos"):
                print(f"      mailto: links : {r['mailtos']}")
            if r.get("tels"):
                print(f"      tel: links    : {r['tels']}")
            if text:
                print("      --- text ---")
                for line in text.splitlines()[:40]:
                    print(f"      | {line[:100]}")
            btns = [b for b in (r.get("buttons") or []) if b.get("visible") and b.get("text")]
            if btns:
                print("      --- visible buttons ---")
                for b in btns[:25]:
                    dis = " (disabled)" if b.get("disabled") else ""
                    print(f"      | <{b['tag']}> {b['text'][:70]!r}{dis}")
    if not any_root:
        print("\n  No panel roots found.")
        print("  Either the Wiza panel is not open, or it does not use 'wiza' in its")
        print("  class/id. Try widening wiza.root_hints in config.yaml, or open the")
        print("  panel in the Chrome window before running this.")


def text_of(snap):
    out = []
    for f in snap["frames"]:
        for r in f.get("roots") or []:
            out.append(r.get("text") or "")
    return "\n".join(out)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python calibrate.py <linkedin-profile-url> [--config config.yaml]")

    url = sys.argv[1]
    cfg_path = "config.yaml"
    if "--config" in sys.argv:
        cfg_path = sys.argv[sys.argv.index("--config") + 1]
    cfg = load_config(cfg_path)
    wcfg = cfg["wiza"]

    pw, browser, context = connect(cfg["browser"]["cdp_url"])
    page = pick_page(context, cfg["browser"].get("reuse_tab", True))

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snaps = []

    try:
        # The single most useful diagnostic: can the script see Wiza's side
        # panel at all? If this list has no EXTENSION row, nothing else matters.
        print("\n  Browser targets visible to the script:")
        for p_ in context.pages:
            try:
                kind = "EXTENSION" if "chrome-extension://" in (p_.url or "") else "page"
                print(f"    [{kind:9}] {(p_.url or '')[:88]}")
            except PWError:
                pass
        if not any("chrome-extension://" in (p_.url or "") for p_ in context.pages):
            print("    !! No extension target is visible.")
            print("       Wiza renders into Chrome's SIDE PANEL. Open it and pin it")
            print("       (the pin icon in its header) before running this.")

        print(f"\nOpening {url} ...")
        page.goto(url, wait_until="domcontentloaded",
                  timeout=cfg["timing"]["page_load_timeout_ms"])
        page.wait_for_timeout(6000)

        before = snapshot(page, wcfg, "before you click Find", context)
        snaps.append(before)
        summarize(before)

        print("\n" + "-" * 70)
        print("  Now, IN THE CHROME WINDOW, click Wiza's Find/Reveal button yourself.")
        print("  Wait for the result (email, phone, or 'not found') to appear.")
        input("  Then press Enter here... ")

        after = snapshot(page, wcfg, "after you clicked Find", context)
        snaps.append(after)
        summarize(after)

        # The diff is the useful part: it is exactly the result markup.
        b, a = set(text_of(before).splitlines()), text_of(after).splitlines()
        new = [ln for ln in a if ln.strip() and ln not in b]
        print(f"\n{'=' * 70}\n  NEW TEXT THAT APPEARED AFTER YOUR CLICK\n{'=' * 70}")
        if new:
            for ln in new[:50]:
                print(f"  + {ln[:110]}")
            print("\n  Use these lines to set wiza.not_found_patterns (if this profile")
            print("  had no data) in config.yaml. Emails and phones are auto-detected.")
        else:
            print("  Nothing new - the panel may render into a frame that was replaced,")
            print("  or the result was already visible in the first snapshot.")

        path = os.path.join(OUT_DIR, f"calibration_{stamp}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snaps, f, indent=2, ensure_ascii=False)
        print(f"\n  Full dump saved to {path}")
        print("  Paste that file back to your dev if the buttons are not matching.")

    finally:
        # Disconnect only - closing a CDP-attached browser shuts Chrome down,
        # taking the LinkedIn and Wiza sessions with it.
        detach(pw, browser)


if __name__ == "__main__":
    main()
