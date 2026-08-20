#!/usr/bin/env python3
# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Download a company's latest 10-K (primary HTM document) plus the three
main financial statements, from SEC EDGAR.

Input:  a single CIK (the only required input; 10-digit zero-padded or
        plain digits both accepted).
Output: data/10k/<cik>_<report_date>_10k.htm                     the full 10-K HTML
        data/10k/<cik>_<report_date>_10k_income_statement.htm    income statement
        data/10k/<cik>_<report_date>_10k_balance_sheet.htm       balance sheet
        data/10k/<cik>_<report_date>_10k_cash_flow_statement.htm cash flow statement
        data/10k/<cik>_<report_date>_10k.json                    source-manifest
        (provenance metadata for the golden set)

The three statements come from the filing's iXBRL report bundle: EDGAR
publishes each tagged statement as its own clean HTML file (R*.htm) in the
same accession directory, and names them in FilingSummary.xml. We identify
the right three by an anchored match on the <ShortName> values, so wording
variance between filers (e.g. "INCOME STATEMENTS" vs "Consolidated
Statements of Operations") is handled without touching the HTML itself.

Notes
-----
- SEC "fair access" rules require a User-Agent identifying you. Default
  below is a harmless placeholder - edit CONTACT or set EDGAR_CONTACT to
  a real name <email> before heavy use.
- No third-party dependencies (stdlib only).
- Picks the most recent form == "10-K" in the submissions feed
  (falls back to "10-K/A" with a warning if that is all that is present).
- Aborts (loudly) if any of the three statements cannot be identified.

Usage:
    uv run scripts/fetch_edgar_10k.py 1108524
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

# --- configuration -----------------------------------------------------------

CONTACT = os.environ.get("EDGAR_CONTACT", "Snapp <snapp@fin-auto-dcf.local>")
USER_AGENT = f"fin-auto-dcf (10-K fetcher; contact: {CONTACT})"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data/{cik_dir}/{acc}/"

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "10k"

MAX_ATTEMPTS = 3


# --- tiny fetch helper --------------------------------------------------------


def _download(url: str) -> bytes:
    """GET bytes with User-Agent. Retries on 429/5xx, 403 -> hint, else raise."""
    last_err: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                last_err = e
                time.sleep(2 * attempt)
                continue
            if e.code == 403:
                raise SystemExit(
                    "403 Forbidden - EDGAR usually rejects missing/misleading "
                    "User-Agent. Check EDGAR_CONTACT / the UA constant."
                ) from e
            raise
        except urllib.error.URLError as e:
            if attempt < MAX_ATTEMPTS:
                last_err = e
                time.sleep(2 * attempt)
                continue
            raise
    raise SystemExit(f"gave up after {MAX_ATTEMPTS} attempts: {last_err}")


def get_json(url: str) -> dict:
    """GET a JSON document (dict) with User-Agent + retries."""
    raw = _download(url)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"response from {url} was not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise SystemExit(f"expected a JSON object from {url}, got {type(obj).__name__}")
    return obj


# --- core --------------------------------------------------------------------


def pick_latest_10k(recent: dict) -> tuple[int, str, str]:
    """Return (index, form, label) of the newest 10-K in the submissions feed."""
    forms = recent["form"]
    for i, f in enumerate(forms):
        if f == "10-K":
            return i, f, "10-K"
    for i, f in enumerate(forms):
        if f == "10-K/A":
            return i, f, "10-K/A (amendment)"
    raise SystemExit("no 10-K or 10-K/A found in the recent filings feed")


# --- statement identification (FilingSummary.xml) ----------------------------

# Anchored regexes over <ShortName>. Filers phrase the three statements
# differently (MSFT: "INCOME STATEMENTS" / "BALANCE SHEETS" / "CASH FLOWS
# STATEMENTS"; LSCC: "Consolidated Statements of Operations" /
# "Consolidated Balance Sheets" / "Consolidated Statements of Cash Flows"),
# so matching is anchored at both ends - loose in the middle, exact at the
# edges - which also rejects parentheticals, detail tables and notes.
STATEMENT_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    "income_statement": (
        re.compile(r"^(consolidated\s+)?statements?\s+of\s+(income|operations)$", re.I),
        re.compile(r"^(consolidated\s+)?income\s+statements?$", re.I),
    ),
    "balance_sheet": (
        re.compile(r"^(consolidated\s+)?balance\s+sheets?$", re.I),
    ),
    "cash_flow_statement": (
        re.compile(r"^(consolidated\s+)?cash\s+flows?\s+statements?$", re.I),
        re.compile(r"^(consolidated\s+)?statements?\s+of\s+cash\s+flows?$", re.I),
    ),
}


