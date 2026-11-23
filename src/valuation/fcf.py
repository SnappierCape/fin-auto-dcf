# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Extracts the Free Cash Flow from the reclassified statements.

The reclassified statements for the company are stored in /data/reclassified.
This module is responsible only for extracting the FCF from a selected
statement, a group of statements, or all the statements present for
a single company.

Usage:

    uv run src/valuation/fcf.py 0000034088          # extracts all the FCFs
    uv run src/valuation/fcf.py 789019 1 4 7        # specific years
    
If standard mode is used (i.e.: full 10y extraction), the module loops through
all the 10 statements and checks that they are all present. If not, it fails
loudly.

Output contract:

The module outputs a single JSON dict in /data/fcf following this structure:

    {
        "y1": 1259862.25,
        "y2": 147586.87,
        "y3": 11553388.05,
        ...
    }

The output file reports the CIK of the selected company and only contains the
selected years FCFs (or all of them) in numeric order.

All the values are in USD units.
"""

from pathlib import Path


# =============================================================================
# Paths and constants
# =============================================================================

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = ROOT_DIR / "data" / "fcf"
RECLASSIFIED_DIR = ROOT_DIR / "data" / "reclassified"

STATEMENTS = ("is", "bs", "cf")


# =============================================================================
# Retrieve helpers
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


def fcf():
    """Calculates the FCF given the inputs."""
    pass

# =============================================================================
# Main
# =============================================================================
statements_present("34088", [2])