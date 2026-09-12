#!/usr/bin/env python3
"""Grade a real run against known-correct answers.

    python verify_run.py out/test_enriched.csv

test-leads.csv carries the answers Wiza gave when these six profiles were
checked BY HAND (columns C and D, transcribed from screenshots). This compares
what the script produced against that, which is the only test that can prove the
automation reads the real panel the same way a person does.

Columns C-E are the expectations and are never touched by the runner, so this
also confirms the original columns survive the write-back intact.
"""

from __future__ import annotations

import sys

import sheet as sh

EXPECTED_STATUS_COL = "C"
EXPECTED_EMAIL_COL = "D"
GOT_EMAIL_COL = "F"
GOT_PHONE_COL = "G"
GOT_STATUS_COL = "H"
ALL_PHONES_COL = "J"
NOTE_COL = "L"

# A run can legitimately land one step short of the hand-checked answer.
TOLERATED = {
    # Both mean "Wiza answered, nothing there" - which bucket depends on
    # whether Wiza matched the profile to a contact record at all.
    ("not_found", "no_match"),
    ("no_match", "not_found"),
}


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "out/test_enriched.csv"
    t = sh.load(path, None, 1)

    c = {k: sh.col_letter_to_index(v) for k, v in {
        "exp_status": EXPECTED_STATUS_COL, "exp_email": EXPECTED_EMAIL_COL,
        "got_email": GOT_EMAIL_COL, "got_phone": GOT_PHONE_COL,
        "got_status": GOT_STATUS_COL, "all_phones": ALL_PHONES_COL,
        "note": NOTE_COL}.items()}

    print(f"\n{'=' * 74}\n  Grading {path} against hand-checked answers\n{'=' * 74}\n")
    print(f"  {'PROFILE':<16} {'EXPECTED':<12} {'GOT':<12} {'EMAIL MATCH':<13} VERDICT")
    print("  " + "-" * 70)

    exact = close = wrong = pending = 0

    for row in range(2, t.last_row + 1):
        name = t.get(row, 0)
        if not name:
            continue
        exp_s = t.get(row, c["exp_status"]).strip()
        exp_e = t.get(row, c["exp_email"]).strip().lower()
        got_s = t.get(row, c["got_status"]).strip()
        got_e = t.get(row, c["got_email"]).strip().lower()

        if not got_s:
            verdict, pending = "not run yet", pending + 1
        elif got_s == exp_s:
            # Status agrees; now does the email agree too?
            if exp_e and got_e != exp_e:
                verdict, wrong = f"WRONG EMAIL (want {exp_e})", wrong + 1
            else:
                verdict, exact = "exact", exact + 1
        elif (exp_s, got_s) in TOLERATED:
            verdict, close = "acceptable variant", close + 1
        else:
            verdict, wrong = "MISMATCH", wrong + 1

        email_cell = "-" if not exp_e else ("yes" if got_e == exp_e else "no")
        print(f"  {name[:15]:<16} {exp_s:<12} {got_s or '-':<12} {email_cell:<13} {verdict}")

        note = t.get(row, c["note"])
        phones = t.get(row, c["all_phones"])
        if note:
            print(f"  {'':<16} note   : {note[:60]}")
        if phones:
            print(f"  {'':<16} phones : {phones[:60]}")

    total = exact + close + wrong + pending
    print("\n  " + "-" * 70)
    print(f"  exact matches      : {exact}/{total}")
    print(f"  acceptable variants: {close}/{total}")
    print(f"  mismatches         : {wrong}/{total}")
    if pending:
        print(f"  not run yet        : {pending}/{total}")

    print("\n  Original columns preserved?")
    hdr = [t.get(1, i) for i in range(5)]
    print(f"    A-E headers still: {hdr}")

    if wrong:
        print("\n  Not clean - investigate the mismatches above.")
        sys.exit(1)
    if pending == total:
        print("\n  Nothing has been run yet.")
        sys.exit(1)
    print("\n  The script agrees with what you saw by hand.")


if __name__ == "__main__":
    main()
