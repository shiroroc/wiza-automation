"""Turn raw panel text into an email, a phone, and a verdict.

Kept free of any browser dependency so it can be unit-tested directly
(see test_extract.py).
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Sequence

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Deliberately permissive; the strict filters below do the real work.
# The optional leading "(" matters: without it "(415) 555-0132" loses its paren.
PHONE_RE = re.compile(r"\+?\(?\d[\d\s().\-]{6,}\d")

# FRAGILE: a repo-wide "smart punctuation to ASCII" pass once rewrote the
# bullet here into a HYPHEN, which silently made every phone number containing
# "-" look like a masked preview and discarded them all. test_text_safety.py
# pins these four code points so that failure cannot ship again.
#   * = asterisk, U+2022 bullet, U+25CF black circle, U+00B7 middle dot
DEFAULT_MASK_CHARS = "*•●·"

# Statuses written to the sheet.
ST_EMAIL_PHONE = "email+phone"
ST_EMAIL = "email"
ST_PHONE = "phone"
ST_NOT_FOUND = "not_found"
ST_NO_MATCH = "no_match"       # Wiza matched the profile to no contact at all
ST_LIMIT = "limit_reached"     # fatal: aborts the whole run
ST_TIMEOUT = "timeout"
ST_NO_PANEL = "no_panel"
ST_WIZA_ERROR = "wiza_error"   # Wiza refused, e.g. "Name can't be blank"
ST_STALE_PANEL = "stale_panel"  # panel never showed the right person
ST_ERROR = "error"
ST_SKIPPED = "skipped"
ST_BAD_URL = "bad_url"

PENDING = "__pending__"   # internal: keep polling

# Statuses where Wiza gave a definite answer, so an empty email/phone cell
# genuinely means "not found" rather than "we never got to look".
DEFINITE = {ST_EMAIL_PHONE, ST_EMAIL, ST_PHONE, ST_NOT_FOUND, ST_NO_MATCH}

# Only these get the permanent "Searched?" stamp. A profile is marked done when
# we actually have an answer about it - or when the row can never work at all.
# An infrastructure failure (the browser died, the panel was shut, the page
# never loaded) is NOT an answer: stamping those loses the row forever, because
# it would never be retried. Losing a row costs more than one repeat lookup.
SEARCH_STAMPED = DEFINITE | {ST_BAD_URL, ST_SKIPPED}


class Verdict:
    panel_error = False   # the panel is showing its own failure screen

    def __init__(self, status, emails=None, phones=None, note="", no_match=False):
        self.status = status
        self.emails = emails or []
        self.phones = phones or []
        self.note = note
        # True for the "we couldn't find this contact" screen, which shows no
        # name - the runner relaxes its name check for exactly this case.
        self.no_match = no_match

    @property
    def email(self) -> str:
        return self.emails[0] if self.emails else ""

    @property
    def phone(self) -> str:
        return self.phones[0] if self.phones else ""

    @property
    def done(self) -> bool:
        return self.status != PENDING

    @property
    def definite(self) -> bool:
        return self.status in DEFINITE

    def __repr__(self):
        return f"<Verdict {self.status} email={self.email!r} phone={self.phone!r}>"


def _compile(patterns: Sequence[str]) -> List[re.Pattern]:
    return [re.compile(p, re.I) for p in (patterns or [])]


def _matches_any(text: str, compiled: List[re.Pattern]) -> Optional[str]:
    for rx in compiled:
        m = rx.search(text)
        if m:
            return m.group(0)
    return None


def _dedupe(items):
    seen, out = set(), []
    for it in items:
        key = it.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out


def _is_masked(value: str, mask_chars: str) -> bool:
    """Wiza previews unrevealed data as ***@uber.com / +1 (***) *** ****."""
    return any(c in value for c in (mask_chars or DEFAULT_MASK_CHARS))


def find_emails(text: str, mailtos: Sequence[str], exclude: List[re.Pattern],
                mask_chars: str = DEFAULT_MASK_CHARS) -> List[str]:
    """mailto: hrefs first - they are the highest-confidence signal."""
    cands = list(mailtos) + EMAIL_RE.findall(text or "")
    out = []
    for e in cands:
        e = e.strip().strip(".,;:")
        if not EMAIL_RE.fullmatch(e):
            continue
        if _is_masked(e, mask_chars):
            continue
        if any(rx.search(e) for rx in exclude):
            continue
        out.append(e)
    return _dedupe(out)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def find_phones(text: str, tels: Sequence[str], strict: bool,
                label_patterns: Sequence[str],
                mask_chars: str = DEFAULT_MASK_CHARS) -> List[str]:
    """tel: hrefs are trusted outright. Loose text needs a reason to be believed.

    In strict mode a bare number in the panel is only accepted if it is E.164
    (+...) or sits on a line carrying a phone-ish label. That keeps LinkedIn
    noise - follower counts, years, "500+ connections" - out of the phone column.
    """
    out = [t.strip() for t in tels
           if 7 <= len(_digits(t)) <= 15 and not _is_masked(t, mask_chars)]

    labels = [re.compile(p, re.I) for p in (label_patterns or [])]
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # A line carrying mask characters is an unrevealed preview. Skip the
        # whole line: "+1 (415) *** ****" must never be read as a number.
        if _is_masked(line, mask_chars):
            continue
        labelled = any(rx.search(line) for rx in labels)
        for m in PHONE_RE.finditer(line):
            cand = m.group(0).strip()
            d = _digits(cand)
            if not (7 <= len(d) <= 15):
                continue
            if strict and not (cand.startswith("+") or labelled):
                continue
            out.append(cand)

    return _dedupe(out)


# -- profile-name verification ---------------------------------------------
#
# The Wiza side panel persists across navigation and repaints a moment after
# the page changes. Without this check, a result read too early is written to
# the wrong row. These helpers answer: "is the panel showing this person yet?"

_NAME_NOISE = {
    "dr", "mr", "mrs", "ms", "miss", "prof", "professor",
    "phd", "ph", "d", "md", "mba", "msc", "bsc", "bed", "jr", "sr",
    "ii", "iii", "iv", "cfa", "cpa", "pmp", "he", "him", "she", "her",
    "they", "them", "his", "hers",
}


def name_tokens(name: str) -> List[str]:
    """Significant lowercase tokens of a person's name.

    Strips accents, punctuation, honorifics, post-nominals and pronouns, so
    "Asha Patel, Ph.D." and "Asha Patel" reduce to the same core tokens.
    """
    if not name:
        return []
    # Strip accents so "Jose" and "José" agree, then split on non-word
    # characters WITH Unicode semantics. Splitting on [^a-z0-9] instead would
    # discard every CJK, Arabic, Cyrillic or Devanagari name entirely - the
    # guard could then never match, and those profiles would be marked
    # stale_panel forever and never written.
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    raw = re.split(r"[\W_]+", s, flags=re.UNICODE)

    out = []
    for t in raw:
        if not t or t in _NAME_NOISE:
            continue
        # Latin initials are noise, but a single CJK glyph is a whole name.
        if len(t) > 1 or not t.isascii():
            out.append(t)
    return out


def name_matches(expected: str, panel_text: str) -> bool:
    """Does the panel appear to be showing `expected`?

    Requires two significant tokens to line up (or the only one, for
    single-token names). Two tokens is deliberate: a lone common first name
    like "Deepak" would otherwise match the wrong person's panel.
    """
    want = name_tokens(expected)
    if not want:
        return False
    have = set(name_tokens(panel_text))
    hits = sum(1 for t in want if t in have)
    return hits >= min(2, len(want))


class Classifier:
    """Holds the compiled config patterns and judges one panel snapshot."""

    def __init__(self, wiza_cfg: dict):
        self.not_found = _compile(wiza_cfg.get("not_found_patterns"))
        self.no_match = _compile(wiza_cfg.get("no_match_patterns"))
        self.in_progress = _compile(wiza_cfg.get("in_progress_patterns"))
        self.stop_run = _compile(wiza_cfg.get("stop_run_patterns"))
        self.panel_error = _compile(wiza_cfg.get("panel_error_patterns"))
        self.wiza_error = _compile(wiza_cfg.get("wiza_error_patterns"))
        self.email_exclude = _compile(wiza_cfg.get("email_exclude_patterns"))
        self.phone_strict = bool(wiza_cfg.get("phone_strict", True))
        self.phone_labels = wiza_cfg.get("phone_label_patterns") or []
        self.mask_chars = wiza_cfg.get("mask_chars") or DEFAULT_MASK_CHARS

    def panel_text(self, roots: Sequence[dict]) -> str:
        return "\n".join(r.get("text", "") for r in roots)

    def classify(self, roots: Sequence[dict]) -> Verdict:
        """roots: the list returned by JS_PROBE, merged across frames/pages.

        Returns a Verdict whose status may be PENDING, meaning "poll again".
        """
        if not roots:
            return Verdict(PENDING, note="panel not visible yet")

        text = self.panel_text(roots)
        mailtos, tels = [], []
        for r in roots:
            mailtos += r.get("mailtos", []) or []
            tels += r.get("tels", []) or []

        # Credits gone - the caller turns this into a hard stop for the run.
        hit = _matches_any(text, self.stop_run)
        if hit:
            return Verdict(ST_LIMIT, note=f"plan/credit limit: {hit}")

        emails = find_emails(text, mailtos, self.email_exclude, self.mask_chars)
        phones = find_phones(text, tels, self.phone_strict, self.phone_labels,
                             self.mask_chars)

        if emails and phones:
            return Verdict(ST_EMAIL_PHONE, emails, phones)
        if emails:
            return Verdict(ST_EMAIL, emails, phones)
        if phones:
            return Verdict(ST_PHONE, emails, phones)

        # Nothing yet. Is the panel telling us it is done, or still working?
        # "no match" is checked first: that screen shows no name, and the
        # runner needs the flag to relax its name check.
        hit = _matches_any(text, self.no_match)
        if hit:
            return Verdict(ST_NO_MATCH, note=hit, no_match=True)

        hit = _matches_any(text, self.not_found)
        if hit:
            return Verdict(ST_NOT_FOUND, note=hit)

        # Only now consider the error screens. A definite "No email found"
        # from the live panel must win over a stale "Wiza couldn't load" left
        # behind in the dead shell - checking errors first masked real answers
        # and turned them into timeouts.
        hit = _matches_any(text, self.wiza_error)
        if hit:
            return Verdict(ST_WIZA_ERROR, note=f"Wiza error: {hit}")

        hit = _matches_any(text, self.panel_error)
        if hit:
            v = Verdict(PENDING, note=f"panel error: {hit}")
            v.panel_error = True
            return v

        if _matches_any(text, self.in_progress):
            return Verdict(PENDING, note="in progress")

        return Verdict(PENDING, note="no signal yet")


# -- LinkedIn URL normalisation --------------------------------------------

_SLUG_OK = re.compile(r"^[A-Za-z0-9\-_%À-ÿ]{2,100}$")


def normalize_linkedin_url(value: str) -> Optional[str]:
    """Accept a full URL, a bare /in/ path, or a public-identifier slug.

    Returns a canonical https://www.linkedin.com/in/<slug>/ URL, or None if the
    value cannot be read as a personal profile reference.
    """
    if not value:
        return None
    v = str(value).strip().strip('"').strip("'")
    if not v:
        return None

    if v.startswith("//"):
        v = "https:" + v

    # Anything with a host in it: pull the /in/ segment out.
    if "linkedin.com" in v.lower():
        m = re.search(r"linkedin\.com/in/([^/?#\s]+)", v, re.I)
        if m:
            return f"https://www.linkedin.com/in/{m.group(1).rstrip('/')}/"
        return None   # company/school/post URL - not a person

    if v.lower().startswith("in/"):
        slug = v[3:].split("?")[0].split("#")[0].strip("/")
        return f"https://www.linkedin.com/in/{slug}/" if slug else None

    if v.startswith("http://") or v.startswith("https://"):
        return None   # some other site entirely

    # Bare public identifier, e.g. "satyanadella".
    slug = v.split("?")[0].split("#")[0].strip("/")
    if _SLUG_OK.match(slug):
        return f"https://www.linkedin.com/in/{slug}/"
    return None
