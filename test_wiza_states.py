"""Tests built from the REAL Wiza panel, transcribed from screenshots.

Each block reproduces one observed panel state verbatim - including the footer
("Unlimited Email credits") that appears on every screen and the masked
previews shown before you press Reveal. Run: python test_wiza_states.py
"""

import sys

import yaml

import extract as ex
import wiza_auto as wa

WIZA = yaml.safe_load(open("config.yaml", encoding="utf-8"))["wiza"]
CLS = ex.Classifier(WIZA)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {label}  got={got!r} want={want!r}")
    else:
        print(f"  ok   {label}")


FOOTER = "Save to list\nSelect list\nUnlimited\nEmail credits\nUnlimited\nPhone credits"
INTEGRATION = "Connect an Integration\nConnect Integration\nNeed help finding an integration? Learn More"


def panel(body, buttons=()):
    text = f"Wiza\nProspect\nContacts\nHistory\n{body}\n{INTEGRATION}\n{FOOTER}"
    return [{
        "text": text,
        "mailtos": [],
        "tels": [],
        "buttons": [{"token": f"t{i}", "tag": "button", "text": b,
                     "disabled": False, "visible": True}
                    for i, b in enumerate(buttons)],
    }]


# --------------------------------------------------------------- image 1/3/8
print("\n[image 1, 3, 8 - panel answered, nothing for either field]")
NOTHING = panel("Noor Jain\nAI/ LLM specialist (Physics)\nNorthwind Inc.\n"
                "Email\nNo email found\nPhone number\nNo phone found")
v = CLS.classify(NOTHING)
check("status", v.status, ex.ST_NOT_FOUND)
check("no phantom email", v.emails, [])
check("no phantom phone", v.phones, [])
check("is a definite answer", v.definite, True)

# ----------------------------------------------------------------- image 2
print("\n[image 2 - profile matched no contact at all]")
NOMATCH = panel("We couldn't find this contact\n"
                "We weren't able to match this profile to a contact, so there's "
                "no contact information to reveal right now.\n"
                "Try opening another profile or check back later.")
v = CLS.classify(NOMATCH)
check("status", v.status, ex.ST_NO_MATCH)
check("flagged as no_match (panel shows no name)", v.no_match, True)
check("is a definite answer", v.definite, True)

# --------------------------------------------------------------- image 4/7
print("\n[image 4, 7 - UNREVEALED: masked previews, Reveal button present]")
MASKED = panel("Asha Patel\nPhysics Artificial Intelligence Analyst\nGlobex\n"
               "Work email\n***@example.com\n"
               "Phone number\n+1 (***) *** ****\n"
               "Personal email\n***@***.com\n"
               "Reveal contact info\nUnlimited reveals on your plan",
               buttons=["Reveal contact info", "Connect Integration",
                        "Unlimited reveals on your plan"])
v = CLS.classify(MASKED)
check("masked email NOT harvested", v.emails, [])
check("masked phone NOT harvested", v.phones, [])
check("stays pending so the reveal gets clicked", v.status, ex.PENDING)

fb = WIZA["find_button"]
choice = wa.pick_find_button([(None, MASKED[0])], fb, set())
check("picks the Reveal button", choice[1]["text"], "Reveal contact info")
check("skips the decoy 'reveals on your plan' text",
      choice[1]["text"] != "Unlimited reveals on your plan", True)

# The destructive one. Image 5 shows "Forget lead" in the panel.
DANGER = panel("Asha Patel", buttons=["Forget lead", "Connect Integration",
                                      "Save to list", "Learn More"])
check("never clicks anything when only dangerous buttons exist",
      wa.pick_find_button([(None, DANGER[0])], fb, set()), None)

# ----------------------------------------------------------------- image 5
print("\n[image 5 - loading state after pressing Reveal]")
LOADING = panel("Asha Patel\nPhysics Specialist\nGlobex\n"
                "Finding contact data...\nHang tight! It's coming in a few seconds\n"
                "Globex\nInternet Marketplace Platforms\nHeadcount\n163,845\n"
                "Founded\n2009\nLocation\nSan francisco, california, united states\n"
                "Forget lead",
                buttons=["Forget lead"])
v = CLS.classify(LOADING)
check("keeps waiting", v.status, ex.PENDING)
check("note says in progress", v.note, "in progress")
check("headcount 163,845 not read as a phone", v.phones, [])
check("founded year 2009 not read as a phone", v.phones, [])

# ----------------------------------------------------------------- image 6
print("\n[image 6 - email found, phone explicitly not found]")
PARTIAL = panel("Asha Patel, Ph.D.\nPhysics Specialist\nGlobex\n"
                "Email\na.patel@globex.test\nPhone number\nNo phone found")
v = CLS.classify(PARTIAL)
check("status", v.status, ex.ST_EMAIL)
check("email", v.email, "a.patel@globex.test")
check("phone stays empty", v.phone, "")
check("is a definite answer", v.definite, True)

