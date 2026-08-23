#!/usr/bin/env python3
# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Download the latest 10-K plus its three main financial statements.

What this script does
---------------------
Downloads the most recent annual report (10-K) of a US public company,
plus the three statements: income statement,
balance sheet, cash-flow statement.

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

# --- configuration -----------------------------------------------------------

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

# Repo root config.
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "10k"

# How many times to retry transient failures (429, 5xx, network errors).
MAX_ATTEMPTS = 3


# --- tiny fetch helper -------------------------------------------------------


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


# --- core --------------------------------------------------------------------


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


# --- statement identification (FilingSummary.xml) ----------------------------

# Anchored regexes over <ShortName>. Filers phrase the three statements
# differently, so matching is anchored at both ends
# - loose in the middle, exact at the
# edges - which also rejects parentheticals, detail tables and notes.
STATEMENT_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    "is": (
        re.compile(
            r"^(consolidated\s+)?statements?\s+of\s+(income|operations)$",
            re.I,
        ),
        re.compile(r"^(consolidated\s+)?income\s+statements?$", re.I),
    ),
    "bs": (
        re.compile(r"^(consolidated\s+)?balance\s+sheets?$", re.I),
    ),
    "cf": (
        re.compile(r"^(consolidated\s+)?cash\s+flows?\s+statements?$", re.I),
        re.compile(
            r"^(consolidated\s+)?statements?\s+of\s+cash\s+flows?$",
            re.I,
        ),
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

    # 1) Find the latest 10-K -------------------------------------------------
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

    # 2) Identify the 3 statements from the report bundle ------------------
    summary_url = (
        ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + "FilingSummary.xml"
    )
    print(f"bundle index : {summary_url}")
    statements = pick_statements(_download(summary_url))
    for kind in STATEMENT_PATTERNS:
        entry = statements[kind]
        print(f"statement    : {entry['short_name']}  -> {entry['html_file']}")

    # 3) Download everything, then land it atomically in data/10k/ ---------
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
            "source_url": (
                ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + src
            ),
            "sha256": sha256(data).hexdigest(),
            "bytes": len(data),
        }
        print("-" * 60)
        print(f"saved {kind:22s}: {out_path}  ({len(data):,} bytes)")

    # 4) Assemble and write the source manifest (provenance record).
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
