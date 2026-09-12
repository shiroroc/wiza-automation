"""Data-integrity tests: nothing a real profile contains may corrupt the CSV.

Real LinkedIn profiles carry emoji, right-to-left names, zero-width joiners,
smart quotes and the occasional stray newline. Any one of those can split a row,
produce mojibake in Excel, or crash a Windows console mid-run.

Also covers the multi-value case: a person with several emails or several phone
numbers must have every one of them written, not just the first.

Run: python test_text_safety.py
"""

import csv
import io
import re
import os
import shutil
import sys
import tempfile

import yaml

import extract as ex
import sheet as sh
import wiza_auto as wa

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {label}")
    else:
        print(f"  ok   {label}")


# ------------------------------------------------------------ cell cleaning
print("\n[characters that break spreadsheets]")
check("newline becomes a space", sh.clean_cell("Jane\r\nDoe"), "Jane Doe")
check("tab becomes a space", sh.clean_cell("A\tB"), "A B")
check("zero-width joiner removed", sh.clean_cell("Jo​hn"), "John")
check("mid-string BOM removed", sh.clean_cell("John﻿ Doe"), "John Doe")
check("bidi override removed",
      sh.clean_cell("‫Ali‬ Khan"), "Ali Khan")
check("non-breaking space normalised", sh.clean_cell("A B"), "A B")
check("runs of space collapsed", sh.clean_cell("A    B"), "A B")
check("None is empty", sh.clean_cell(None), "")
check("control chars stripped", sh.clean_cell("a\x00b\x07c"), "a b c")

print("\n[smart punctuation -> ASCII, so Excel cannot mojibake it]")
check("em dash", sh.clean_cell("Anne—Marie"), "Anne-Marie")
check("en dash", sh.clean_cell("2019–2024"), "2019-2024")
check("curly apostrophe", sh.clean_cell("O’Brien"), "O'Brien")
check("curly quotes", sh.clean_cell("“JD”"), '"JD"')
check("ellipsis", sh.clean_cell("wait…"), "wait...")
check("no em dash survives anywhere",
      "—" in sh.clean_cell("a—b—c"), False)

print("\n[real international text is KEPT, not mangled]")
check("accents kept", sh.clean_cell("José Ángel Núñez"),
      "José Ángel Núñez")
check("CJK kept", sh.clean_cell("张伟"), "张伟")
check("Arabic kept", sh.clean_cell("علي"), "علي")
check("emoji kept", sh.clean_cell("Ali \U0001F680 Khan"), "Ali \U0001F680 Khan")
# NFC: an accent written as two code points must collapse to one.
check("decomposed accent normalised to NFC",
      sh.clean_cell("José"), "José")