# ------------------------------------------- already-revealed, multiple phones
print("\n[Jordan Rivera panel - already revealed, THREE phone numbers]")
REVEALED = panel("Jordan Rivera\nFounder\nRiverside Foundation\n"
              "Email\nj.rivera@riverside.test\n"
              "Phone numbers\n"
              "+1 (555) 214-0613\n+1 (555) 695-0911\n+1 (555) 406-4224",
              buttons=["Connect Integration"])
v = CLS.classify(REVEALED)
check("status", v.status, ex.ST_EMAIL_PHONE)
check("email", v.email, "j.rivera@riverside.test")
check("primary phone", v.phone, "+1 (555) 214-0613")
check("ALL three phones kept", v.phones,
      ["+1 (555) 214-0613", "+1 (555) 695-0911", "+1 (555) 406-4224"])
check("no reveal button needed here",
      wa.pick_find_button([(None, REVEALED[0])], WIZA["find_button"], set()), None)
check("name guard passes", ex.name_matches("Jordan Rivera", CLS.panel_text(REVEALED)), True)

# The page behind it is full of numbers that must not become phone numbers.
check("412,000 followers never becomes a phone",
      ex.find_phones("412,000 followers", [], True, WIZA["phone_label_patterns"]), [])

# ---------------------------------------------- the footer must never be fatal
print("\n[footer 'Unlimited ... credits' must not trigger the fatal stop]")
for label, body in [("not-found screen", NOTHING), ("partial screen", PARTIAL),
                    ("masked screen", MASKED), ("loading screen", LOADING)]:
    check(f"{label} is not limit_reached",
          CLS.classify(body).status == ex.ST_LIMIT, False)
check("a real exhaustion message IS fatal",
      CLS.classify(panel("You are out of credits")).status, ex.ST_LIMIT)

# ------------------------------------------------ the stale-panel guard
print("\n[stale panel guard - the wrong-row bug]")
# Panel still showing the PREVIOUS person while we are on a new profile.
check("panel showing Noor does not pass for Asha",
      ex.name_matches("Asha Patel, Ph.D.", CLS.panel_text(NOTHING)), False)
check("panel showing Asha passes for Asha",
      ex.name_matches("Asha Patel, Ph.D.", CLS.panel_text(PARTIAL)), True)
# Wiza abbreviates: page says "Asha Patel, Ph.D.", panel said "Asha Patel".
check("tolerates Wiza's shortened name (image 4 vs 6)",
      ex.name_matches("Asha Patel, Ph.D.", "Wiza Asha Patel Globex"), True)
check("honorifics ignored", ex.name_matches("Dr. Nolan", "Wiza Dr. Nolan Turing"), True)
check("pronouns ignored", ex.name_matches("Dev Yadav He/Him", "Dev Yadav Driftworks"), True)
check("one shared first name is not enough",
      ex.name_matches("Dev Yadav", "Wiza Dev Sharma Acme"), False)
check("different person rejected",
      ex.name_matches("Sam Farhan", "Wiza Noor Jain Northwind"), False)
check("tokenises accents", ex.name_tokens("José Ángel Núñez"), ["jose", "angel", "nunez"])

# ------------------------------------------------------ what lands in the sheet
print("\n[what actually gets written to the sheet]")
import sheet as sh

cfg = wa.load_config("config.yaml")
tbl = sh.Table([["Name", "Profile URL"], ["Asha", "asha-patel-demo"]], "t.csv", None, 1)
cols = wa.resolve_output_columns(tbl, cfg)

wa.write_result(tbl, 2, cols, CLS.classify(PARTIAL), "u", "not found")
check("email cell", tbl.get(2, cols["email"]), "a.patel@globex.test")
check("phone cell says not found", tbl.get(2, cols["phone"]), "not found")

wa.write_result(tbl, 2, cols, CLS.classify(NOTHING), "u", "not found")
check("both cells say not found when neither exists",
      (tbl.get(2, cols["email"]), tbl.get(2, cols["phone"])),
      ("not found", "not found"))

wa.write_result(tbl, 2, cols, ex.Verdict(ex.ST_TIMEOUT, note="x"), "u", "not found")
check("a timeout leaves cells BLANK, not 'not found'",
      (tbl.get(2, cols["email"]), tbl.get(2, cols["phone"])), ("", ""))
check("timeout is retried on the next run",
      ex.ST_TIMEOUT in wa.FINAL_STATUSES, False)
check("stale_panel is retried on the next run",
      ex.ST_STALE_PANEL in wa.FINAL_STATUSES, False)
check("no_match is settled", ex.ST_NO_MATCH in wa.FINAL_STATUSES, True)

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):\n")
    for f in FAILURES:
        print(" - " + f)
    sys.exit(1)
print("All real-panel state tests passed.")
