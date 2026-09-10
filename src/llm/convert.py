# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Converts a 10-K statement R-file (standalone .htm) into LLM-ready JSON.

The pipeline is fully automatic: given a CIK and a statement (is | bs | cf),
the module locates the right pre-split R-file under data/10k/, extracts one
flat record per data row, and writes compact JSON into data/converted.

Record contract:

    "id"           : "34088_bs_2023-01-01_01",
    "stmt"         : "bs",
    "order"        : 1,
    "level"        : 0,
    "label"        : "Current assets",
    "tag"          : "defref_us-gaap_AssetsCurrentAbstract",
    "has_value"    : true

Meaning:

    id        -- "{cik}_{stmt}_{date_YYYY-MM-DD}_{order:02d}", stable across runs for the same file
    stmt      -- the statement name (is | bs | cf)
    order     -- 1-based position among data rows (headers excluded)
    level     -- grouping depth: 0 = subtotal / heading (bold row),
                 1 = ordinary line item (no indentation exists in these
                 files' markup, so bold is the only reliable signal)
    label     -- the line's text, verbatim (whitespace collapsed, footnote
                 markers such as [1] preserved)
    tag       -- the concept reference in the row's "Details" anchor,
                 kept exactly (defref_us-gaap_*, defref_cisco_*, ...)
    has_value -- whether the row carried numeric period values

Numbers are not parsed into the records: classification is driven by
label + hierarchy + tag, and a validator does the arithmetic later.

Usage:
  
    uv run src/llm/convert.py 0001652044 is                     # converts only the latest Income Statement
    uv run src/llm/convert.py 0001652044 bs --date 2018-12-31   # converts a specific historical year's Balance Sheet
    uv run src/llm/convert.py 0001652044 cf --all               # converts all historical Cash Flow statements (10 years)
    uv run src/llm/convert.py 0001652044 --all                  # converts ALL 10-year historical statements (is + bs + cf = 30 files)
    uv run src/llm/convert.py 789019 cf --out /tmp/...          # explicit out
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup


# =============================================================================
# Paths and constants
# =============================================================================

LLM_DIR = Path(__file__).resolve().parent
DATA_DIR = LLM_DIR.parents[1] / "data" / "10k"
OUTPUT_DIR = LLM_DIR.parents[1] / "data" / "converted"
STATEMENTS = ("is", "bs", "cf")  # valid statement names


# -----------------------------------------------------------------------------
# Regex bundle to extract values from .htm files
# -----------------------------------------------------------------------------

# The concept reference a row's "Details" anchor points at. It is the
# only machine-stable identity a row has; it is what we audit.
_TAG_RE = re.compile(r"Show\.showAR\(\s*this\s*,\s*'([^']+)'")

# A cell that is (part of) a number: optional currency sign, digits with
# thousands separators and / or decimals, optional parentheses or percent.
_VALUE_RE = re.compile(
    r"""
    \s*  # optional whitespaces
    [-+]?  # optional sign
    \s*
    [$£€]?  # optional currency sign
    \s*
    [-+]?
    \s*
    (?:  # grouping
        \(  # opening parenthesis
            \s*
            [$£€]?
            \s*
            \d  # a single digit
            [\d,.\s]*  # any number of digits, separators, or whitespaces
            %?  # optional single percentage sign
            \s*
        \)  # closing parenthesis
    |  # or
        \d
        [\d,.\s]*
        %?
    )
    \s*
    """, re.VERBOSE
)
                       
# A whitespace character lxml decodes from entities such as &#160; .
_WS_RE = re.compile(r"[\s\xa0]+")

def _clean(text: str) -> str:
    """Collapses all whitespaces to single spaces, trim ends."""
    return _WS_RE.sub(" ", text).strip()


# -----------------------------------------------------------------------------
# Parser settings
# -----------------------------------------------------------------------------

# bs4 backend: lxml is lenient about EDGAR's mixed/incomplete markup;
# swap for the stdlib "html.parser" if lxml must never appear.
PARSER = "lxml"
ENCODING = "utf-8"


# =============================================================================
# Main class
# =============================================================================

