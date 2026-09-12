"""Tests for the write-back half: columns, resume, atomic saves, xlsx.

Run: python test_sheet_flow.py   (no browser involved)
"""

import csv
import io
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
        print(f"  FAIL {label}  got={got!r} want={want!r}")
    else:
        print(f"  ok   {label}")


def base_cfg(tmp, in_path, out_path):
    cfg = wa.load_config("config.yaml")
    cfg["input"].update({"path": in_path, "url_column": "B", "start_row": 2,
                         "end_row": None, "header_row": 1})
    cfg["output"].update({"path": out_path, "in_place": False})
    return cfg


def make_csv(path, rows):
    with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(rows)


def read_csv(path):
    with io.open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.reader(f)]


ROWS = [
    ["Name", "Profile URL", "Company"],
    ["Jane", "jane-doe", "Acme"],
    ["Raj", "raj-patel", "Globex"],
    ["Mei", "mei-chen", "Initech"],
]

tmp = tempfile.mkdtemp(prefix="wiza_test_")
try:
    in_path = os.path.join(tmp, "leads.csv")
    out_path = os.path.join(tmp, "out.csv")
    make_csv(in_path, ROWS)
    cfg = base_cfg(tmp, in_path, out_path)

    # ------------------------------------------------------- column creation
    print("\n[output columns]")
    table = sh.load(in_path, None, 1)
    cols = wa.resolve_output_columns(table, cfg)
    # Derived from config, not hard-coded: remapping output columns is a
    # supported thing to do and must not break the test suite.
    want_email = sh.col_letter_to_index(cfg["output"]["columns"]["email"])
    want_note = sh.col_letter_to_index(cfg["output"]["columns"]["note"])
    check("email lands in its configured column", cols["email"], want_email)
    check("note lands in its configured column", cols["note"], want_note)
    check("header written", table.get(1, cols["email"]), "Wiza Email")
    check("source headers untouched", table.get(1, 1), "Profile URL")
    check("gap column D stays empty", table.get(1, sh.col_letter_to_index("D")), "")

    # ------------------------------------------------------- writing results
    print("\n[write_result]")
    v = ex.Verdict(ex.ST_EMAIL_PHONE, ["a@x.com", "b@x.com"], ["+14155550132"])
    wa.write_result(table, 2, cols, v, "url")
    check("primary email", table.get(2, cols["email"]), "a@x.com")
    check("primary phone", table.get(2, cols["phone"]), "+14155550132")
    check("status", table.get(2, cols["status"]), "email+phone")
    check("all emails joined", table.get(2, cols["all_emails"]), "a@x.com; b@x.com")
    check("timestamp present", len(table.get(2, cols["checked_at"])) > 0, True)
    check("other rows untouched", table.get(3, cols["email"]), "")

    nf = ex.Verdict(ex.ST_NOT_FOUND, note="no email found")
    wa.write_result(table, 3, cols, nf, "url")
    check("not_found still writes a row", table.get(3, cols["status"]), "not_found")
    check("not_found note kept", table.get(3, cols["note"]), "no email found")
    # A definite "Wiza has nothing" now says so in the cell, rather than
    # leaving a blank that reads as "we never checked".
    check("not_found writes 'not found' into the email cell",
          table.get(3, cols["email"]), "not found")
    blank = ex.Verdict(ex.ST_TIMEOUT, note="timed out")
    wa.write_result(table, 4, cols, blank, "url")
    check("a timeout leaves the cell blank instead",
          table.get(4, cols["email"]), "")

    # --------------------------------------------------------- save + reload
    print("\n[save / reload]")
    sh.save(table, out_path)
    check("output file exists", os.path.exists(out_path), True)
    back = read_csv(out_path)
    check("row count preserved", len(back), 4)
    check("every row is rectangular", len({len(r) for r in back}), 1)
    check("original data preserved", back[1][0], "Jane")
    check("result readable after reload", back[1][want_email], "a@x.com")

    reloaded = sh.load(out_path, None, 1)
    check("reload resolves by new header", reloaded.resolve_column("Wiza Email"),
          want_email)

    # ---------------------------------------------------------------- resume
    print("\n[resume]")
    # Row 2 settled, row 3 settled, row 4 untouched -> only row 4 is left.
    plan = wa.build_plan(reloaded, cfg)
    status_col = wa.resolve_output_columns(reloaded, cfg)["status"]
    todo = [r for r in plan
            if reloaded.get(r[0], status_col) not in wa.FINAL_STATUSES
            or not reloaded.get(r[0], status_col)]
    check("only the unprocessed row remains", [r[0] for r in todo], [4])

    # An error row must be retried, not skipped.
    reloaded.set(2, status_col, ex.ST_ERROR)
    todo2 = [r for r in wa.build_plan(reloaded, cfg)
             if reloaded.get(r[0], status_col) not in wa.FINAL_STATUSES]
    check("error rows are retried", 2 in [r[0] for r in todo2], True)
    check("timeout is retryable", ex.ST_TIMEOUT in wa.FINAL_STATUSES, False)
    check("not_found is final", ex.ST_NOT_FOUND in wa.FINAL_STATUSES, True)

    # ------------------------------------------------------- source safety
    print("\n[source file safety]")
    check("input file unchanged", read_csv(in_path), ROWS)

    # -------------------------------------------------------- atomic writes
    print("\n[atomic save]")
    before = read_csv(out_path)
    try:
        bad = sh.Table([["ok"]], out_path, None, 1)
        bad.rows = None          # force _save_csv to blow up mid-write
        sh.save(bad, out_path)
    except Exception:
        pass
    check("failed write left the old file intact", read_csv(out_path), before)
    leftovers = [f for f in os.listdir(tmp) if f.startswith("tmp")]
    check("no temp files left behind", leftovers, [])

    # ---------------------------------------------------------------- xlsx
    print("\n[xlsx]")
    xlsx_in = os.path.join(tmp, "leads.xlsx")
    t2 = sh.Table([list(r) for r in ROWS], xlsx_in, "Sheet1", 1)
    sh.save(t2, xlsx_in)
    t3 = sh.load(xlsx_in, None, 1)
    check("xlsx roundtrip", t3.get(2, 1), "jane-doe")
    check("xlsx headers", t3.resolve_column("Profile URL"), 1)
    c2 = wa.resolve_output_columns(t3, cfg)
    wa.write_result(t3, 2, c2, v, "url")
    sh.save(t3, xlsx_in)
    check("xlsx result persisted", sh.load(xlsx_in, None, 1).get(2, c2["email"]), "a@x.com")

    # --------------------------------------------------- start row / range
    print("\n[row ranges]")
    cfg2 = base_cfg(tmp, in_path, out_path)
    cfg2["input"]["start_row"] = 3
    t4 = sh.load(in_path, None, 1)
    check("start_row honoured", [r[0] for r in wa.build_plan(t4, cfg2)], [3, 4])
    cfg2["input"]["end_row"] = 3
    check("end_row honoured", [r[0] for r in wa.build_plan(t4, cfg2)], [3])

    cfg3 = base_cfg(tmp, in_path, out_path)
    cfg3["input"]["url_column"] = "Profile URL"
    check("url column by header name",
          [r[2] for r in wa.build_plan(sh.load(in_path, None, 1), cfg3)][0],
          "https://www.linkedin.com/in/jane-doe/")

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):\n")
    for f in FAILURES:
        print(" - " + f)
    sys.exit(1)
print("Sheet-flow tests passed.")
