# Wiza manual-extraction automation

Walks a sheet of LinkedIn profiles, reads the Wiza extension panel on each one,
and writes the email/phone back into the columns you configure. It automates the
clicking and copy-pasting you are doing by hand right now — it does not bypass,
patch, or re-implement anything about Wiza.

**Read [Before you run this](#before-you-run-this) first.** There is real account
risk here and it is worth understanding before you start.

---

## How it works

The script does **not** launch its own browser or log in anywhere. You start
Chrome yourself, sign in to LinkedIn and Wiza exactly as you normally do, and the
script attaches to that already-running window over Chrome's debug protocol.
Every lookup is your real session, your real extension, your real plan.

Per profile it: opens the URL → waits for the Wiza panel → presses the
Find/Reveal button if there is one → waits for an email, a phone, or a
not-found message → writes the row → pauses → moves on.

The panel is found by walking **shadow roots and extension iframes**, not by a
single brittle CSS path, because that is where extensions actually render.

---

## Setup (once)

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then start the browser:

```powershell
powershell -ExecutionPolicy Bypass -File launch-chrome.ps1
```

This opens a **permanent** Chrome profile stored in `.chrome-profile/` next to
the script. In that window, once and only once:

1. Sign in to LinkedIn.
2. Install the Wiza extension and sign in to it.
3. Open any profile, open the Wiza side panel, and **pin it** (the pin icon in
   its header) so it stays open as you navigate. The script reads that panel;
   if it is closed there is nothing to read.

**You never do this again.** The profile keeps both logins across reboots and
across every future run — cookies, the extension, its session, all of it. From
then on your entire routine is: run `launch-chrome.ps1`, then run the script.
The launcher tells you which situation you are in ("first-time setup" vs
"existing profile — already signed in").

It is a separate profile from your everyday Chrome for exactly one reason:
Chrome 136+ refuses `--remote-debugging-port` on the default profile directory.
Your everyday logins do not transfer into it, which is why there is a one-time
sign-in — not a per-run one. Delete `.chrome-profile/` only if you want to start
over from scratch.

---

## Check your setup before spending anything

```powershell
.\.venv\Scripts\python.exe doctor.py
```

This is the fastest way to answer "is everything in place?". It checks, in the
order they will bite you:

1. `config.yaml` parses and every regex in it compiles
2. your sheet exists and the URL column resolves (and how many rows are usable)
3. Chrome is reachable on the debug port
4. a LinkedIn tab is open and signed in, not sitting on a login wall
5. **the Wiza side panel is open** — the one people miss
6. the panel is readable, and which state it is currently in
7. today's volume budget and the working-hours window

It never clicks Reveal, so it costs you nothing. It exits non-zero and lists
blockers if anything is wrong.

---

## Calibrate (once, and again whenever Wiza ships a UI change)

The script cannot know Wiza's DOM in advance, so teach it:

```powershell
.\.venv\Scripts\python.exe calibrate.py https://www.linkedin.com/in/someone/
```

It prints the panel it found, every button it can see, and — after you click
Find **by hand** — exactly which text appeared. Copy the relevant wording into
`config.yaml` under `wiza.find_button.text_patterns` and
`wiza.not_found_patterns`. Emails and phones are detected automatically; you
usually only need to adjust the button and not-found wording.

This costs one lookup from your plan.

---

## Run

Always start with a dry run — no browser, no lookups:

```powershell
.\.venv\Scripts\python.exe wiza_auto.py --dry-run
```

Then a small real run before you trust it with the whole file:

```powershell
.\.venv\Scripts\python.exe wiza_auto.py --limit 5
```

Then the rest:

```powershell
.\.venv\Scripts\python.exe wiza_auto.py
```

### Pointing it at your data

Either edit `config.yaml`, or pass flags:

```powershell
.\.venv\Scripts\python.exe wiza_auto.py --input leads.csv --start-cell B2
```

`--start-cell B2` is the "column and the cell to start from, going vertically
down" model: column B, starting at row 2. The URL column also accepts a header
name (`--url-column "Profile URL"`).

Accepted profile values — all of these work:

| In your sheet | Resolved to |
|---|---|
| `https://www.linkedin.com/in/jane-doe/` | as-is |
| `https://in.linkedin.com/in/jane-doe` | canonical `www` URL |
| `.../in/jane-doe/?originalSubdomain=uk` | tracking params stripped |
| `jane-doe` (bare public ID) | full profile URL |
| `in/jane-doe` | full profile URL |
| `.../company/microsoft/` | rejected, row marked `bad_url` |

### Useful flags

| Flag | Effect |
|---|---|
| `--dry-run` | Resolve rows and print the plan. Never opens a browser. |
| `--limit N` | Stop after N profiles this run. |
| `--in-place` | Write back into the input file instead of a copy. |
| `--no-resume` | Redo rows that already have a status. |
| `--no-click` | Never press a reveal button (if your panel auto-reveals). |
| `--start-row` / `--end-row` | Process a slice. |

---

## Output

By default results go to `out/leads_enriched.csv` — **your source file is never
modified** unless you pass `--in-place`. All original columns are preserved; the
result columns (F–L by default, configurable) are added alongside.

| Status | Meaning | Retried? |
|---|---|---|
| `email+phone` / `email` / `phone` | Found. | settled |
| `not_found` | Wiza answered: "No email found" / "No phone found". | settled |
| `no_match` | "We couldn't find this contact" — no match at all. | settled |
| `bad_url` | The cell was not a personal profile reference. | settled |
| `timeout` | Panel never resolved. | retried |
| `no_panel` | Panel never appeared — side panel closed, or not signed in. | retried |
| `stale_panel` | Panel never showed this person. **Nothing written** — see below. | retried |
| `error` | Load failure or LinkedIn checkpoint. | retried |
| `limit_reached` | Wiza says the quota is gone. **Stops the whole run.** | — |

When Wiza gives a definite answer but has nothing for a field, that cell reads
`not found` rather than sitting empty, so you can tell "Wiza has nothing" from
"we never got there". A `timeout` or `error` leaves the cell **blank** on
purpose — blank means unknown, and those rows are retried automatically.

### The `stale_panel` status, and why it exists

Wiza's panel is Chrome's **side panel**: one persistent panel that repaints as
you move between profiles, a beat behind the page. Read it too early and you get
the *previous* person's email — written onto this person's row, with no error.
That is the worst failure this tool could have, because nothing looks wrong.

So before accepting any result, the runner checks the panel is naming the person
whose page it is on (matching loosely, since Wiza shortens "Asha Patel, Ph.D."
to "Asha Patel"). If the panel never catches up, the row is marked
`stale_panel`, **nothing is written**, and it is retried on the next run.

A handful of these is normal on a slow connection — raise
`timing.result_timeout_ms`. A whole run of them means the panel is not tracking
navigation at all; stop and run `calibrate.py`.

The sheet is rewritten **after every single row**, atomically (temp file +
swap). Ctrl-C or a crash costs you at most the profile in flight, never the
file. Re-running the same command resumes: settled rows are skipped, `error` and
`timeout` rows are retried.

Per-run JSONL logs land in `logs/`.

---

## Before you run this

Three things you should weigh, honestly:

**Wiza's terms almost certainly prohibit this.** An "unlimited manual" tier is
priced on the assumption of human pace. Automating the manual path is the kind
of thing that gets an account terminated — including, potentially, the seats
your teammates rely on. You have paid for the lookups; that is not the same as
having permission to drive the extension with a script.

**LinkedIn's User Agreement prohibits automated profile access outright**, and
LinkedIn is considerably better at detecting it than Wiza is. The realistic
worst case is not a warning — it is a restricted or permanently banned LinkedIn
account for whoever's session is driving this. Use an account you could afford
to lose, not your founder's.

**This is why the pacing defaults are slow.** 12–30 seconds between profiles,
plus a dwell on each page, plus a 90–210 second break every 25, is roughly
60–100 profiles an hour. That is not a limitation to tune away — it is the
entire reason the run might survive.
If you cut it to 2 seconds you will finish a few hundred rows and then lose the
account. Run it on one machine, during working hours, not overnight at volume.

### How many can you actually attempt?

Nobody outside LinkedIn knows the real thresholds, they are not published, and
they vary by account age, connection count, Sales Navigator status and history.
Treat every number below as a community-observed estimate, not a fact:

| Account | Rough daily profile views before risk climbs |
|---|---|
| New / low-activity free account | 30–50 |
| Established free account | 80–100 |
| Sales Navigator seat | 150–300 |

Free accounts also hit a separate **Commercial Use Limit** on search, which
resets on the 1st of each month and is unrelated to profile views.

The defaults here cap you at **100 per day** (`limits.daily_cap`) inside a
**09:00–20:00** window (`limits.working_hours`). The cap is stored in
`logs/daily_state.json` and counted across *all* runs, so restarting the script
does not hand you a fresh budget — that restart-to-reset pattern is precisely
what gets noticed.

The Wiza side is different: your plan says **Unlimited Email / Phone credits**,
so the ceiling there is Wiza's tolerance for automation, not a credit count. If
Wiza ever does report exhaustion, the run stops on `limit_reached` rather than
hammering it.

Ramp slowly. Run 20–30 a day for the first few days, confirm no warnings on
either account, then raise `daily_cap` a little at a time. Spreading across
several LinkedIn accounts spreads the *detection* risk but multiplies the number
of accounts you could lose — and each one needs its own Chrome profile and its
own Wiza seat.

Also worth knowing: the contact data you are collecting is personal data. If any
of these people are in the EU or UK, GDPR applies to how you store and use it
regardless of where it came from, and "Wiza gave it to us" is not a lawful basis
on its own.

None of this is a reason you cannot proceed — it is your org, your paid plan,
your call. It is a reason to start with `--limit 5` and to not point this at
50,000 rows on day one.

---

## Tests

```powershell
.\.venv\Scripts\python.exe run_tests.py          # all three suites
.\.venv\Scripts\python.exe run_tests.py --fast   # skip the browser suite
```

- `test_extract.py` — URL normalisation, email/phone extraction, classification.
  Includes the false-positive cases that matter: "500+ connections",
  "12,345 followers" and date ranges must **not** land in your phone column.
- `test_sheet_flow.py` — column creation, write-back, resume, atomic saves, xlsx.
- `test_wiza_states.py` — every panel state transcribed from real screenshots:
  masked previews (`***@example.com`, `+1 (***) *** ****`) must never be harvested
  as data; "Unlimited Email credits" in the footer must never be read as a
  credit limit; "Forget lead" must never be clicked.
- `test_browser.py` — launches a real Chromium against `tests/mock_profile.html`,
  a fake panel that hides its UI in a shadow root and puts the phone behind a
  second button. Never touches LinkedIn, Wiza, or your plan.
- `test_sidepanel.py` — loads a real unpacked extension and proves the script can
  read a `chrome-extension://` side panel target, that it *cannot* without that
  fix, and that a panel showing the wrong person is refused rather than written.

---

## Troubleshooting

**`Could not attach to Chrome`** — `launch-chrome.ps1` is not running, or Chrome
was started without the debug port. Close all Chrome windows and re-run it.

**Every row comes back `no_panel`** — the side panel is almost certainly closed.
Wiza only exists as a browser target while its panel is open, so open it and pin
it before starting, and do not close it mid-run. `calibrate.py` prints the target
list and says outright whether an EXTENSION target is visible. If it is visible
but no roots are found, widen `wiza.root_hints` or set `wiza.panel_selector`.

**Every row is `timeout`** — the panel is found but the reveal button is not
being matched. Check `wiza.find_button.text_patterns` against the button labels
calibration printed. `logs/panel_row*.txt` holds the text that was on screen
when each timeout happened.

**Phone column has junk in it** — tighten, don't loosen: keep
`wiza.phone_strict: true`. In strict mode a number is only accepted from a
`tel:` link, in `+E164` form, or next to a phone label.

**Run stops with `limit_reached`** — Wiza is reporting the quota is gone. Nothing
in this repo can or should work around that.

**LinkedIn shows a checkpoint** — the run pauses and waits for you to solve it in
the Chrome window, then continues. If you see these repeatedly, you are going too
fast: raise `timing.min_delay_s` / `max_delay_s` and stop for the day.