# ------------------------------------------------- the CSV must round-trip
print("\n[CSV round-trip with hostile values]")
tmp = tempfile.mkdtemp(prefix="wiza_txt_")
try:
    path = os.path.join(tmp, "out.csv")
    nasty = [
        ("emoji", "Ali \U0001F680 Khan"),
        ("rtl", "‫Ali علي‬ Khan"),
        ("newline", "Jane\r\nDoe"),
        ("comma and quote", 'Smith, "JD" Jr.'),
        ("semicolons", "a@x.com; b@x.com; c@x.com"),
        ("accents", "José Ángel"),
        ("cjk", "张伟"),
        ("emdash", "Anne—Marie O’Brien"),
        ("formula", "=cmd|'/c calc'!A1"),
    ]
    t = sh.Table([["label", "value"]], path, None, 1)
    for i, (lbl, val) in enumerate(nasty, start=2):
        t.set(i, 0, lbl)
        t.set(i, 1, val)
    sh.save(t, path)

    with io.open(path, encoding="utf-8-sig", newline="") as f:
        back = [r for r in csv.reader(f)]

    check("every row survived", len(back), len(nasty) + 1)
    check("every row has exactly 2 columns",
          {len(r) for r in back}, {2})
    labels = [r[0] for r in back[1:]]
    check("labels in order", labels, [n[0] for n in nasty])
    check("commas/quotes preserved inside one cell",
          back[4][1], 'Smith, "JD" Jr.')
    check("semicolon-joined list intact", back[5][1], "a@x.com; b@x.com; c@x.com")
    check("emoji survived the round-trip", back[1][1], "Ali \U0001F680 Khan")
    check("CJK survived the round-trip", back[7][1], "张伟")
    check("no em dash in the file",
          "—" in io.open(path, encoding="utf-8-sig").read(), False)

    # Reloading through our own loader must agree with the raw csv reader.
    reloaded = sh.load(path, None, 1)
    check("loader agrees with csv module", reloaded.get(2, 1), "Ali \U0001F680 Khan")
    check("row count after reload", reloaded.last_row, len(nasty) + 1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# -------------------------------------------- several emails / phones
print("\n[more than one email or phone must all be written]")
cfg = wa.load_config("config.yaml")
WIZA = cfg["wiza"]
CLS = ex.Classifier(WIZA)

MULTI = [{
    "text": ("Wiza\nDana Osei\nFounder\nRiverside\n"
             "Email\nd.osei@riverside.test\nPersonal email\ndana@personal.test\n"
             "Phone numbers\n+1 (555) 214-0613\n+1 (555) 695-0911\n+1 (555) 406-4224"),
    "mailtos": [], "tels": [], "buttons": [],
}]
v = CLS.classify(MULTI)
check("status", v.status, ex.ST_EMAIL_PHONE)
check("both emails captured", len(v.emails), 2)
check("all three phones captured", len(v.phones), 3)
check("primary email is the first", v.email, "d.osei@riverside.test")
check("primary phone is the first", v.phone, "+1 (555) 214-0613")

tmp2 = tempfile.mkdtemp(prefix="wiza_multi_")
try:
    p2 = os.path.join(tmp2, "m.csv")
    t2 = sh.Table([["firstName", "lastName", "url"], ["Dana", "Osei", "dana-demo"]],
                  p2, None, 1)
    cols = wa.resolve_output_columns(t2, cfg)
    wa.write_result(t2, 2, cols, v, "u", "not found")
    sh.save(t2, p2)
    r = sh.load(p2, None, 1)

    check("all_emails joined with ; ", r.get(2, cols["all_emails"]),
          "d.osei@riverside.test; dana@personal.test")
    check("all_phones keeps all three", r.get(2, cols["all_phones"]),
          "+1 (555) 214-0613; +1 (555) 695-0911; +1 (555) 406-4224")
    check("primary email column", r.get(2, cols["email"]), "d.osei@riverside.test")
    check("primary phone column", r.get(2, cols["phone"]), "+1 (555) 214-0613")
    check("nothing lost: 3 phones still separable",
          len(r.get(2, cols["all_phones"]).split("; ")), 3)
    check("original columns untouched", (r.get(2, 0), r.get(2, 1)), ("Dana", "Osei"))
finally:
    shutil.rmtree(tmp2, ignore_errors=True)


# ------------------------------------------- names with odd characters
print("\n[name guard copes with international names]")
check("accented name matches its own panel",
      ex.name_matches("José Ángel Núñez",
                      "Wiza Jose Angel Nunez Acme"), True)
check("emoji in the page name does not break matching",
      ex.name_matches("Ali \U0001F680 Khan", "Wiza Ali Khan Globex"), True)
check("CJK name matches itself",
      ex.name_matches("张伟 李", "Wiza 张伟 李"), True)
check("still rejects a different person",
      ex.name_matches("José Ángel", "Wiza Dana Osei"), False)


# --------------------------------- the overlap guard (the row 25 bug)
# Wiza repaints the panel field by field. Row 25 got row 24's EMAIL while the
# name and phone had already updated to row 25's person, so an exact
# (emails, phones) comparison saw a difference and let it through.
print("\n[repeated-contact guard catches a partial repaint]")


def _overlaps(cur_emails, cur_phones, prev_emails, prev_phones):
    return bool(set(e.lower() for e in cur_emails) & set(e.lower() for e in prev_emails)
                or set(cur_phones) & set(prev_phones))


check("same email, different phone -> caught (the real bug)",
      _overlaps(["b@merakilabs.test"], ["+1 (352) 870-5456"],
                ["b@merakilabs.test"], []), True)
check("identical pair -> caught",
      _overlaps(["b@x.test"], ["+1"], ["b@x.test"], ["+1"]), True)
check("same phone, different email -> caught",
      _overlaps(["c@x.test"], ["+1 555"], ["b@x.test"], ["+1 555"]), True)
check("genuinely different person -> allowed",
      _overlaps(["c@y.test"], ["+1 999"], ["b@x.test"], ["+1 555"]), False)
check("case difference still counts as the same email",
      _overlaps(["B@X.test"], [], ["b@x.test"], []), True)
check("no previous contact -> allowed",
      _overlaps(["c@y.test"], [], [], []), False)


# ------------------------------------------------ guard against text passes
# Three separate bugs came from a repo-wide "smart punctuation to ASCII" pass
# rewriting literal special characters inside source constants:
#   sheet._PUNCT                 em-dash KEY became "-" (3 chars) -> import crash
#   extract.DEFAULT_MASK_CHARS   bullet became "-" -> every phone number with a
#                                hyphen was discarded as a "masked preview"
#   wiza_auto._TITLE_SUFFIX      "[|<endash>-]" became "[|--]" -> invalid range
# These fail loudly if any of them is damaged again.
print("\n[source constants survive text rewrites]")

check("mask chars are the intended four",
      [hex(ord(c)) for c in ex.DEFAULT_MASK_CHARS],
      ["0x2a", "0x2022", "0x25cf", "0xb7"])
check("hyphen is NOT a mask char (real phone numbers contain it)",
      "-" in ex.DEFAULT_MASK_CHARS, False)
check("a hyphenated phone survives",
      ex.find_phones("Mobile: (415) 555-0132", [], True, ["mobile"]),
      ["(415) 555-0132"])

check("punct table keys are single characters",
      all(len(k) == 1 for k in sh._PUNCT), True)
check("em dash still maps to a plain hyphen", sh._PUNCT["—"], "-")

_bad = []
for _mod in (ex, sh, wa):
    for _name in dir(_mod):
        _v = getattr(_mod, _name)
        if isinstance(_v, re.Pattern):
            try:
                re.compile(_v.pattern)
            except re.error as _e:
                _bad.append(f"{_mod.__name__}.{_name}: {_e}")
check("every module-level regex compiles", _bad, [])
check("title suffix still strips ' | LinkedIn'",
      wa._TITLE_SUFFIX.sub("", "Dana Osei | LinkedIn"), "Dana Osei")
check("degree badge still recognised",
      bool(wa._DEGREE_ONLY.match("· 3rd+")), True)
check("a real headline is NOT mistaken for a degree badge",
      bool(wa._DEGREE_ONLY.match("Founder at Riverside")), False)


print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):\n")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("Text-safety and multi-value tests passed.")
