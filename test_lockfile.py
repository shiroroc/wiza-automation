"""Tests for the browser/port/profile lock.

The bug these exist to prevent: a lock that cannot be READ is a lock that is
not protecting anything, and it fails silently. PowerShell 5.1 writes UTF-8
with a BOM, which plain utf-8 json.load() rejects - so the loader must tolerate
it, and an unreadable lock must never be mistaken for a passing check.

Run: python test_lockfile.py
"""

import io
import json
import os
import shutil
import sys
import tempfile

import lockfile as lf

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {label}  got={got!r} want={want!r}")
    else:
        print(f"  ok   {label}")


PROFILE = r"C:\Dev\automation-wiza\.chrome-profile"
LOCK = {"chrome_path": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "profile_dir": PROFILE, "port": 9222, "cdp_url": "http://127.0.0.1:9222"}


class FakeContext:
    """Stands in for a browser reporting a given chrome://version body."""

    def __init__(self, profile_path, fail=False):
        self.profile_path = profile_path
        self.fail = fail
        self.opened = 0
        self.closed = 0

    def new_page(self):
        self.opened += 1
        return FakePage(self)


class FakePage:
    def __init__(self, ctx):
        self.ctx = ctx

    def goto(self, url, timeout=None):
        if self.ctx.fail:
            raise RuntimeError("chrome:// navigation blocked")

    def inner_text(self, sel, timeout=None):
        return (f"Google Chrome\t152.0.7977.83\n"
                f"Command Line\tchrome.exe --remote-debugging-port=9222\n"
                f"Profile Path\t{self.ctx.profile_path}\n"
                f"Variations\tabc")

    def close(self):
        self.ctx.closed += 1


# ------------------------------------------------------------- file handling
print("\n[reading the lock file]")
tmp = tempfile.mkdtemp(prefix="wiza_lock_")
try:
    # Exactly what Windows PowerShell 5.1's Out-File -Encoding utf8 produces.
    bom_path = os.path.join(tmp, lf.LOCK_NAME)
    with io.open(bom_path, "w", encoding="utf-8-sig") as f:
        json.dump(LOCK, f)
    got = lf.load(tmp)
    check("reads a BOM-prefixed file (the PowerShell 5.1 default)",
          got is not None and got["profile_dir"], PROFILE)

    with io.open(bom_path, "w", encoding="utf-8") as f:
        json.dump(LOCK, f)
    check("reads a plain utf-8 file too", lf.load(tmp)["port"], 9222)

    with io.open(bom_path, "w", encoding="utf-8") as f:
        f.write("{ not json at all")
    check("corrupt file returns None, does not raise", lf.load(tmp), None)

    os.unlink(bom_path)
    check("missing file returns None", lf.load(tmp), None)

    saved = lf.save(LOCK, tmp)
    check("round-trips what it wrote", lf.load(tmp)["profile_dir"], PROFILE)
    check("stamps a write time", "written_at" in lf.load(tmp), True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------- path comparison
print("\n[profile path matching]")
check("chrome's \\Default suffix still matches the user-data-dir",
      lf.profile_matches(PROFILE + r"\Default", PROFILE), True)
check("exact match", lf.profile_matches(PROFILE, PROFILE), True)
check("case-insensitive on Windows",
      lf.profile_matches(PROFILE.upper() + r"\Default", PROFILE.lower()),
      os.name == "nt")
check("trailing separator ignored",
      lf.profile_matches(PROFILE + r"\Default", PROFILE + "\\"), True)
check("a different profile does NOT match",
      lf.profile_matches(r"C:\Users\me\AppData\Local\Google\Chrome\User Data\Default",
                         PROFILE), False)
check("empty is never a match", lf.profile_matches("", PROFILE), False)
check("a prefix-looking sibling does not match",
      lf.profile_matches(PROFILE + "-other\\Default", PROFILE), False)


# ------------------------------------------------------------- verification
print("\n[verify]")
ok, msg = lf.verify(FakeContext(PROFILE + r"\Default"), LOCK, expected_port=9222)
check("the real automation browser passes", ok, True)

ctx = FakeContext(r"C:\Users\me\AppData\Local\Google\Chrome\User Data\Default")
ok, msg = lf.verify(ctx, LOCK, expected_port=9222)
check("your everyday Chrome is REFUSED", ok, False)
check("and the message names both profiles", "NOT the automation browser" in msg, True)
check("the scratch tab is always closed again", ctx.closed, 1)

ok, msg = lf.verify(FakeContext(PROFILE + r"\Default"), LOCK, expected_port=9333)
check("a port that disagrees with the lock is refused", ok, False)

ok, msg = lf.verify(FakeContext(PROFILE, fail=True), LOCK, expected_port=9222)
check("unreadable chrome://version does not hard-fail the run", ok, True)
check("but says so plainly", "not verified" in msg, True)

ok, msg = lf.verify(FakeContext(PROFILE), None)
check("no lock at all is permitted here (callers warn separately)", ok, True)


# ------------------------------------------------------------------- ports
print("\n[port parsing]")
check("from a cdp url", lf.port_from_cdp_url("http://127.0.0.1:9222"), 9222)
check("with a path", lf.port_from_cdp_url("http://localhost:9333/json"), 9333)
check("no port", lf.port_from_cdp_url("http://localhost"), None)
check("empty", lf.port_from_cdp_url(""), None)


print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):\n")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("Lock tests passed.")
