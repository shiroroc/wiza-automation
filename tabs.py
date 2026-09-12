"""Tab lifecycle: one fresh tab per profile, and no pile-up.

Opening each profile in its own tab is what a person doing this by hand does,
but a 300-row run would leave 300 tabs behind and eventually stall the browser.
So every tab this opens is tracked with a timestamp and closed once the next one
has taken over, with a sweep for anything that leaked.

Two rules keep this from ever eating your own work:
 - it only ever closes tabs showing a LinkedIn PROFILE (/in/...)
 - it never closes the tab currently being worked on, and never the last tab
Your Wiza dashboard, your spreadsheet, your inbox are all untouchable by this.
"""

from __future__ import annotations

import time

from playwright.sync_api import Error as PWError

PROFILE_MARKER = "linkedin.com/in/"


def _url(page) -> str:
    try:
        return page.url or ""
    except PWError:
        return ""


def is_profile_tab(page) -> bool:
    return PROFILE_MARKER in _url(page).lower()


def _close(page) -> bool:
    try:
        page.close()
        return True
    except PWError:
        return False


class TabKeeper:
    """Hands out tabs for each profile and cleans up after itself."""

    def __init__(self, context, cfg, clock=None):
        self.context = context
        self.cfg = cfg.get("browser") or {}
        self._clock = clock or time.time
        self._opened = {}        # page -> timestamp we opened it
        self._retiring = []      # tabs superseded, closed once the next one works
        self.closed_count = 0

    # -- strategy -----------------------------------------------------------

    @property
    def new_tab_mode(self) -> bool:
        return (self.cfg.get("tab_strategy") or "reuse") == "new_tab"

    # -- per-profile --------------------------------------------------------

    def next_tab(self, current):
        """The tab to use for the next profile.

        In reuse mode this is simply the tab you already had. In new_tab mode a
        fresh tab is opened in the SAME window and the previous one is queued
        for closing - not closed yet, because the side panel needs a live tab to
        stay bound to while the new one loads.
        """
        if not self.new_tab_mode:
            return current

        try:
            fresh = self.context.new_page()
        except PWError:
            return current       # window is gone or busy; keep using this one

        self._opened[fresh] = self._clock()
        if current is not None and current is not fresh:
            self._retiring.append(current)
        return fresh

    def retire_previous(self, keep):
        """Close the tab(s) the last profile used, now that `keep` has taken over."""
        still = []
        for page in self._retiring:
            if page is keep or page.is_closed():
                continue
            # Only ever close a profile tab. If the user navigated it somewhere
            # else, or it was their own tab to begin with, leave it alone.
            if is_profile_tab(page) or page in self._opened:
                if _close(page):
                    self.closed_count += 1
                    self._opened.pop(page, None)
            else:
                still.append(page)
        self._retiring = still

    # -- sweeps -------------------------------------------------------------

    def reap_leaked(self, keep, max_open=3):
        """Close our own tabs that outlived their turn (a crash, a retry loop)."""
        mine = [p for p in self._opened if not p.is_closed() and p is not keep]
        mine.sort(key=lambda p: self._opened.get(p, 0))
        while len(mine) > max(0, int(max_open) - 1):
            page = mine.pop(0)
            if _close(page):
                self.closed_count += 1
            self._opened.pop(page, None)

    def close_stale_profile_tabs(self, keep, older_than_s=None):
        """Close LinkedIn profile tabs left over from earlier sessions.

        Tabs we did not open have no age we can read, so `older_than_s` only
        filters the ones we did. Everything else is judged purely on being a
        profile tab that is not in use - which is the thing the user asked to
        have cleaned up.
        """
        closed = 0
        pages = [p for p in self.context.pages if not p.is_closed()]
        if len(pages) <= 1:
            return 0
        now = self._clock()
        for page in pages:
            if page is keep or not is_profile_tab(page):
                continue
            opened_at = self._opened.get(page)
            if opened_at is not None and older_than_s is not None:
                if now - opened_at < float(older_than_s):
                    continue
            if len([p for p in self.context.pages if not p.is_closed()]) <= 1:
                break
            if _close(page):
                closed += 1
                self._opened.pop(page, None)
        self.closed_count += closed
        return closed

    def close_all_ours(self, keep=None):
        """End-of-run tidy: close every tab we opened except the working one."""
        closed = 0
        for page in list(self._opened):
            if page is keep or page.is_closed():
                continue
            if _close(page):
                closed += 1
            self._opened.pop(page, None)
        for page in list(self._retiring):
            if page is not keep and not page.is_closed() and _close(page):
                closed += 1
        self._retiring = []
        self.closed_count += closed
        return closed
