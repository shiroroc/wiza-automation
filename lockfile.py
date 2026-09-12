"""Pin the browser, the port and the profile so only the logins can vary.

The failure this exists to prevent: port 9222 is already held, the script
assumes that is our automation Chrome, attaches, and quietly drives whatever
browser is actually there - including your everyday Chrome signed into your
personal LinkedIn. Nothing would look wrong until the wrong account got
restricted.

So the launcher records what it started in .wiza-lock.json, and every run asks
the attached browser to prove it is that same one. Chrome answers honestly:
chrome://version reports its own profile path and command line.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

LOCK_NAME = ".wiza-lock.json"

_PROFILE_RE = re.compile(r"^\s*Profile Path\s+(.+?)\s*$", re.M)
_CMDLINE_RE = re.compile(r"^\s*Command Line\s+(.+?)\s*$", re.M)


# -- the lock file ----------------------------------------------------------


def lock_path(project_dir: str = ".") -> str:
    return os.path.join(project_dir, LOCK_NAME)


def load(project_dir: str = "."):
    # utf-8-sig, not utf-8: Windows PowerShell 5.1 writes UTF-8 WITH a BOM, and
    # plain utf-8 decoding chokes on it. That failure is silent and dangerous -
    # an unreadable lock disables the profile check entirely - so be tolerant.
    try:
        with open(lock_path(project_dir), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(data: dict, project_dir: str = ".") -> str:
    path = lock_path(project_dir)
    data = dict(data)
    data["written_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return path


# -- asking the browser who it is -------------------------------------------


def _norm(p: str) -> str:
    """Compare paths the way the filesystem does, not the way strings do."""
    if not p:
        return ""
    p = os.path.normpath(os.path.abspath(os.path.expanduser(str(p).strip().strip('"'))))
    return p.rstrip("\\/").lower() if os.name == "nt" else p.rstrip("/")


def read_identity(context, timeout_ms: int = 8000):
    """Ask the attached Chrome for its own profile path and command line.

    Opens chrome://version in a scratch tab and closes it again. Returns
    {"profile_path": ..., "command_line": ...} or None if it could not be read.
    """
    page = None
    try:
        page = context.new_page()
        page.goto("chrome://version", timeout=timeout_ms)
        text = page.inner_text("body", timeout=timeout_ms)
    except Exception:
        return None
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    prof = _PROFILE_RE.search(text or "")
    cmd = _CMDLINE_RE.search(text or "")
    if not prof:
        return None
    return {
        "profile_path": prof.group(1).strip(),
        "command_line": cmd.group(1).strip() if cmd else "",
    }


def profile_matches(reported_profile: str, locked_profile: str) -> bool:
    """Chrome reports '<user-data-dir>\\Default'; the lock holds the user-data-dir."""
    rep, lock = _norm(reported_profile), _norm(locked_profile)
    if not rep or not lock:
        return False
    return rep == lock or rep.startswith(lock + os.sep) or os.path.dirname(rep) == lock


def verify(context, lock: dict, expected_port=None):
    """(ok, message). ok=False means: do not touch this browser.

    A lock that cannot be read at all is reported separately from one that
    reads fine but names a different profile - the first is inconvenient, the
    second is the dangerous case.
    """
    if not lock:
        return True, "no lock file yet - skipping the profile check"

    ident = read_identity(context)
    if ident is None:
        return True, "could not read chrome://version - profile not verified"

    locked_profile = lock.get("profile_dir")
    if not profile_matches(ident["profile_path"], locked_profile):
        return False, (
            "This is NOT the automation browser.\n"
            f"  attached to : {ident['profile_path']}\n"
            f"  expected    : {locked_profile}\n"
            "Something else is holding the debug port - most likely your everyday\n"
            "Chrome. Driving it would use the wrong LinkedIn account. Close it and\n"
            "run launch-chrome.ps1 again."
        )

    if expected_port and lock.get("port") and int(expected_port) != int(lock["port"]):
        return False, (
            f"Port mismatch: config says {expected_port}, the lock says {lock['port']}. "
            "Point browser.cdp_url at the locked port, or re-run launch-chrome.ps1."
        )

    return True, f"verified: {ident['profile_path']}"


def port_from_cdp_url(url: str):
    m = re.search(r":(\d+)", str(url or ""))
    return int(m.group(1)) if m else None
