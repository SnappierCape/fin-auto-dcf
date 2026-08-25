#!/usr/bin/env python3
# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Convert one EDGAR R-file (standalone statement .htm) into line records.

This is the HTM -> JSON stage of the LLM reclassification pipeline.
EDGAR pre-renders each tagged section of a filing as a clean standalone
HTML table (an "R-file", e.g. the balance-sheet-only extract). Every R-file
presents a <table> of statement rows, but they do NOT all share the exact
same layout: the header row can be nested, label cells can be spanned with
colspan, the XBRL tag column may or may not be present.

So this module defines the *structure* of the conversion and stops short
of hard-coding one particular filer's layout. Everything that is shared
(load, row walk, record shape, JSON serialisation) is fixed. Everything
that is layout-specific is delegated to the overridable hooks below.

To adapt a given R-file: subclass Converter and override only the hooks
that differ. convert() stays the single entry point and is unchanged.

Record contract (one dict per data row, keys always present):
    id         str   stable row key: "<base>_<zero-padded-order>"
    stmt       str   which statement it came from (is | bs | cf | custom)
    order      int   row position within the table, 0-based
    level      int   hierarchy depth of the row (0 = top line)
    label      str   the cleaned line-label text
    tag        str   the XBRL concept on this row ("" when absent)
    has_value  bool  True when the row carries at least one period value

Values are deliberately NOT parsed into numbers here -- this stage is
text- and structure-only. Downstream (tag reclassification, number
extraction) reads from this record shape.

Usage (as a script):
    uv run src/llm/convert.py <file.htm> --stmt bs
    uv run src/llm/convert.py <file.htm> --stmt bs --out out.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bs4 import BeautifulSoup

# HTML parser handed to BeautifulSoup. "lxml" is the most lenient for the
# messy, inconsistent markup EDGAR produces. Switch to the stdlib
# "html.parser" to drop the lxml dependency entirely.
PARSER = "lxml"


# =============================================================================
# Converter : fixed pipeline + layout hooks
# =============================================================================


class Converter:
    """Turn one R-file (an HTM statement table) into a list of line records.

    The fixed pipeline is:

        HTM  ->  text     (load)
             ->  soup     (make_soup)
             ->  table    (main_table)
             ->  rows     (skip_row  keeps only data rows)
             ->  cells    (split_row)
             ->  record   (build_row, using the interpret hooks)

    Only the row interpretation is delegated to the hooks. If you are
    parsing a filer whose R-file looks different, override one or more
    hooks -- you never need to touch convert() itself.
    """

    # -------------------------------------------------------------------------
    # Configuration
    # -------------------------------------------------------------------------

    def __init__(self, stmt: str = "generic") -> None:
        """Record the statement kind (is|bs|cf|custom) stamped on each line."""
        self.stmt = stmt

    # -------------------------------------------------------------------------
    # Layout hooks
    # -------------------------------------------------------------------------    
    # Override any of these for a differently laid-out
    # HTML table. Each has a sensible default so convert() runs out of the
    # box, but a real R-file will usually want label_of / tag_of / level_of
    # adjusted.

    def main_table(self, soup: BeautifulSoup):
        """Returns the table element that holds the statement rows.

        Default: the first <table>. Override when the statement table is
        not first on the page (after a title, wrapped, etc.).
        """
        return soup.find("table")

    def skip_row(self, row) -> bool:
        """True when a row should be dropped (header, total, spacer, note).

        Default: drop only blank rows. Override to also drop a filer's
        column-header row, section headings, or parenthetical details.
        """
        return row.get_text(strip=True) == ""

    def split_row(self, row) -> list[str]:
        """Flattens one table row into a list of cleaned cell strings.

        Uses <td>/<th>; if a row has neither (a bare <tr> of text) it falls
        back to treating the row itself as a single cell.
        """
        cells = row.find_all(["td", "th"]) or [row]
        return [self.clean(c.get_text()) for c in cells]

    def label_of(self, cells: list[str]) -> str:
        """Picks the line label from the cells. Default: the first cell."""
        return cells[0] if cells else ""

    def tag_of(self, cells: list[str]) -> str:
        """Picks the XBRL concept tag from the cells.

        Default: the last cell that contains a ":", matching us-gaap: and
        company tags like cisco:. Override if the tag lives elsewhere.
        """
        for text in reversed(cells):
            if ":" in text:
                return text
        return ""

    def level_of(self, cells: list[str]) -> int:
        """Returns the hierarchy depth of the row. Default: 0 (flat).

        Override to encode the filer's indentation / nesting. Depth is the
        strongest free signal for the downstream reclassification: a child
        row inherits the family of the row above it.
        """
        return 0

    def has_value(self, cells: list[str]) -> bool:
        """True when the row carries at least one period value.

        Default: some cell beyond the label that is non-blank and not the
        tag. Override if a filer's value cells are in a fixed column range.
        """
        tag = self.tag_of(cells)
        for text in cells[1:]:
            if text and text != tag:
                return True
        return False

    # -------------------------------------------------------------------------
    # Shared pipeline -- normally left as-is
    # -------------------------------------------------------------------------

    def load(self, path: Path) -> str:
        """Reads the R-file and returns its decoded text."""
        return path.read_text(encoding="utf-8")

    def make_soup(self, html: str) -> BeautifulSoup:
        """Parses the HTML into a BeautifulSoup tree."""
        return BeautifulSoup(html, PARSER)

    def clean(self, text: str) -> str:
        """Normalises a cell: trims and collapses runs of whitespace to one."""
        return " ".join(text.split())

    def build_row(self, base: str, order: int, cells: list[str]) -> dict:
        """Assembles one output record from a parsed row's cells.

        This is the single place the record contract is built: the layout
        hooks above supply its values. Change the shape here, everywhere.
        """
        return {
            "id": f"{base}_{order:03d}",
            "stmt": self.stmt,
            "order": order,
            "level": self.level_of(cells),
            "label": self.label_of(cells),
            "tag": self.tag_of(cells),
            "has_value": self.has_value(cells),
        }

    def convert(self, path: Path, stmt: str | None = None) -> list[dict]:
        """Runs the whole pipeline on one R-file and returns the records."""
        if stmt is not None:
            self.stmt = stmt
        soup = self.make_soup(self.load(path))
        table = self.main_table(soup)
        if table is None:
            raise SystemExit(f"no <table> found in {path}")
        rows = [r for r in table.find_all("tr") if not self.skip_row(r)]
        base = path.stem
        records = []
        for order, row in enumerate(rows):
            records.append(self.build_row(base, order, self.split_row(row)))
        return records

    @staticmethod
    def dumps(records: list[dict]) -> str:
        """Serialises records to compact JSON (no decorative whitespace)."""
        return json.dumps(records, ensure_ascii=False, separators=(",", ":"))


# =============================================================================
# Command-line entry point
# =============================================================================


def main(argv: list[str] | None = None) -> None:
    """Convert one R-file to JSON (next to it, or to --out)."""
    parser = argparse.ArgumentParser(
        description="Convert an EDGAR R-file .htm into line-record JSON."
    )
    parser.add_argument("input", type=Path, help="path to the R-file .htm")
    parser.add_argument(
        "--stmt", default="generic", help="is | bs | cf | custom"
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="output .json path"
    )
    args = parser.parse_args(argv)

    records = Converter(stmt=args.stmt).convert(args.input)
    out = args.out or args.input.with_suffix(".json")
    out.write_text(Converter.dumps(records) + "\n", encoding="utf-8")
    print(f"{args.input} -> {out}  ({len(records)} rows)")


if __name__ == "__main__":
    main()
