# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Calculates the Free Cash Flow from the reclassified statements.

The reclassified statements for the company are stored in /data/reclassified,
named ``<cik>_<YYYY-MM-DD>_<stmt>.json`` (YYYY-MM-DD is the fiscal year end).
This module is responsible only for extracting the FCF for the selected
company.

Usage:

    uv run src/valuation/fcf.py 34088  # extracts all the FCFs
    
The module loops through all the 10 statements and checks that
they are all present. If not, it fails loudly.

Output contract:

The module outputs a single JSON dict in /data/fcf following this structure:

    {
        "1": 1259862.25,
        "2": 147586.87,
        "3": 11553388.05,
        ...
    }

The output file is named {cik}_fcf.json and contains the
FCFs in numeric order (fiscal year 1 is the oldest year).

All the values are in USD units.
"""

from pathlib import Path

import argparse
import json
import sys


# =============================================================================
# Paths and constants
# =============================================================================

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = ROOT_DIR / "data" / "fcf"
RECLASSIFIED_DIR = ROOT_DIR / "data" / "reclassified"

STATEMENTS = ("is", "bs", "cf")

TOTAL_YEARS = 10

ENCODING = "utf-8"


# =============================================================================
# Extraction helpers
# =============================================================================

def normalize_cik(cik: str) -> str:
    """Returns company's CIK zero-padded to 10 digits (EDGAR CIK form).

    Accepts "104169" or the already-padded "0000104169".
    Non-digit input is rejected rather than silently mangled.
    """
    if not cik.isdigit() or len(cik) > 10:
        raise ValueError(f"CIK must be numeric 1-10 digits, got {cik!r}")
    return cik.zfill(10)


def statements_present(cik: str) -> tuple[bool, list[str]]:
    """Checks the reclassified set for a cik: naming convention, the full
    10 fiscal years, and all 3 statements per fiscal year.

    Returns (True, fiscal year ends in date order) when the set is complete,
    or (False, []) when a fiscal year is missing a statement.
    """
    cik = normalize_cik(cik)
    existing_stems = [
        f.stem for f in RECLASSIFIED_DIR.glob(f"{cik}_*.json") if f.is_file()
    ]
    
    # Check the naming convention: <cik>_<YYYY-MM-DD>_<stmt> is 24 chars.
    bad = [s for s in existing_stems if len(s) != 24]
    if bad:
        raise ValueError(f"Found reclassified statements with wrong name: {', '.join(bad)}")
    
    # Put the statements in order of date.
    fy_list = sorted({d.split("_")[1] for d in existing_stems})

    # Check the set is the full 10 fiscal years.
    if len(fy_list) != TOTAL_YEARS:
        raise ValueError(f"Expected {TOTAL_YEARS} fiscal years, found {len(fy_list)}")
    
    # Check that the fy suffixes match.
    missing = []
    for fy in fy_list:
        for stmt in STATEMENTS:
            expected = f"{cik}_{fy}_{stmt}"
            
            if expected not in existing_stems:
                missing.append(f"{expected}.json")

    if missing:
        print(f"Missing files: {', '.join(missing)}")
        return False, []
    
    return True, fy_list


# =============================================================================
# Calculation helpers
# =============================================================================

def fcf(cik: str):
    """Calculates the FCF given the inputs."""
    cik = normalize_cik(cik)
    
    present, fy_list = statements_present(cik=cik)
    if not present:
        raise ValueError("Input error.")
    
    # Initialize empty dict to hold FCF results.
    fcf = {}
    
    # Extract every FCF input for all 10 fiscal years.
    for fy_num, fy in enumerate(fy_list, 1):
        
        is_path = RECLASSIFIED_DIR / f"{cik}_{fy}_is.json"
        cf_path = RECLASSIFIED_DIR / f"{cik}_{fy}_cf.json"
        
        is_file = json.loads(is_path.read_text(encoding=ENCODING))
        cf_file = json.loads(cf_path.read_text(encoding=ENCODING))

        # ── Extract the FCF inputs ───────────────────────────────────────────
        ebit = sum(
            block["value"]
            for block in is_file
            if block["target"] == "is.ebit"
        )
        income_tax = sum(
            block["value"]
            for block in is_file
            if block["target"] == "is.income_tax"
        )
        ebt = sum(
            block["value"]
            for block in is_file
            if block["target"] == "is.ebt"
        )
        d_a = sum(
            block["value"]
            for block in cf_file
            if block["target"] == "cfo.d_a"
        )
        capex = sum(
            block["value"]
            for block in cf_file
            if block["target"] == "cfi.capex"
        )
        nwc = sum(
            block["value"]
            for block in cf_file
            if block["target"] in (
                "cfo.nwc_a_p",
                "cfo.nwc_a_r",
                "cfo.nwc_inv",
                "cfo.nwc_tax_payable",
                "cfo.nwc_dividends_payable",
                "cfo.nwc_other"
            )
        )
        
        # ── Calculate FCF ────────────────────────────────────────────────────
        fcf[fy_num] = ebit * (1 - (abs(income_tax) / ebt)) + d_a + nwc + capex
        # NOTE: If the sign changes in the original statements this formula
        # will give a wrong fcf value. It could be useful to
        # implement a sign check step.
        
    return fcf
        

# =============================================================================
# Main
# =============================================================================

def main() -> int:
    """CLI entry point: extract FCFs and write data/fcf/<cik>_fcf.json.

    Requires all 10 fiscal years and fails loudly if any statement is missing.
    """
    parser = argparse.ArgumentParser(
        description="Extract Free Cash Flow from the reclassified statements."
    )
    parser.add_argument("cik", help="Company EDGAR CIK, e.g. 104169")
    args = parser.parse_args()

    try:
        cik = normalize_cik(args.cik)
        result = fcf(cik)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{cik}_fcf.json"
    out_path.write_text(json.dumps(result, indent=2) + "\n", encoding=ENCODING)
    print(f"fcf {cik}: {len(result)} fiscal year(s) -> {out_path}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())