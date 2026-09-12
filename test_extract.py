"""Tests for the parsing layer. Run: python test_extract.py

No browser needed - these feed synthetic panel snapshots through the same
Classifier the live runner uses.
"""

import sys

import yaml

import extract as ex
import sheet


def load_wiza_cfg():
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["wiza"]


WIZA = load_wiza_cfg()
CLS = ex.Classifier(WIZA)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {label}")
    else:
        print(f"  ok   {label}")


def root(text="", mailtos=None, tels=None):
    return {"text": text, "mailtos": mailtos or [], "tels": tels or [], "buttons": []}


# ---------------------------------------------------------------- URL parsing
print("\n[normalize_linkedin_url]")
CANON = "https://www.linkedin.com/in/satyanadella/"
for label, raw, want in [
    ("full https URL", "https://www.linkedin.com/in/satyanadella/", CANON),
    ("no trailing slash", "https://www.linkedin.com/in/satyanadella", CANON),
    ("tracking params", "https://www.linkedin.com/in/satyanadella/?originalSubdomain=us", CANON),
    ("regional subdomain", "https://in.linkedin.com/in/satyanadella", CANON),
    ("no scheme", "www.linkedin.com/in/satyanadella/", CANON),
    ("protocol-relative", "//linkedin.com/in/satyanadella", CANON),
    ("bare slug", "satyanadella", CANON),
    ("in/ prefix", "in/satyanadella", CANON),
    ("whitespace + quotes", '  "https://www.linkedin.com/in/satyanadella/"  ', CANON),
    ("sales-navigator-ish path", "https://www.linkedin.com/in/satyanadella/detail/contact-info/", CANON),
    ("company URL rejected", "https://www.linkedin.com/company/microsoft/", None),
    ("post URL rejected", "https://www.linkedin.com/feed/update/urn:li:activity:123/", None),
    ("other site rejected", "https://example.com/in/foo", None),
    ("empty", "", None),
    ("blank cell", "   ", None),
]:
    check(label, ex.normalize_linkedin_url(raw), want)

# hyphen/number slugs are extremely common on LinkedIn
check("hyphenated slug", ex.normalize_linkedin_url("jane-doe-4a7b21"),
      "https://www.linkedin.com/in/jane-doe-4a7b21/")

# --------------------------------------------------------------- email finding
print("\n[find_emails]")
exc = ex._compile(WIZA["email_exclude_patterns"])
check("plain text email", ex.find_emails("Email: jane@acme.com", [], exc), ["jane@acme.com"])
check("mailto href wins", ex.find_emails("", ["jane@acme.com"], exc), ["jane@acme.com"])
check("dedupes case-insensitively",
      ex.find_emails("Jane@Acme.com", ["jane@acme.com"], exc), ["jane@acme.com"])
check("excludes linkedin assets",
      ex.find_emails("noreply@linkedin.com and jane@acme.com", [], exc), ["jane@acme.com"])
check("excludes wiza's own domain",
      ex.find_emails("support@wiza.co", [], exc), [])
check("strips trailing punctuation",
      ex.find_emails("Contact: jane@acme.com.", [], exc), ["jane@acme.com"])
check("plus-addressing kept",
      ex.find_emails("jane+leads@acme.co.uk", [], exc), ["jane+leads@acme.co.uk"])
check("no email", ex.find_emails("Nothing here", [], exc), [])

# --------------------------------------------------------------- phone finding
print("\n[find_phones - strict mode]")
LABELS = WIZA["phone_label_patterns"]


def phones(text, tels=()):
    return ex.find_phones(text, tels, True, LABELS)


check("tel: href trusted", phones("", ["+1 415 555 0132"]), ["+1 415 555 0132"])
check("E.164 in text", phones("+14155550132"), ["+14155550132"])
check("labelled number", phones("Mobile: (415) 555-0132"), ["(415) 555-0132"])
check("Direct label", phones("Direct  415-555-0132"), ["415-555-0132"])
check("intl with parens", phones("Phone: +1 (415) 555-0132"), ["+1 (415) 555-0132"])
# The false-positive cases that matter - LinkedIn is full of stray numbers.
check("connection count rejected", phones("500+ connections"), [])
check("follower count rejected", phones("12,345 followers"), [])
check("date range rejected", phones("Jan 2019 - Dec 2023"), [])
check("short number rejected", phones("Phone: 12345"), [])
check("over-long number rejected", phones("Phone: 1234567890123456789"), [])
check("unlabelled bare number rejected", phones("2015 2019 8005551234"), [])

print("\n[find_phones - loose mode]")
check("loose accepts unlabelled",
      ex.find_phones("8005551234", [], False, LABELS), ["8005551234"])

# ---------------------------------------------------------------- classifying
print("\n[Classifier]")
check("no panel yet -> pending", CLS.classify([]).status, ex.PENDING)
check("empty panel -> pending", CLS.classify([root("")]).status, ex.PENDING)
check("spinner -> pending", CLS.classify([root("Searching...")]).status, ex.PENDING)
check("email only", CLS.classify([root("jane@acme.com")]).status, ex.ST_EMAIL)
check("phone only", CLS.classify([root("", tels=["+14155550132"])]).status, ex.ST_PHONE)
check("both", CLS.classify([root("jane@acme.com", tels=["+14155550132"])]).status,
      ex.ST_EMAIL_PHONE)
check("not found", CLS.classify([root("No email found for this profile")]).status,
      ex.ST_NOT_FOUND)
check("couldn't find (apostrophe)",
      CLS.classify([root("We couldn't find a contact")]).status, ex.ST_NOT_FOUND)
check("credit exhaustion -> fatal limit",
      CLS.classify([root("You are out of credits")]).status, ex.ST_LIMIT)
check("upgrade prompt -> fatal limit",
      CLS.classify([root("Please upgrade your plan to continue")]).status, ex.ST_LIMIT)
check("data beats a stale spinner",
      CLS.classify([root("Searching...\njane@acme.com")]).status, ex.ST_EMAIL)
check("data beats not-found text",
      CLS.classify([root("No phone found\njane@acme.com")]).status, ex.ST_EMAIL)
check("merges across frames",
      CLS.classify([root("jane@acme.com"), root("", tels=["+14155550132"])]).status,
      ex.ST_EMAIL_PHONE)

v = CLS.classify([root("jane@acme.com\nsecond@acme.com")])
check("first email is primary", v.email, "jane@acme.com")
check("all emails retained", v.emails, ["jane@acme.com", "second@acme.com"])

# ------------------------------------------------------------------- addressing
print("\n[sheet addressing]")
check("A->0", sheet.col_letter_to_index("A"), 0)
check("AA->26", sheet.col_letter_to_index("AA"), 26)
check("roundtrip 0..100",
      all(sheet.col_letter_to_index(sheet.col_index_to_letter(i)) == i for i in range(101)),
      True)
check("cell ref", sheet.parse_cell_ref("C7"), ("C", 7))

t = sheet.Table([["Name", "Profile URL"], ["Jane", "jane-doe"]], "x.csv", None, 1)
check("resolve by header", t.resolve_column("Profile URL"), 1)
check("resolve by letter", t.resolve_column("B"), 1)
check("header match is case-insensitive", t.resolve_column("profile url"), 1)
t.set(2, 5, "hello")
check("set past width grows row", t.get(2, 5), "hello")
check("grown rows are rectangular", len(t.rows[0]), 6)
check("last_row", t.last_row, 2)

# ------------------------------------------------------------------------ done
print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):\n")
    for f in FAILURES:
        print(" - " + f)
    sys.exit(1)
print("All tests passed.")
