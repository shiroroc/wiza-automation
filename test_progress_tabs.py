"""Tests for the ETA tracker and tab lifecycle.

The tab tests use a fake context so they can assert the safety rules cheaply -
the rule that matters is that nothing except an idle LinkedIn PROFILE tab is
ever closed. Run: python test_progress_tabs.py
"""

import sys

import yaml

import progress as prog
import tabs as tb

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {label}  got={got!r} want={want!r}")
    else:
        print(f"  ok   {label}")


CFG = yaml.safe_load(open("config.yaml", encoding="utf-8"))


# ----------------------------------------------------------------- durations
print("\n[duration formatting]")
check("seconds", prog.fmt_duration(45), "45s")
check("minutes", prog.fmt_duration(14 * 60), "14m")
check("hours", prog.fmt_duration(3 * 3600 + 10 * 60), "3h 10m")
check("days", prog.fmt_duration(26 * 3600), "1d 2h")
check("unknown", prog.fmt_duration(None), "?")
check("negative floors at zero", prog.fmt_duration(-5), "0s")


# ------------------------------------------------------------ a-priori estimate
print("\n[estimate from config alone]")
per = prog.estimate_seconds_per_profile(CFG)
# 12-30s gap (21 avg) + 2-6s dwell (4) + ~9s work + 150/25 amortised pause = ~40s
check("plausible seconds per profile", 30 < per < 55, True)
rate = 3600 / per
check("matches the documented 60-100/hour claim", 60 <= rate <= 100, True)
print(f"       -> {per:.1f}s per profile, {rate:.0f}/hour")


# ------------------------------------------------------------------- progress
print("\n[progress + ETA]")


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


clk = FakeClock()
p = prog.Progress(100, CFG, clock=clk)
check("estimates before any row completes", p.eta_seconds > 0, True)
check("nothing done yet", p.done, 0)
check("all rows remaining", p.remaining_rows, 100)

for _ in range(10):
    clk.advance(40)
    p.tick()
check("counted", p.done, 10)
check("remaining", p.remaining_rows, 90)
check("measured 40s per profile", round(p.per_profile), 40)
check("eta = 90 x 40s", round(p.eta_seconds), 3600)
check("percent", round(p.percent), 10)
check("rate per hour", round(p.rate_per_hour), 90)

# A slow patch must move the estimate, not be ignored.
for _ in range(10):
    clk.advance(80)
    p.tick()
check("slowdown raises the per-profile figure", p.per_profile > 40, True)
check("and pushes the ETA out", p.eta_seconds > 3600 * 0.8, True)

bar = p.bar(20)
check("bar is the requested width", len(bar), 20)
check("bar reflects 20% done", bar.startswith("####"), True)
summary = p.summary()
check("summary mentions the count", "20/100" in summary, True)
check("summary gives a finish time", "finishes ~" in summary, True)

done = prog.Progress(3, CFG, clock=clk)
for _ in range(3):
    clk.advance(10)
    done.tick()
check("finished run reports no ETA", "left" in done.summary(), False)
check("zero-row run does not divide by zero", prog.Progress(0, CFG).percent, 100.0)


# ----------------------------------------------------------------------- tabs
print("\n[tab safety rules]")


class FakePage:
    def __init__(self, url):
        self.url = url
        self._closed = False

    def is_closed(self):
        return self._closed

    def close(self):
        self._closed = True


class FakeContext:
    def __init__(self, urls):
        self.pages = [FakePage(u) for u in urls]

    def new_page(self):
        p = FakePage("about:blank")
        self.pages.append(p)
        return p


check("recognises a profile tab", tb.is_profile_tab(FakePage("https://www.linkedin.com/in/x/")), True)
check("regional subdomain too", tb.is_profile_tab(FakePage("https://in.linkedin.com/in/x")), True)
check("a company page is not a profile tab",
      tb.is_profile_tab(FakePage("https://www.linkedin.com/company/microsoft/")), False)
check("the LinkedIn feed is not a profile tab",
      tb.is_profile_tab(FakePage("https://www.linkedin.com/feed/")), False)
check("the Wiza dashboard is not a profile tab",
      tb.is_profile_tab(FakePage("https://wiza.co/dashboard")), False)

# The user's real tab bar, from their screenshot.
REAL = [
    "https://docs.google.com/spreadsheets/d/abc",       # their sheet
    "https://wiza.co/settings",                         # Wiza dashboard
    "chrome-extension://abcdef/sidepanel.html",         # the side panel
    "https://www.linkedin.com/in/jordan-rivera-demo/",      # working tab
    "https://www.linkedin.com/in/noor-jain-demo/",       # stale profile
    "https://www.linkedin.com/in/dev-yadav-demo/",         # stale profile
    "https://discord.com/channels/123",                 # unrelated
]
ctx = FakeContext(REAL)
cfg = {"browser": {"tab_strategy": "new_tab"}}
keeper = tb.TabKeeper(ctx, cfg)
working = ctx.pages[3]

closed = keeper.close_stale_profile_tabs(working)
check("closed exactly the two stale profile tabs", closed, 2)
check("the working tab survives", working.is_closed(), False)
check("the spreadsheet survives", ctx.pages[0].is_closed(), False)
check("the Wiza dashboard survives", ctx.pages[1].is_closed(), False)
check("the side panel survives", ctx.pages[2].is_closed(), False)
check("Discord survives", ctx.pages[6].is_closed(), False)

print("\n[never close the last tab]")
solo = FakeContext(["https://www.linkedin.com/in/only-one/"])
k2 = tb.TabKeeper(solo, cfg)
check("a lone profile tab is left alone", k2.close_stale_profile_tabs(None), 0)
check("still open", solo.pages[0].is_closed(), False)

print("\n[one tab per profile, previous retired]")
ctx2 = FakeContext(["https://www.linkedin.com/in/first/"])
k3 = tb.TabKeeper(ctx2, cfg)
cur = ctx2.pages[0]
t1 = k3.next_tab(cur)
check("opened a new tab", t1 is not cur, True)
check("the previous one is NOT closed yet", cur.is_closed(), False)
t1.url = "https://www.linkedin.com/in/second/"
k3.retire_previous(t1)
check("previous closed once the new tab took over", cur.is_closed(), True)
check("current tab still open", t1.is_closed(), False)

print("\n[leak sweep]")
ctx3 = FakeContext(["https://www.linkedin.com/in/a/"])
k4 = tb.TabKeeper(ctx3, cfg)
cur = ctx3.pages[0]
made = [k4.next_tab(cur) for _ in range(6)]
for i, m in enumerate(made):
    m.url = f"https://www.linkedin.com/in/p{i}/"
live_before = len([p for p in ctx3.pages if not p.is_closed()])
k4.reap_leaked(made[-1], max_open=3)
live_after = len([p for p in ctx3.pages if not p.is_closed()])
check("sweep reduced the tab count", live_after < live_before, True)
check("the tab in use survived", made[-1].is_closed(), False)

print("\n[reuse mode opens nothing]")
k5 = tb.TabKeeper(FakeContext(["https://www.linkedin.com/in/a/"]),
                  {"browser": {"tab_strategy": "reuse"}})
same = FakePage("https://www.linkedin.com/in/a/")
check("hands back the same tab", k5.next_tab(same) is same, True)

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):\n")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("Progress + tab tests passed.")