class Converter:
    """One R-file -> a flat JSON of records.

    The pipeline is fixed: find the table, walk its rows, extract fields.
    The hooks each answer one question.
    """
    def __init__(
        self, stmt: str, cik: str = "", report_date: str = ""
    ) -> None:
        if stmt not in STATEMENTS:
            sys.exit(f"convert: unknown statement {stmt!r} (want is|bs|cf)")
        self.stmt = stmt
        self.cik = cik.lstrip("0") or cik
        self.report_date = report_date
    
    
    @staticmethod
    def soup(path: Path) -> BeautifulSoup:
        """Parses the .htm file into a BeautifulSoup tree.
        
        Ingests the raw .htm file as a pathlib Path object and extracts the raw
        charachters to treat it as simple text to parse.
        """
        return BeautifulSoup(path.read_text(encoding=ENCODING), PARSER)


    def main_table(self, soup: BeautifulSoup):  # NOTE: Brittle.
        """The statement's table.

        The single <table class="report"> the R-file builder
        emits; fall back to the first row-bearing table.  Override for
        a layout where the statement table is something else.
        """
        table = soup.find("table", class_=["report"])  # finds 1° table with class "report"
        
        if table is None:
            table = next(
                (t for t in soup.find_all("table") if t.find("tr")),  # first table with "tr"
                soup.find("table"),  # first table in general
            )
            
        if table is None:
            raise ValueError("no <table> found in file")
        
        return table


    @staticmethod
    def rows(table) -> list:
        """Returns every row of the table.

        Directs <tr> children only (recursive=False): footnote and
        definition tables live *inside* report cells and must not leak
        in as rows.
        """
        return table.find_all("tr", recursive=False)


    def skip_row(self, row) -> bool:
        """Drops a row.

        Rows without a concept reference ─ the two header
        rows and any layout row never got a tag.  A data row we cannot
        tag is one we cannot audit, so it is dropped rather than
        guessed at.
        """
        return not self.tag_of(self.cells(row))


    def cells(self, row) -> list:
        """Returns a row's cells as a list of BeautifulSoup elements."""
        return row.find_all(["td", "th"], recursive=False)


    def convert(self, path: Path) -> list[dict]:
        """Reads one statement file and returns its list of records."""
        table = self.main_table(self.soup(path))
        records = []
        order = 0
        
        for row in self.rows(table):
            if self.skip_row(row):
                continue  # NOTE: Isn't it dangerous to skip rows without tag?
            
            order += 1
            cells = self.cells(row)
            date_part = f"{self.report_date}" if self.report_date else ""
            
            records.append({
                "id": f"{self.cik}_{self.stmt}_{date_part}_{order:02d}",  # if 99 rows are too many, the format can be changed to 03d
                "stmt": self.stmt,
                "order": order,
                "level": self.level_of(cells),
                "label": self.label_of(cells),
                "tag": self.tag_of(cells),
                "has_value": self.has_value(cells),
            })
            
        if not records:
            raise ValueError(f"no tagged data rows extracted from {path}")
        
        return records


    def dumps(self, records: list[dict]) -> str:
        """Compacts JSON, no decorative whitespace."""
        return json.dumps(records, ensure_ascii=False, separators=(",", ":"))


    def label_of(self, cells) -> str:  # NOTE: Brittle.
        """Returns the row's label.

        The first non-empty cell in the row (the label cell in
        these reports; footnote markers like [1] stay in).
        """
        for cell in cells:
            text = _clean(cell.get_text())  # remove whitespaces
            
            if text:
                return text  # return the first non-empty one
            
        return ""


    def tag_of(self, cells) -> str:  # NOTE: Brittle.
        """Returns the tag of the row, verbatim.

        The first Show.showAR("...") argument found anywhere in
        the row ─ the anchor lives in the label cell in these reports,
        but searching the row is robust to a different cell.
        """
        for cell in cells:
            
            m = _TAG_RE.search(cell.get_text())  # search visible text
            
            if not m:
                m = _TAG_RE.search(str(cell))  # search raw htm
            if m:
                return m.group(1)  # return first captured group
            
        return ""


    def level_of(self, cells) -> int:  # NOTE: Brittle.
        """Returns the grouping depth of the row.

        0 for subtotal / heading rows (label rendered bold),
        1 for ordinary line items.  These files carry no indentation,
        so bold is the only depth signal the markup offers.
        """
        for cell in cells:
            text = _clean(cell.get_text())
            
            if not text:
                continue
            return 0 if cell.find(["strong", "b"]) else 1
        
        return 0


    def has_value(self, cells) -> bool:
        """Whether the row carried numeric period values.

        The method looks at each cell in the row except for the "TAG" cell,
        and returns True if any of the cells contains a numeric value with or
        without periods or currency signs.
        """
        for cell in cells:
            
            # Skip the "TAG" cell, because it may contain
            # numbers that are not numeric values.
            if _TAG_RE.search(str(cell)):
                continue

            text = _clean(cell.get_text())
            if not text:
                continue

            # Check if the cell actually contains numeric values.
            if _VALUE_RE.fullmatch(text):
                return True
            
        return False


