#!/usr/bin/env python3
# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Converts a 10-K statement R-file (standalone .htm) into LLM-ready JSON.

The pipeline is fully automatic: given a CIK and a statement name, the
module locates the right pre-split R-file under data/10k/, extracts one
flat record per data row, and writes compact JSON into the llm/ folder.

Record contract (fixed, snake_case, key order stable):

    "id"           : "34088_bs_01",
    "stmt"         : "bs",
    "order"        : 1,
    "level"        : 0,
    "label"        : "Current assets",
    "tag"          : "defref_us-gaap_AssetsCurrentAbstract",
    "has_value"    : false

Meaning:

    id        -- "{cik}_{stmt}_{row:02d}", stable across runs for the same file
    stmt      -- the statement name (is / bs / cf)
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

Usage (from the repo root; CWD-independent for data and output dirs)::

    uv run src/llm/convert.py 0000034088 bs                # defaults
    uv run src/llm/convert.py 789019 cf --out /tmp/m.csv.. # explicit out

Programmatic:

    from src.llm.convert import convert, Converter
    out = convert("0000789019", "is")            # -> src/llm/789019_is.json
    out = convert("789019", "cf", out=Path("tmp/cf.json"))
    records = Converter("cf", "789019").convert(
        find_filing("789019", "cf"))

The hooks below the pipeline (main_table / skip_row / label_of / tag_of /
level_of / has_value) are intentionally tiny, per-filer-overridable
defaults.  They exist so this module keeps working when a new filer's
layout shifts a detail, without touching the pipeline around them.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

# bs4 backend: lxml is lenient about EDGAR's mixed/incomplete markup;
# swap for the stdlib "html.parser" if lxml must never appear.
PARSER = "lxml"

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

# This file lives in src/llm/convert.py, so:
LLM_DIR = Path(__file__).resolve().parent            # default output dir
DATA_DIR = LLM_DIR.parents[1] / "data" / "10k"       # filing html files

STATEMENTS = ("is", "bs", "cf")                        # valid statement names

# The concept reference a row's "Details" anchor points at.  It is the
# only machine-stable identity a row has; it is what we audit.
_TAG_RE = re.compile(r"Show\.showAR\(\s*this\s*,\s*'([^']+)'")

# A cell that is (part of) a number: optional currency sign, digits with
# thousands separators and / or decimals, optional parentheses or percent.
_VALUE_RE = re.compile(
    r"^\s*[(]?\s?[$€£]?\s?[\d][\d,\.\s%]*\s*$"
)

# A whitespace character lxml decodes from entities such as &#160; .
_WS_RE = re.compile(r"[\s\xa0]+")


def _clean(text: str) -> str:
    """Collapse all whitespace (incl. nbsp) to single spaces, trim ends."""
    return _WS_RE.sub(" ", text).strip()


