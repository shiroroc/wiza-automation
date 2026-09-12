"""Spreadsheet I/O: CSV and XLSX, addressed the way a human addresses a sheet.

Rows are 1-based (row 1 is the first row of the file, matching Excel/Sheets).
Columns are addressed by letter ("B") or by exact header name ("Profile URL").
Writes are atomic: a temp file is fully written, then swapped into place, so a
crash or Ctrl-C mid-run can never leave you with a half-written sheet.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from typing import List, Optional


def col_letter_to_index(letter: str) -> int:
    """'A' -> 0, 'B' -> 1, 'Z' -> 25, 'AA' -> 26."""
    letter = letter.strip().upper()
    if not letter or not letter.isalpha():
        raise ValueError(f"Not a column letter: {letter!r}")
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def col_index_to_letter(index: int) -> str:
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA'."""
    if index < 0:
        raise ValueError("Column index must be >= 0")
    out = ""
    n = index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def parse_cell_ref(ref: str):
    """'B2' -> ('B', 2). Used by --start-cell."""
    ref = ref.strip().upper()
    letters = "".join(c for c in ref if c.isalpha())
    digits = "".join(c for c in ref if c.isdigit())
    if not letters or not digits:
        raise ValueError(f"Not a cell reference like 'B2': {ref!r}")
    return letters, int(digits)


class Table:
    """A whole sheet held in memory as a rectangular list of strings."""

    def __init__(self, rows: List[List[str]], path: str, sheet: Optional[str] = None,
                 header_row: Optional[int] = 1):
        self.rows = rows
        self.path = path
        self.sheet = sheet
        self.header_row = header_row

    # -- addressing ---------------------------------------------------------

    @property
    def headers(self) -> List[str]:
        if self.header_row and len(self.rows) >= self.header_row:
            return [str(c).strip() for c in self.rows[self.header_row - 1]]
        return []

    def resolve_column(self, ref: str, create: bool = False) -> int:
        """Resolve a letter or a header name to a 0-based column index."""
        ref = str(ref).strip()
        # A pure-alpha short token is ambiguous; prefer an exact header match,
        # fall back to treating it as a column letter.
        headers = self.headers
        for i, h in enumerate(headers):
            if h and h.lower() == ref.lower():
                return i
        if ref.isalpha() and len(ref) <= 3:
            return col_letter_to_index(ref)
        if create:
            idx = self.width
            self.ensure_width(idx + 1)
            if self.header_row:
                self.rows[self.header_row - 1][idx] = ref
            return idx
        raise KeyError(
            f"Column {ref!r} is neither a column letter nor a header in {self.path}. "
            f"Headers are: {headers}"
        )

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def ensure_width(self, width: int) -> None:
        for r in self.rows:
            if len(r) < width:
                r.extend([""] * (width - len(r)))

    def ensure_rows(self, count: int) -> None:
        w = self.width
        while len(self.rows) < count:
            self.rows.append([""] * w)

    # -- cell access (row is 1-based) ---------------------------------------

    def get(self, row: int, col: int) -> str:
        if row < 1 or row > len(self.rows):
            return ""
        r = self.rows[row - 1]
        return str(r[col]).strip() if col < len(r) else ""

    def set(self, row: int, col: int, value) -> None:
        self.ensure_rows(row)
        self.ensure_width(col + 1)
        self.rows[row - 1][col] = "" if value is None else str(value)

    def set_header(self, col: int, name: str) -> None:
        """Write a header name only if that header cell is currently empty."""
        if not self.header_row or not name:
            return
        self.ensure_rows(self.header_row)
        self.ensure_width(col + 1)
        if not str(self.rows[self.header_row - 1][col]).strip():
            self.rows[self.header_row - 1][col] = name

    @property
    def last_row(self) -> int:
        """Last row that has any content at all."""
        for i in range(len(self.rows) - 1, -1, -1):
            if any(str(c).strip() for c in self.rows[i]):
                return i + 1
        return 0


# -- load / save ------------------------------------------------------------


def load(path: str, sheet: Optional[str] = None, header_row: Optional[int] = 1) -> Table:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return _load_xlsx(path, sheet, header_row)
    return _load_csv(path, header_row)


def _load_csv(path: str, header_row) -> Table:
    # utf-8-sig transparently strips the BOM Excel likes to add.
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with io.open(path, "r", encoding=enc, newline="") as f:
                rows = [list(r) for r in csv.reader(f)]
            break
        except UnicodeDecodeError:
            continue
    else:
        raise IOError(f"Could not decode {path} with any known encoding")
    return Table(rows, path, None, header_row)


def _load_xlsx(path: str, sheet, header_row) -> Table:
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows = [["" if c is None else str(c) for c in row]
            for row in ws.iter_rows(values_only=True)]
    return Table(rows, path, ws.title, header_row)


def save(table: Table, path: Optional[str] = None) -> str:
    """Atomically write the table. Returns the path written."""
    path = path or table.path
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    ext = os.path.splitext(path)[1].lower()

    fd, tmp = tempfile.mkstemp(suffix=ext or ".csv", dir=parent)
    os.close(fd)
    try:
        if ext in (".xlsx", ".xlsm"):
            _save_xlsx(table, tmp)
        else:
            _save_csv(table, tmp)
        os.replace(tmp, path)   # atomic on Windows and POSIX
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def _save_csv(table: Table, path: str) -> None:
    with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(table.rows)


def _save_xlsx(table: Table, path: str) -> None:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    if table.sheet:
        ws.title = table.sheet
    for row in table.rows:
        ws.append(list(row))
    wb.save(path)