# =============================================================================
# Finding the filing and running the conversion
# =============================================================================

def find_filing(cik: str, stmt: str, report_date: str | None = None) -> Path:
    """Locates the statement's .htm file under DATA_DIR.

    Conventions: CIK is 10 digits zero-padded in filenames; if several
    filings match (multiple 10-K dates), the most recent wins.
    """
    cik = cik if not cik.isdigit() else cik.zfill(10)

    if report_date:  # If a specific report date is given, match that exact filename pattern.
        pattern = f"{cik}_{report_date}_10k_{stmt}.htm"
        hits = list(DATA_DIR.glob(pattern))
        if not hits:
            sys.exit(f"convert: no filing matching {pattern} in {DATA_DIR}")
        return hits[0]

    hits = sorted(DATA_DIR.glob(f"{cik}_*_10k_{stmt}.htm"))  # Gather all historical filings for this CIK and statement.
    if not hits:
        
        known = sorted(
            {p.name.split("_")[0] for p in DATA_DIR.glob("*_10k_*.htm")}
        )
        
        sys.exit(
            f"convert: no {stmt} filing for cik {cik} in {DATA_DIR}\n"
            f"  available: {', '.join(known) or '(none)'}"
        )
        
    return hits[-1]


def convert(
    cik: str,
    stmt: str,
    converter: Converter | None = None,
    out: Path | None = None,
    report_date: str | None = None,
) -> Path:
    """Full automatic pass: locate the file, extract records, write JSON.

    cik         -- CIK, any width (34088 or 0000034088).
    stmt        -- ("is" | "bs" | "cf").
    converter   -- a Converter; defaults to one built in this module.
    out         -- output json path.
    report_date -- optional specific report date (YYYY-MM-DD).

    Returns the path written, so a pipeline can chain on it.
    """
    path = find_filing(cik, stmt, report_date=report_date)
    actual_date = path.name.split("_")[1]
    
    if converter is None:
        converter = Converter(stmt, cik, report_date=actual_date)
    else:
        converter.stmt = stmt
        converter.cik = cik.lstrip("0") or cik
        converter.report_date = actual_date
        
    records = converter.convert(path)
    
    if out is None:
        padded_cik = converter.cik.zfill(10)
        out = OUTPUT_DIR / f"{padded_cik}_{actual_date}_{stmt}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    
    out.write_text(converter.dumps(records), encoding=ENCODING)
    return out


# =============================================================================
# Main function
# =============================================================================

def main(argv=None) -> None:
    """CLI entry point: convert.py <cik> [stmt] [--date DATE] [--all]."""
    parser = argparse.ArgumentParser(
        description="Convert a 10-K statement R-file into LLM-ready JSON."
    )
    
    parser.add_argument("cik", help="company CIK, e.g. 0000034088 or 34088")
    parser.add_argument(
        "stmt", nargs="?", default=None, choices=STATEMENTS,
        help="statement (is | bs | cf)"
    )
    out_hint = f"{OUTPUT_DIR}/<padded_cik>_<date>_<stmt>.json"
    parser.add_argument(
        "--out", type=Path, default=None,
        help=f"output .json path (default {out_hint})"
    )
    parser.add_argument(
        "--date", default=None, help="specific report date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="convert all downloaded historical statements for this CIK and statement"
    )
    
    ns = parser.parse_args(argv)
    
    if ns.all:
        padded_cik = ns.cik if not ns.cik.isdigit() else ns.cik.zfill(10)
        stmts = [ns.stmt] if ns.stmt else STATEMENTS
        written = []
        for s in stmts:
            for p in sorted(DATA_DIR.glob(f"{padded_cik}_*_10k_{s}.htm")):
                rep_date = p.name.split("_")[1]
                written.append(convert(ns.cik, s, report_date=rep_date))
        for w in written:
            print(w)
        return
        
    if not ns.stmt:
        parser.error(
            "the following arguments are required: stmt (or use --all)"
        )
        
    path = convert(ns.cik, ns.stmt, out=ns.out, report_date=ns.date)
    print(path)


if __name__ == "__main__":
    main()