def pick_statements(summary_xml: bytes) -> dict[str, dict[str, str]]:
    """Map statement kind -> {short_name, html_file} from FilingSummary.xml.

    Reads the <Reports>/<Report> list (ShortName + HtmlFileName) of EDGAR's
    iXBRL report bundle and keeps the first report whose ShortName matches
    each statement pattern. Raises if any of the three is not identified.
    """
    try:
        root = ET.fromstring(summary_xml)
    except ET.ParseError as e:
        raise SystemExit(f"FilingSummary.xml is not valid XML: {e}") from e

    matched: dict[str, dict[str, str]] = {}
    for report in root.iter("Report"):
        short = (report.findtext("ShortName") or "").strip()
        html_file = (report.findtext("HtmlFileName") or "").strip()
        if not short or not html_file:
            continue
        for kind, patterns in STATEMENT_PATTERNS.items():
            if kind not in matched and any(p.match(short) for p in patterns):
                matched[kind] = {"short_name": short, "html_file": html_file}

    missing = [k for k in STATEMENT_PATTERNS if k not in matched]
    if missing:
        named = {k: v["short_name"] for k, v in matched.items()}
        candidates = sorted(
            (r.findtext("ShortName") or "").strip()
            for r in root.iter("Report")
            if (r.findtext("ShortName") or "").strip()
        )
        raise SystemExit(
            f"could not identify statements {missing} in FilingSummary.xml "
            f"(matched so far: {named}). "
            f"Available ShortNames: {candidates}"
        )
    return matched


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__.strip())
        raise SystemExit(1)

    cik_arg = sys.argv[1].strip().removeprefix("CIK").removesuffix(".json")
    if not cik_arg.isdigit():
        raise SystemExit(f"CIK must be digits, got: {sys.argv[1]!r}")
    cik_int = int(cik_arg)
    cik10 = f"{cik_int:010d}"
    cik_dir = str(cik_int)  # archive folder strips leading zeros

    # 1) find the latest 10-K -------------------------------------------------
    sub = get_json(SUBMISSIONS_URL.format(cik10=cik10))
    issuer: str = sub.get("name", "<unknown>")
    tickers: str = ",".join(sub.get("tickers", []))
    recent = sub["filings"]["recent"]
    idx, form, label = pick_latest_10k(recent)
    accession = recent["accessionNumber"][idx]
    acc_dir = accession.replace("-", "")
    report_date = recent["reportDate"][idx]
    filing_date = recent["filingDate"][idx]
    primary = recent.get("primaryDocument", [None] * len(recent["form"]))[idx]

    print(f"issuer : {issuer}")
    print(f"tickers : {tickers}")
    print(f"form : {label}")
    print(f"filing date : {filing_date}")
    print(f"report date : {report_date}")
    print(f"accession : {accession}")

    if not primary:
        # Fallback: ask the filing's index.json which file is the primary doc.
        print("primaryDocument missing - consulting index.json ...")
        index = get_json(ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + "index.json")
        for item in index.get("directory", {}).get("item", []):
            if item.get("name", "").endswith((".htm", ".html")) and item.get("type") == "text/html":
                primary = item["name"]
                break
        if not primary:
            raise SystemExit("could not determine the primary 10-K document")

    url = ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + primary
    print(f"primary doc  : {primary}")
    print(f"url          : {url}")

    # 2) identify the 3 statements from the report bundle ------------------
    summary_url = ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + "FilingSummary.xml"
    print(f"bundle index : {summary_url}")
    statements = pick_statements(_download(summary_url))
    for kind in STATEMENT_PATTERNS:
        print(f"statement    : {statements[kind]['short_name']}  -> {statements[kind]['html_file']}")

    # 3) download everything, then land it atomically in data/10k/ ---------
    #    (fetch all 4 first so a mid-way failure never leaves a partial set)
    files: dict[str, bytes] = {f"10k_{primary}": _download(url)}
    stem = f"{cik10}_{report_date}"
    for kind in STATEMENT_PATTERNS:
        out_name = f"{stem}_10k_{kind}.htm"
        src = statements[kind]["html_file"]
        target_url = ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + src
        print(f"downloading: {src}")
        files[out_name] = _download(target_url)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    htm_path = OUT_DIR / f"{stem}_10k.htm"
    meta_path = OUT_DIR / f"{stem}_10k.json"

    body = files[f"10k_{primary}"]
    htm_path.write_bytes(body)

    statements_meta = {}
    for kind in STATEMENT_PATTERNS:
        src = statements[kind]["html_file"]
        out_name = f"{stem}_10k_{kind}.htm"
        out_path = OUT_DIR / out_name
        data = files[out_name]
        out_path.write_bytes(data)
        statements_meta[kind] = {
            "source_html_file": src,
            "short_name": statements[kind]["short_name"],
            "local_file": out_name,
            "source_url": ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + src,
            "sha256": sha256(data).hexdigest(),
            "bytes": len(data),
        }
        print("-" * 60)
        print(f"saved {kind:22s}: {out_path}  ({len(data):,} bytes)")

    meta = {
        "cik": cik10,
        "issuer": issuer,
        "tickers": sub.get("tickers", []),
        "form": label,
        "filing_date": filing_date,
        "report_date": report_date,
        "accession": accession,
        "primary_document": primary,
        "source_url": url,
        "sha256": sha256(body).hexdigest(),
        "bytes": len(body),
        "statements": statements_meta,
        "downloaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    meta_path.write_text(json.dumps(meta, indent=4, sort_keys=False) + "\n", encoding="utf-8")

    print("-" * 60)
    print(f"saved htm    : {htm_path}  ({len(body):,} bytes)  sha256 {meta['sha256']}")
    print(f"saved meta   : {meta_path}")
    tokens = len(body) // 4  # conservative ~4 bytes/token for HTML markup
    print(f"~tokens      : {tokens:,} (rough, pre-stripped HTML, main 10-K only)")


if __name__ == "__main__":
    main()
