# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Calculates the Free Cash Flow from the reclassified statements.

The reclassified statements for the company are stored in /data/reclassified.
This module is responsible only for extracting the FCF for the selected
fiscal years for thea chosen company.

Usage:

    uv run src/valuation/fcf.py 0000034088          # extracts all the FCFs
    uv run src/valuation/fcf.py 789019 1 4 7        # specific years
    
If standard mode is used (i.e.: full 10y extraction), the module loops through
all the 10 statements and checks that they are all present. If not, it fails
loudly.

Output contract:

The module outputs a single JSON dict in /data/fcf following this structure:

    {
        1: 1259862.25,
        2: 147586.87,
        3: 11553388.05,
        ...
    }

The output file is named {cik}_fcf.json and only contains the
FCFs for the selected fiscal years (or all of them) in numeric order.

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


def statements_present(cik: str, fy_list: list[int]) -> bool:
    """Checks if all the statements for a fy and cik are present."""
    cik = normalize_cik(cik)
    existing_stems = {
        f.stem for f in RECLASSIFIED_DIR.glob(f"{cik}_*.json") if f.is_file()
    }
    
    # Check that the fy suffixes match.
    missing = []
    for fy in fy_list:
        for stmt in STATEMENTS:
            expected = f"{cik}_{fy}_{stmt}"
            
            if expected not in existing_stems:
                missing.append(f"{expected}.json")

    if missing:
        print(f"Missing files: {', '.join(missing)}")
        return False
    
    return True


# =============================================================================
# Calculation helpers
# =============================================================================

def fcf(cik: str, fy_list: list[int]):
    """Calculates the FCF given the inputs."""
    cik = normalize_cik(cik)
    
    if not statements_present(cik=cik, fy_list=fy_list):
        raise ValueError("Input error.")
    
    # Initialize empty dict to hold FCF results.
    fcf = {}
    
    # Extract every FCF input for the selected years.
    for fy in fy_list:
        
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
        fcf[fy] = ebit * (1 - (abs(income_tax) / ebt)) + d_a + nwc + capex
    
    return fcf
        
# =============================================================================
# Main
# =============================================================================
def main() -> int:
    """CLI entry point: extract FCFs and write data/fcf/<cik>_fcf.json.

    Standard mode (no fiscal years given) requires all 10 fiscal years and
    fails loudly if any statement is missing — the 10-year rule.
    """
    parser = argparse.ArgumentParser(
        description="Extract Free Cash Flow from the reclassified statements."
    )
    parser.add_argument("cik", help="Company EDGAR CIK, e.g. 104169")
    parser.add_argument(
        "fy", nargs="*", type=int, default=None,
        help="fiscal years to extract (e.g. 1 4 7). Default: all 10 years (1-10)"
    )
    args = parser.parse_args()

    # Numeric order, no duplicates, regardless of the order typed on the CLI.
    fy_list = sorted(set(args.fy)) if args.fy else list(range(1, 11))

    try:
        cik = normalize_cik(args.cik)
        result = fcf(cik, fy_list)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    # Output contract: {fy: value} per company, keys in numeric order.
    output = {fy: value for fy, value in result.items()}

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{cik}_fcf.json"
    out_path.write_text(json.dumps(output, indent=2) + "\n", encoding=ENCODING)
    print(f"fcf {cik}: {len(output)} fiscal year(s) -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())