class Converter:
    """One 10-K statement R-file -> a list of flat records (see module doc).

    The pipeline is fixed: find the table, walk its rows, extract fields.
    The hooks each answer one question with a per-filer-overridable
    default; override only what differs on the new filer.

    Subclass and pass to convert() to adapt, e.g.::

        class Msc(Converter):
            def has_value(self, cells): ...
        records = convert("789019", "is", converter=Msc("is"))
    """

    #: statement name, stamped into every record (is / bs / cf).
    stmt: str
    #: CIK, stripped of leading zeros (34088, not 0000034088).
    cik: str

    def __init__(self, stmt: str, cik: str = "") -> None:
        if stmt not in STATEMENTS:
            sys.exit(f"convert: unknown statement {stmt!r} (want is|bs|cf)")
        self.stmt = stmt
        self.cik = cik.lstrip("0") or cik

    # -- fixed pipeline --------------------------------------------------

    @staticmethod
    def soup(path: Path) -> BeautifulSoup:
        """Parse the html file into a BeautifulSoup tree (lxml backend)."""
        return BeautifulSoup(
            path.read_text(encoding="utf-8"), PARSER
        )

    def main_table(self, soup: BeautifulSoup):
        """The statement's table.

        Default: the single <table class="report"> the R-file builder
        emits; fall back to the first row-bearing table.  Override for
        a layout where the statement table is something else.
        """
        table = soup.find("table", class_=["report"])
        if table is None:
            table = next(
                (t for t in soup.find_all("table") if t.find("tr")),
                soup.find("table"),
            )
        if table is None:
            raise ValueError("no <table> found in file")
        return table

    @staticmethod
    def rows(table) -> list:
        """The table's own rows.

        Direct <tr> children only (recursive=False): footnote and
        definition tables live *inside* report cells and must not leak
        in as rows.
        """
        return table.find_all("tr", recursive=False)

    def skip_row(self, row) -> bool:
        """Drop a row.

        Default: rows without a concept reference -- the two header
        rows and any layout row never got a tag.  A data row we cannot
        tag is one we cannot audit, so it is dropped rather than
        guessed at.
        """
        return not self.tag_of(self.cells(row))

    def cells(self, row) -> list:
        """A row's cells as a list of BeautifulSoup elements."""
        return row.find_all(["td", "th"], recursive=False)

    def convert(self, path: Path) -> list[dict]:
        """Read one statement file and return its list of records."""
        table = self.main_table(self.soup(path))
        records = []
        order = 0
        for row in self.rows(table):
            if self.skip_row(row):
                continue
            order += 1
            cells = self.cells(row)
            records.append({
                "id": f"{self.cik}_{self.stmt}_{order:02d}",
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
        """Compact JSON, no decorative whitespace (project convention)."""
        import json
        return json.dumps(records, ensure_ascii=False, separators=(",", ":"))

    # -- per-filer hooks -----------------------------------------------------

    def label_of(self, cells) -> str:
        """The row's label text.

        Default: the first non-empty cell's text (the label cell in
        these reports; footnote markers like [1] stay in).  Override if
        a filer puts the label somewhere else.
        """
        for cell in cells:
            text = _clean(cell.get_text())
            if text:
                return text
        return ""

    def tag_of(self, cells) -> str:
        """The concept reference for the row, verbatim.

        Default: the first Show.showAR("...") argument found anywhere in
        the row -- the anchor lives in the label cell in these reports,
        but searching the row is robust to a different cell.  Override
        if a filer carries the concept elsewhere.
        """
        for cell in cells:
            m = _TAG_RE.search(cell.get_text())
            if not m:
                m = _TAG_RE.search(str(cell))
            if m:
                return m.group(1)
        return ""

    def level_of(self, cells) -> int:
        """Grouping depth of the row.

        Default: 0 for subtotal / heading rows (label rendered bold),
        1 for ordinary line items.  These files carry no indentation,
        so bold is the only depth signal the markup offers.  Override
        with a filer's own indentation scheme when you have one.
        """
        for cell in cells:
            text = _clean(cell.get_text())
            if not text:
                continue
            return 0 if cell.find(["strong", "b"]) else 1
        return 0

    def has_value(self, cells) -> bool:
        """Whether the row carried numeric period values.

        Default: any cell (other than the label's) that is (part of) a
        number -- so subtotal, component and note rows are all told
        apart at a glance.  Override if a filer's "value" columns live
        in fixed positions.
        """
        seen_label = False
        for cell in cells:
            text = _clean(cell.get_text())
            if not text:
                continue
            if not seen_label and not _TAG_RE.search(str(cell)):
                seen_label = True
            elif _VALUE_RE.match(text):
                return True
        return False


# ---------------------------------------------------------------------------
# Finding and running the conversion.
# ---------------------------------------------------------------------------

def find_filing(cik: str, stmt: str) -> Path:
    """Locate the statement's html file under DATA_DIR.

    Conventions: CIK is 10 digits zero-padded in filenames; if several
    filings match (multiple 10-K dates), the most recent wins.
    Raises with the list of available CIKs when none matches.
    """
    cik = cik if not cik.isdigit() else cik.zfill(10)
    hits = sorted(DATA_DIR.glob(f"{cik}_*_10k_{stmt}.htm"))
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
) -> Path:
    """Full automatic pass: locate the file, extract records, write JSON.

    cik     -- CIK, any width (34088 or 0000034088).
    stmt    -- "is", "bs" or "cf".
    converter -- a (possibly subclassed) Converter; defaults to one
                 built for this cik + stmt.  Pass a subclass instance
                 to override hooks for a differently laid-out filer.
    out     -- output json path; default LLM_DIR/{cik}_{stmt}.json.

    Returns the path written, so a pipeline can chain on it.
    """
    if converter is None:
        converter = Converter(stmt, cik)
    else:
        converter.stmt = stmt
        converter.cik = cik.lstrip("0") or cik
    path = find_filing(cik, stmt)
    records = converter.convert(path)
    if out is None:
        out = LLM_DIR / f"{converter.cik}_{stmt}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(converter.dumps(records), encoding="utf-8")
    return out


def main(argv=None) -> None:
    """CLI entry point: convert.py <cik> <stmt> [--out PATH]."""
    parser = argparse.ArgumentParser(
        description="Convert a 10-K statement R-file into LLM-ready JSON."
    )
    parser.add_argument("cik", help="company CIK, e.g. 0000034088 or 34088")
    parser.add_argument("stmt", choices=STATEMENTS, help="statement")
    parser.add_argument(
        "--out", type=Path, default=None,
        help=f"output .json path (default {LLM_DIR}/<cik>_<stmt>.json)"
    )
    ns = parser.parse_args(argv)
    path = convert(ns.cik, ns.stmt, out=ns.out)
    print(path)


if __name__ == "__main__":
    main()
