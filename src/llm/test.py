#!/usr/bin/env python3
# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
import re
import argparse
import sys
from pathlib import Path

from bs4 import BeautifulSoup

# bs4 backend: lxml is lenient about EDGAR's mixed/incomplete markup;
# swap for the stdlib "html.parser" if lxml must never appear.
PARSER = "lxml"


# =============================================================================
# Paths and constants
# =============================================================================


LLM_DIR = Path(__file__).resolve().parent
DATA_DIR = LLM_DIR.parents[1] / "data" / "10k"  # filing html files
STATEMENTS = ("is", "bs", "cf")  # valid statement names

def soup(path: Path) -> BeautifulSoup:
    """Parses the html file into a BeautifulSoup tree (lxml backend)."""
    return BeautifulSoup(path.read_text(encoding="utf-8"), PARSER)

path = Path("/code/fin-auto-dcf/data/10k/0000858877_2025-07-26_10k_is.htm")

soup = soup(path)
print(soup)