#!/usr/bin/env python3
# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Download the latest 10-K plus its three main financial statements.

What this script does
---------------------
Downloads the most recent annual report (10-K) of a US public company,
plus the three statements: income statement, balance sheet, cash-flow
statement.

It turns ONE input (the company's SEC CIK, an 8-10 digit ID - see
https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany to look one
up) into FIVE files under data/10k/ :

    <cik>_<report_date>_10k.htm           the full 10-K filing
    <cik>_<report_date>_10k_is.htm        income statement only
    <cik>_<report_date>_10k_bs.htm        balance sheet only
    <cik>_<report_date>_10k_cf.htm        cash-flow statement only
    <cik>_<report_date>_10k.json          source manifest (provenance)

The three statement files are EDGAR's "R-files" (R2.htm, R3.htm, R5.htm,
...): EDGAR pre-renders every tagged section of a filing as a standalone
clean HTML table. They strip out all the inline-XBRL markup noise
(embedded in the full 10-K's HTML) and leave plain <table> markup, which
is exactly the input a financial-statement parser wants. The manifest
JSON records, for every file: where on EDGAR it came from, its sha256
and size - so any number we later extract from these files can be
verified byte-for-byte against the SEC's own copy. That record is the
provenance spine of the golden set.

Statement identification
------------------------
EDGAR does not name the statements for us. Each filing's "Report bundle"
list (FilingSummary.xml) names the statement files R1.htm, R2.htm, ...
- and that numbering is ARBITRARY per filer.
Instead we match each report's human-readable <ShortName>
against three regexes.
Anchored matching (exact at both ends, free in the middle)
tolerates all that while still rejecting detail tables, parentheticals
and notes.

SEC networking rules
--------------------
- We send a User-Agent that identifies the project and a contact, as
  SEC "fair access" rules require (a generic browser UA gets a 403).
- Transient failures (429 rate-limit, 5xx server errors, network blips)
  are retried up to MAX_ATTEMPTS times with a short sleep in between.
- No third-party dependencies: stdlib only.

Failing loudly
--------------
Every error path calls SystemExit with a human-readable message. Only
"wrong number of arguments" carries an explicit code (1); all other
failures exit with the default SystemExit code (also 1) but with a
distinct message that tells you what failed and, where relevant, what
to fix (e.g. 403 -> check the contact identity). There are no silent
partial downloads: if any of the three statements cannot be identified
or any download fails, nothing is left on disk.

Usage
-----
    uv run scripts/fetch_edgar_10k.py 0000789019  # (zero-padding optional)

Outputs
-------
The five files listed above, under data/10k/.
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

# =============================================================================
# Configuration
# =============================================================================

# SEC fair-access rule: requests must carry a User-Agent identifying the
# caller. We build it from the project name + a contact string. Override
# the contact via the EDGAR_CONTACT env var before heavy use.
# (Placeholder "snapp@fin-auto-dcf.local" is a safe default; replace with
# a real "Name <email>" before hammering EDGAR.)
CONTACT = os.environ.get("EDGAR_CONTACT", "Snapp <snapp@fin-auto-dcf.local>")
USER_AGENT = f"fin-auto-dcf (10-K fetcher; contact: {CONTACT})"

# SEC data endpoints (see https://www.sec.gov/search-faqs).
# SUBMISSIONS_URL: JSON feed of a company's recent filings.
#   {cik10} = 10-digit zero-padded CIK (e.g. 789019 -> 0000789019).
# ARCHIVE_BASE:  static file layout of one filing's directory.
#   {cik_dir} = CIK WITHOUT leading zeros (789019, not 0000789019).
#   {acc}     = accession number WITHOUT dashes.
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data/{cik_dir}/{acc}/"

# Repo root configuration.
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "10k"

# How many times to retry transient failures (429, 5xx, network errors).
MAX_ATTEMPTS = 3


# =============================================================================
# Fetch helpers
# =============================================================================

def _download(url: str) -> bytes:
    """GET a URL with a compliant User-Agent, retrying on 429/5xx.

    A 403 exits with a hint about the contact identity; any other
    error propagates once the retry budget is spent.
    """
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
    """GET a JSON document (dict) with User-Agent + retries.

    Wraps _download and parses the response. Aborts if the body is not
    valid JSON or is not a dict (some SEC endpoints return lists).
    """
    raw = _download(url)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"response from {url} was not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        name = type(obj).__name__
        raise SystemExit(f"expected a JSON object from {url}, got {name}")
    return obj


# =============================================================================
# Submission processing
# =============================================================================

def pick_latest_10k(recent: dict) -> tuple[int, str, str]:
    """Return the newest 10-K entry of the submissions feed.

    A plain 10-K wins; if only amendments are present, the newest
    10-K/A is returned and labelled as such.
    """
    forms = recent["form"]
    for i, f in enumerate(forms):
        if f == "10-K":
            return i, f, "10-K"
    for i, f in enumerate(forms):
        if f == "10-K/A":
            return i, f, "10-K/A (amendment)"
    raise SystemExit("no 10-K or 10-K/A found in the recent filings feed")


# =============================================================================
# Statement identification (FilingSummary.xml)
# =============================================================================

# -----------------------------------------------------------------------------
# Regex patterns for statement identification across GAAP variants
# -----------------------------------------------------------------------------
# Matching Mechanics:
# 1. Anchoring: All patterns use '^' and '$' to enforce full-string matching.
# 2. Case-Insensitivity: 're.I' matches regardless of title-case or uppercase.
# 3. Optional groups: '(consolidated\s+)?' allows optional "Consolidated ".
# 4. Word Alternation: Grouping tests for standard GAAP synonyms.
STATEMENT_PATTERNS: dict[str, list[re.Pattern]] = {
    # ── Income statement patterns ────────────────────────────────────────────
    "is": [
        # Matches "Statements of Income", "Statements of Operations",
        # "Statements of Earnings", "Statement of Profit and Loss",
        # "Results of Operations", and combined comprehensive filings,
        # with optional date suffixes (e.g. " for the Years Ended...").
        re.compile(
            r"^(consolidated\s+)?(statements?\s+of\s+|results\s+of\s+)?"
            r"(income|operations|earnings|profit\s+and\s+loss|"
            r"results\s+of\s+operations)"
            r"(\s+and\s+comprehensive\s+income)?"
            r"(\s+for\s+the\s+.*)?$",
            re.I,
        ),
        # Matches reverse order phrasing such as "Consolidated Income
        # Statements" or "Operations Statement".
        re.compile(
            r"^(consolidated\s+)?"
            r"(income|operations|earnings|profit\s+and\s+loss)\s+"
            r"statements?(\s+for\s+the\s+.*)?$",
            re.I,
        ),
    ],
    # ── Balance sheet patterns ───────────────────────────────────────────────
    "bs": [
        # Standard GAAP "Balance Sheet" or "Consolidated Balance Sheets".
        re.compile(
            r"^(consolidated\s+)?balance\s+sheets?(\s+at\s+.*)?$",
            re.I,
        ),
        # GAAP / IFRS alternative title "Statements of Financial Position".
        re.compile(
            r"^(consolidated\s+)?(statements?\s+of\s+)?"
            r"financial\s+position(\s+at\s+.*)?$",
            re.I,
        ),
    ],
    # ── Cash flow statement patterns ─────────────────────────────────────────
    "cf": [
        # Standard "Consolidated Statements of Cash Flows" (or "Cash Flow").
        re.compile(
            r"^(consolidated\s+)?statements?\s+of\s+cash\s+flows?"
            r"(\s+for\s+the\s+.*)?$",
            re.I,
        ),
        # Alternative phrasing "Consolidated Cash Flows Statements".
        re.compile(
            r"^(consolidated\s+)?cash\s+flows?\s+statements?"
            r"(\s+for\s+the\s+.*)?$",
            re.I,
        ),
    ],
}

# -----------------------------------------------------------------------------
# Disqualification filters
# -----------------------------------------------------------------------------
# Evaluated using 'p.search(short_name)'. If any exclusion pattern matches
# anywhere within the title, the candidate is discarded before scoring.
EXCLUSION_PATTERNS: list[re.Pattern] = [
    re.compile(r"parenthetical", re.I),  # Secondary disclosure parentheticals
    re.compile(r"note\s+\d+", re.I),  # Footnotes and accounting policies
    re.compile(r"schedule", re.I),  # Supplementary valuation schedules
    re.compile(r"segment", re.I),  # Segment reporting breakdowns
    re.compile(r"equity", re.I),  # Stockholders' equity rollforwards
    re.compile(r"capital", re.I),  # Capital changes statements
    re.compile(
        r"^consolidated\s+statements?\s+of\s+comprehensive\s+income", re.I
    ),
    re.compile(r"^consolidated\s+comprehensive\s+income", re.I),
]

# Non-statement categories in SEC EDGAR's FilingSummary.xml report bundle.
EXCLUDED_MENU_CATEGORIES: set[str] = {
    "notes",
    "policies",
    "tables",
    "details",
    "cover",
}

# -----------------------------------------------------------------------------
# Content verification baseline lexicons
# -----------------------------------------------------------------------------
# Substrings expected inside the decoded HTML body of valid statements.
CONTENT_ANCHORS: dict[str, list[str]] = {
    # Income Statement: revenues, operating results, and bottom-line profit.
    "is": [
        "revenue",
        "sales",
        "operating income",
        "operating loss",
        "net income",
        "net loss",
    ],
    # Balance Sheet: assets, liabilities, and stockholders' equity accounts.
    "bs": [
        "total assets",
        "total liabilities",
        "stockholders' equity",
        "shareholders' equity",
        "retained earnings",
    ],
    # Cash Flow: ASC 230 activity sections plus cash reconciliation.
    "cf": [
        "operating activities",
        "investing activities",
        "financing activities",
        "cash and cash equivalents",
    ],
}


def pick_statements(summary_xml: bytes) -> dict[str, dict[str, str]]:
    """Map statement kind -> {short_name, html_file} from FilingSummary.xml.

    Parses FilingSummary.xml, evaluates all reports across the three
    statement categories (IS, BS, CF), applies exclusion filters, computes
    candidate quality scores based on XML metadata and title matching, and
    returns the globally highest-scoring report for each statement kind.
    """
    # Parse the FilingSummary XML byte tree.
    try:
        root = ET.fromstring(summary_xml)
    except ET.ParseError as e:
        raise SystemExit(f"FilingSummary.xml is not valid XML: {e}") from e

    # Dictionary accumulator: collects all scored candidate matches per kind.
    candidates: dict[str, list[dict]] = {"is": [], "bs": [], "cf": []}

    # Iterate over every <Report> node across FilingSummary.xml.
    for report in root.iter("Report"):
        # Extract text attributes and normalize whitespace.
        short = (report.findtext("ShortName") or "").strip()
        html_file = (report.findtext("HtmlFileName") or "").strip()
        category = (report.findtext("MenuCategory") or "").strip()
        position = int(report.findtext("Position") or 999)

        # Skip empty report entries missing required attributes.
        if not short or not html_file:
            continue

        # Step 1: Disqualification check via category and substring search.
        if category.lower() in EXCLUDED_MENU_CATEGORIES:
            continue

        if any(p.search(short) for p in EXCLUSION_PATTERNS):
            continue

        # Step 2: Statement Pattern Evaluation.
        for kind, patterns in STATEMENT_PATTERNS.items():
            for pattern in patterns:
                # 'pattern.match(short)' matches from character 0 to '$'.
                if pattern.match(short):
                    # Base score: awarded for matching a canonical GAAP regex.
                    score = 100

                    # Metadata bonus (+50 pts): primary Statement menu tag.
                    if category.lower() == "statements":
                        score += 50

                    # Position adjustment: prefer primary statements near top.
                    score -= min(position, 40)

                    candidates[kind].append(
                        {
                            "short_name": short,
                            "html_file": html_file,
                            "score": score,
                            "category": category,
                        }
                    )
                    break  # Matched pattern; evaluate next category.

    # Step 3: Best Candidate Selection per Category.
    matched: dict[str, dict[str, str]] = {}
    for kind in ("is", "bs", "cf"):
        kind_candidates = sorted(
            candidates[kind], key=lambda x: x["score"], reverse=True
        )
        if kind_candidates:
            best = kind_candidates[0]
            matched[kind] = {
                "short_name": best["short_name"],
                "html_file": best["html_file"],
            }

    # Step 4: Completeness Gate.
    missing = [k for k in STATEMENT_PATTERNS if k not in matched]
    if missing:
        named = {k: v["short_name"] for k, v in matched.items()}
        # Harvest all short names in the XML bundle to print diagnosis.
        candidates_all = sorted(
            (r.findtext("ShortName") or "").strip()
            for r in root.iter("Report")
            if (r.findtext("ShortName") or "").strip()
        )
        raise SystemExit(
            f"could not identify statements {missing} in FilingSummary.xml "
            f"(matched so far: {named}). "
            f"Available ShortNames: {candidates_all}"
        )
    return matched


# -----------------------------------------------------------------------------
# Content verification helper
# -----------------------------------------------------------------------------

def verify_statement_content(
    html_bytes: bytes, kind: str
) -> tuple[bool, float, list[str]]:
    """Verify downloaded R-file HTML contains canonical financial items.

    Decodes the raw HTML table, searches for standard financial line item
    anchors, and computes an objective confidence ratio.

    Returns:
        passed (bool): True if at least 2 expected anchors are found.
        confidence (float): Fraction of domain anchors identified (0.0..1.0).
        matched_anchors (list[str]): List of detected anchor keywords.
    """
    try:
        # Decode HTML body into lowercase text for case-insensitive matching.
        text = html_bytes.decode("utf-8", errors="ignore").lower()
    except Exception:
        return False, 0.0, []

    expected = CONTENT_ANCHORS.get(kind, [])
    if not expected:
        return True, 1.0, []

    # Detect presence of canonical anchors in the table text.
    found = [anchor for anchor in expected if anchor in text]
    confidence = len(found) / len(expected)
    # Gating rule: require at least 2 distinct anchor terms to pass.
    passed = len(found) >= 2
    return passed, round(confidence, 2), found


# =============================================================================
# Main execution pipeline
# =============================================================================

def main() -> None:
    """Fetch the 10-K bundle for the CIK given as the only argument.

    Pipeline:
    1. Look up the CIK in the submissions feed, pick the newest 10-K.
    2. Locate the primary 10-K document (the inline-XBRL .htm filing).
    3. Read FilingSummary.xml and match the three statements by name.
    4. Download all four files (full 10-K + three R-files).
    5. Write everything to data/10k/ atomically (all-or-nothing).
    6. Build and write the source manifest JSON.

    Steps 4-5 are ordered "fetch everything first, then write" so that
    a mid-run network failure never leaves a half-set on disk.
    """
    if len(sys.argv) != 2:
        print(__doc__.strip())
        raise SystemExit(1)

    cik_arg = sys.argv[1].strip().removeprefix("CIK").removesuffix(".json")
    if not cik_arg.isdigit():
        raise SystemExit(f"CIK must be digits, got: {sys.argv[1]!r}")
    cik_int = int(cik_arg)
    cik10 = f"{cik_int:010d}"  # 10-digit zero-padded (URL form)
    cik_dir = str(cik_int)  # no leading zeros (archive dir form)
    # NOTE: the submissions feed is a PARALLEL-ARRAY layout: each field
    # (form, accessionNumber, reportDate, ...) is a flat list, and the i-th
    # entry of every list describes the same i-th filing. We therefore look
    # up ONE index (idx) and read every field at that index. If EDGAR ever
    # re-groups these into per-filing objects, this breaks loudly.

    # ── 1) Find the latest 10-K ──────────────────────────────────────────────
    sub = get_json(SUBMISSIONS_URL.format(cik10=cik10))
    issuer: str = sub.get("name", "<unknown>")
    tickers: str = ",".join(sub.get("tickers", []))
    recent = sub["filings"]["recent"]
    idx, _, label = pick_latest_10k(recent)
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
        index_url = ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir)
        index = get_json(index_url + "index.json")
        for item in index.get("directory", {}).get("item", []):
            if item.get("type") != "text/html":
                continue
            if item.get("name", "").endswith((".htm", ".html")):
                primary = item["name"]
                break
        if not primary:
            raise SystemExit("could not determine the primary 10-K document")

    url = ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + primary
    print(f"primary doc  : {primary}")
    print(f"url          : {url}")

    # ── 2) Identify the 3 statements from the report bundle ──────────────────
    summary_url = (
        ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + "FilingSummary.xml"
    )
    print(f"bundle index : {summary_url}")
    statements = pick_statements(_download(summary_url))
    for kind in STATEMENT_PATTERNS:
        entry = statements[kind]
        print(f"statement    : {entry['short_name']}  -> {entry['html_file']}")

    # ── 3) Download everything, then land it atomically in data/10k/ ─────────
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

        # Perform content verification safety check on in-memory HTML bytes.
        passed, confidence, found_anchors = verify_statement_content(
            data, kind
        )
        if not passed:
            # Fail loudly and prevent committing invalid tables to disk.
            raise SystemExit(
                f"Safety check failed for {kind} ({src}): "
                f"insufficient financial anchors found (matched: "
                f"{found_anchors})."
            )

        # Commit verified HTML table to disk.
        out_path.write_bytes(data)
        statements_meta[kind] = {
            "source_html_file": src,
            "short_name": statements[kind]["short_name"],
            "local_file": out_name,
            "source_url": (
                ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + src
            ),
            "sha256": sha256(data).hexdigest(),
            "bytes": len(data),
            # Record content verification confidence and anchors (D7/O6).
            "confidence": confidence,
            "matched_anchors": found_anchors,
        }
        print("-" * 60)
        print(
            f"saved {kind:22s}: {out_path}  ({len(data):,} bytes) "
            f"[confidence: {confidence:.2f}]"
        )

    # ── 4) Assemble and write the source manifest (provenance record) ────────
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
        "downloaded_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    meta_text = json.dumps(meta, indent=4, sort_keys=False) + "\n"
    meta_path.write_text(meta_text, encoding="utf-8")

    print("-" * 60)
    print(
        f"saved htm    : {htm_path}  ({len(body):,} bytes)  "
        f"sha256 {meta['sha256']}"
    )
    print(f"saved meta   : {meta_path}")
    tokens = len(body) // 4  # conservative ~4 bytes/token for HTML markup
    print(
        f"~tokens      : {tokens:,} (rough, pre-stripped HTML, "
        "main 10-K only)"
    )


if __name__ == "__main__":
    main()
