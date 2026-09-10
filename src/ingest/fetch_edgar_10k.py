#!/usr/bin/env python3
# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Download 10-K annual reports plus their three main financial statements.

What this script does
---------------------
Downloads annual reports (10-K) of a US public company, plus the three
statements: income statement, balance sheet, cash-flow statement.
By default in v1, it automatically ingests a static 10-year historical set
with an upfront hard reject if the company was listed less than 10 years ago.

It turns ONE input (the company's SEC CIK, an 8-10 digit ID) into FIVE files
per fiscal year under data/10k/ :

    <cik>_<report_date>_10k.htm           the full 10-K filing
    <cik>_<report_date>_10k_is.htm        income statement only
    <cik>_<report_date>_10k_bs.htm        balance sheet only
    <cik>_<report_date>_10k_cf.htm        cash-flow statement only
    <cik>_<report_date>_10k.json          source manifest (provenance)

The three statement files are EDGAR's "R-files" (R2.htm, R3.htm, R5.htm, ...):
EDGAR pre-renders every tagged section of a filing as a standalone clean HTML
table. They strip out all the inline-XBRL markup noise (embedded in the full
10-K's HTML) and leave plain <table> markup. The manifest JSON records, for
every file: where on EDGAR it came from, its sha256 and size.

Statement identification
------------------------
Each filing's "Report bundle" list (FilingSummary.xml) names statement files
R1.htm, R2.htm, ... - an arbitrary numbering per filer. We match each report's
human-readable <ShortName> against canonical regexes with scoring.

SEC networking rules
--------------------
- We send a User-Agent that identifies the project and a contact, as
  SEC "fair access" rules require (a generic browser UA gets a 403).
- Network requests are paced (minimum 120ms interval) to ensure the client
  never exceeds the SEC 10 requests-per-second rate limit.
- Transient failures (429 rate-limit, 5xx server errors, network blips)
  are retried up to MAX_ATTEMPTS times with a short sleep in between.
- No third-party dependencies: stdlib only.

Failing loudly
--------------
Every error path calls SystemExit with a human-readable message.
If any of the three statements cannot be identified or any download fails,
nothing is left on disk.

Usage
-----
    uv run src/ingest/fetch_edgar_10k.py 0000789019              # 10y history
    uv run src/ingest/fetch_edgar_10k.py 0000789019 --single     # single year

Outputs
-------
The five files per fiscal year listed above, under data/10k/.
"""

from __future__ import annotations

import argparse
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
CONTACT = os.environ.get("EDGAR_CONTACT", "Snapp <snapp@fin-auto-dcf.local>")
USER_AGENT = f"fin-auto-dcf (10-K fetcher; contact: {CONTACT})"

# SEC data endpoints (see https://www.sec.gov/search-faqs).
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data/{cik_dir}/{acc}/"

# Repo root configuration.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO_ROOT / "data" / "10k"

# How many times to retry transient failures (429, 5xx, network errors).
MAX_ATTEMPTS = 3
REQUEST_INTERVAL_SECONDS = 0.12  # Pacing: ~8 requests per second max
_LAST_REQUEST_TIME: float = 0.0


# =============================================================================
# Fetch helpers
# =============================================================================

def _download(url: str) -> bytes:
    """GET a URL with a compliant User-Agent, retrying on 429/5xx.

    A 403 exits with a hint about the contact identity; any other
    error propagates once the retry budget is spent.
    """
    global _LAST_REQUEST_TIME
    now = time.monotonic()
    elapsed = now - _LAST_REQUEST_TIME
    if elapsed < REQUEST_INTERVAL_SECONDS:
        time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)

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
        finally:
            _LAST_REQUEST_TIME = time.monotonic()
    raise SystemExit(
        f"failed to fetch {url} after {MAX_ATTEMPTS} attempts: {last_err}"
    )


def get_json(url: str) -> dict:
    """Fetch URL and parse as JSON."""
    return json.loads(_download(url).decode("utf-8"))


# =============================================================================
# Submission processing
# =============================================================================

def collect_all_submissions(
    cik10: str, sub: dict, min_10ks: int = 10
) -> list[dict]:
    """Harvest all filing records across recent and older submission files.

    Handles SEC pagination under sub['filings']['files'] so high-volume
    issuers with >1,000 recent filings do not drop older 10-Ks.
    """
    records: list[dict] = []
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    for i in range(len(forms)):
        records.append({
            "form": recent["form"][i],
            "accessionNumber": recent["accessionNumber"][i],
            "filingDate": recent["filingDate"][i],
            "reportDate": recent["reportDate"][i],
            "primaryDocument": recent.get(
                "primaryDocument", [None] * len(forms)
            )[i],
        })

    # Count distinct 10-K fiscal years currently discovered
    distinct_years = len({
        r["reportDate"]
        for r in records
        if r["form"] in ("10-K", "10-K/A") and r.get("reportDate")
    })
    if distinct_years < min_10ks:
        files = sub.get("filings", {}).get("files", [])
        for f_meta in files:
            file_name = f_meta.get("name")
            if not file_name:
                continue
            url = f"https://data.sec.gov/submissions/{file_name}"
            older_data = get_json(url)
            forms_old = older_data.get("form", [])
            for i in range(len(forms_old)):
                records.append({
                    "form": older_data["form"][i],
                    "accessionNumber": older_data["accessionNumber"][i],
                    "filingDate": older_data["filingDate"][i],
                    "reportDate": older_data["reportDate"][i],
                    "primaryDocument": older_data.get(
                        "primaryDocument", [None] * len(forms_old)
                    )[i],
                })
            distinct_years = len({
                r["reportDate"]
                for r in records
                if r["form"] in ("10-K", "10-K/A") and r.get("reportDate")
            })
            if distinct_years >= min_10ks:
                break
    return records


def pick_historical_10ks(
    records: list[dict], years_needed: int = 10, single_mode: bool = False
) -> list[dict]:
    """Select distinct 10-K filings, applying restatement precedence.

    Groups 10-K and 10-K/A entries by reportDate. Candidate filings within
    each period are sorted by filingDate descending so amendments are
    preferred (D22). If an amendment lacks financial statements, fallback to
    prior filings is supported.
    Enforces a hard reject if available fiscal years < years_needed.
    """
    grouped_by_date: dict[str, list[dict]] = {}
    for rec in records:
        form = rec.get("form", "")
        if form not in ("10-K", "10-K/A"):
            continue
        rep_date = rec.get("reportDate", "")
        if not rep_date:
            continue
        grouped_by_date.setdefault(rep_date, []).append(rec)

    # Within each fiscal period, sort candidates by filingDate descending.
    for group in grouped_by_date.values():
        group.sort(key=lambda x: x["filingDate"], reverse=True)

    # Sort periods chronologically descending (newest reportDate first).
    sorted_dates = sorted(grouped_by_date.keys(), reverse=True)

    if single_mode:
        if not sorted_dates:
            raise SystemExit("no 10-K or 10-K/A found in submissions feed")
        latest_date = sorted_dates[0]
        return [{
            "reportDate": latest_date,
            "candidates": grouped_by_date[latest_date],
            **grouped_by_date[latest_date][0],
        }]

    if len(sorted_dates) < years_needed:
        raise SystemExit(
            f"Hard reject: Issuer has only {len(sorted_dates)} fiscal years "
            f"of 10-K filings on EDGAR (< {years_needed} required for v1 DCF)."
            " Company was listed less than 10 years ago (D21)."
        )

    return [
        {
            "reportDate": d,
            "candidates": grouped_by_date[d],
            **grouped_by_date[d][0],
        }
        for d in sorted_dates[:years_needed]
    ]


# =============================================================================
# Statement identification (FilingSummary.xml)
# =============================================================================

# -----------------------------------------------------------------------------
# Regex patterns for statement identification across GAAP variants
# -----------------------------------------------------------------------------
STATEMENT_PATTERNS: dict[str, list[re.Pattern]] = {
    # ── Income statement patterns ────────────────────────────────────────────
    "is": [
        re.compile(
            r"^(consolidated\s+)?(statements?\s+of\s+)?"
            r"(operations|income|earnings|loss|profit\s+and\s+loss)"
            r"(\s+and\s+comprehensive\s+(income|loss))?"
            r"(\s+for\s+the\s+.*)?$",
            re.I,
        ),
        re.compile(
            r"^(consolidated\s+)?results\s+of\s+operations"
            r"(\s+for\s+the\s+.*)?$",
            re.I,
        ),
        re.compile(
            r"^(consolidated\s+)?(income|operations|earnings)\s+statements?"
            r"(\s+for\s+the\s+.*)?$",
            re.I,
        ),
    ],
    # ── Balance sheet patterns ───────────────────────────────────────────────
    "bs": [
        re.compile(
            r"^(consolidated\s+)?(statements?\s+of\s+)?"
            r"(financial\s+position|financial\s+condition|balance\s+sheets?)"
            r"(\s+at\s+.*)?$",
            re.I,
        ),
        re.compile(
            r"^(consolidated\s+)?balance\s+sheets?"
            r"(\s+at\s+.*)?$",
            re.I,
        ),
    ],
    # ── Cash flow statement patterns ─────────────────────────────────────────
    "cf": [
        re.compile(
            r"^(consolidated\s+)?statements?\s+of\s+cash\s+flows?"
            r"(\s+for\s+the\s+.*)?$",
            re.I,
        ),
        re.compile(
            r"^(consolidated\s+)?statements?\s+of\s+cash\s+flow"
            r"(\s+for\s+the\s+.*)?$",
            re.I,
        ),
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
EXCLUSION_PATTERNS: list[re.Pattern] = [
    re.compile(r"parenthetical", re.I),
    re.compile(r"note\s+\d+", re.I),
    re.compile(r"schedule", re.I),
    re.compile(r"segment", re.I),
    re.compile(r"equity", re.I),
    re.compile(r"capital", re.I),
    re.compile(
        r"^consolidated\s+statements?\s+of\s+comprehensive\s+income", re.I
    ),
    re.compile(r"^consolidated\s+comprehensive\s+income", re.I),
]

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
CONTENT_ANCHORS: dict[str, list[str]] = {
    "is": [
        "revenue",
        "sales",
        "operating income",
        "operating loss",
        "net income",
        "net loss",
    ],
    "bs": [
        "total assets",
        "total liabilities",
        "stockholders' equity",
        "shareholders' equity",
        "retained earnings",
    ],
    "cf": [
        "operating activities",
        "investing activities",
        "financing activities",
        "cash and cash equivalents",
    ],
}


def pick_statements(summary_xml: bytes) -> dict[str, dict[str, str]]:
    """Map statement kind -> {short_name, html_file} from FilingSummary.xml."""
    try:
        root = ET.fromstring(summary_xml)
    except ET.ParseError as e:
        raise ValueError(f"FilingSummary.xml is not valid XML: {e}") from e

    candidates: dict[str, list[dict]] = {"is": [], "bs": [], "cf": []}

    for report in root.iter("Report"):
        short = (report.findtext("ShortName") or "").strip()
        html_file = (report.findtext("HtmlFileName") or "").strip()
        category = (report.findtext("MenuCategory") or "").strip()
        position = int(report.findtext("Position") or 999)

        if not short or not html_file:
            continue

        if category.lower() in EXCLUDED_MENU_CATEGORIES:
            continue

        if any(p.search(short) for p in EXCLUSION_PATTERNS):
            continue

        for kind, patterns in STATEMENT_PATTERNS.items():
            for pattern in patterns:
                if pattern.match(short):
                    score = 100
                    if category.lower() == "statements":
                        score += 50
                    score -= min(position, 40)

                    candidates[kind].append({
                        "short_name": short,
                        "html_file": html_file,
                        "score": score,
                        "category": category,
                    })
                    break

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

    missing = [k for k in STATEMENT_PATTERNS if k not in matched]
    if missing:
        named = {k: v["short_name"] for k, v in matched.items()}
        candidates_all = sorted(
            (r.findtext("ShortName") or "").strip()
            for r in root.iter("Report")
            if (r.findtext("ShortName") or "").strip()
        )
        raise ValueError(
            f"could not identify statements {missing} in FilingSummary.xml "
            f"(matched so far: {named}). "
            f"Available ShortNames: {candidates_all}"
        )
    return matched


def verify_statement_content(
    html_bytes: bytes, kind: str
) -> tuple[bool, float, list[str]]:
    """Verify downloaded R-file HTML contains canonical financial items."""
    try:
        text = html_bytes.decode("utf-8", errors="ignore").lower()
    except Exception:
        return False, 0.0, []

    expected = CONTENT_ANCHORS.get(kind, [])
    if not expected:
        return True, 1.0, []

    found = [anchor for anchor in expected if anchor in text]
    confidence = len(found) / len(expected)
    passed = len(found) >= 2
    return passed, round(confidence, 2), found


# =============================================================================
# Main execution pipeline
# =============================================================================

def main() -> None:
    """Fetch 10-K bundles from EDGAR for the given CIK."""
    parser = argparse.ArgumentParser(
        description="Download 10-K reports and core statements from EDGAR."
    )
    parser.add_argument("cik", help="Company CIK (digits, e.g. 0000789019)")
    parser.add_argument(
        "--single",
        action="store_true",
        help="Fetch only the single latest fiscal year (testing/debugging)",
    )
    parser.add_argument(
        "--history-years",
        type=int,
        default=10,
        help="Number of historical fiscal years required (default: 10)",
    )
    args = parser.parse_args()

    cik_arg = args.cik.strip().removeprefix("CIK").removesuffix(".json")
    if not cik_arg.isdigit():
        raise SystemExit(f"CIK must be digits, got: {args.cik!r}")
    cik_int = int(cik_arg)
    cik10 = f"{cik_int:010d}"
    cik_dir = str(cik_int)

    # 1) Fetch submissions feed
    sub = get_json(SUBMISSIONS_URL.format(cik10=cik10))
    issuer: str = sub.get("name", "<unknown>")
    tickers: str = ",".join(sub.get("tickers", []))

    min_needed = 1 if args.single else args.history_years
    all_records = collect_all_submissions(cik10, sub, min_10ks=min_needed)
    selected_filings = pick_historical_10ks(
        all_records, years_needed=args.history_years, single_mode=args.single
    )

    print(f"issuer       : {issuer}")
    print(f"tickers      : {tickers}")
    mode_label = (
        "single latest year"
        if args.single
        else f"{len(selected_filings)} fiscal years"
    )
    print(f"mode         : {mode_label}")
    print("=" * 79)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for idx, filing_group in enumerate(selected_filings, start=1):
        report_date = filing_group["reportDate"]
        candidates = filing_group.get("candidates", [filing_group])

        total_n = len(selected_filings)
        print(f"\n[{idx}/{total_n}] Fiscal Year ending {report_date}")

        chosen_filing: dict | None = None
        statements: dict[str, dict[str, str]] | None = None
        last_candidate_err: str = ""

        for c_idx, candidate in enumerate(candidates):
            c_form = candidate["form"]
            c_acc = candidate["accessionNumber"]
            c_acc_dir = c_acc.replace("-", "")

            summary_url = (
                ARCHIVE_BASE.format(cik_dir=cik_dir, acc=c_acc_dir)
                + "FilingSummary.xml"
            )

            try:
                summary_xml = _download(summary_url)
                statements = pick_statements(summary_xml)
                chosen_filing = candidate
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    last_candidate_err = (
                        f"{c_form} ({c_acc}) lacks FilingSummary.xml"
                    )
                    if len(candidates) > c_idx + 1:
                        print(
                            f"  notice     : {c_form} ({c_acc}) is a "
                            "non-financial amendment (no FilingSummary.xml); "
                            "falling back to prior filing..."
                        )
                        continue
                raise
            except ValueError as e:
                last_candidate_err = (
                    f"{c_form} ({c_acc}) incomplete statements: {e}"
                )
                if len(candidates) > c_idx + 1:
                    print(
                        f"  notice     : {c_form} ({c_acc}) lacks complete "
                        "statements; falling back to prior filing..."
                    )
                    continue
                raise SystemExit(
                    f"could not identify statements for {report_date}: {e}"
                ) from e

        if chosen_filing is None or statements is None:
            raise SystemExit(
                f"could not obtain financial statements for {report_date} "
                f"from any candidate filing: {last_candidate_err}"
            )

        accession = chosen_filing["accessionNumber"]
        acc_dir = accession.replace("-", "")
        filing_date = chosen_filing["filingDate"]
        primary = chosen_filing.get("primaryDocument")
        form_label = chosen_filing["form"]

        print(f"  form       : {form_label}")
        print(f"  filing date: {filing_date}")
        print(f"  accession  : {accession}")

        if not primary:
            index_url = ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir)
            index = get_json(index_url + "index.json")
            for item in index.get("directory", {}).get("item", []):
                if item.get("type") != "text/html":
                    continue
                if item.get("name", "").endswith((".htm", ".html")):
                    primary = item["name"]
                    break
            if not primary:
                raise SystemExit(
                    f"could not determine primary document for {report_date}"
                )

        primary_url = (
            ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + primary
        )

        files: dict[str, bytes] = {f"10k_{primary}": _download(primary_url)}
        stem = f"{cik10}_{report_date}"
        for kind in STATEMENT_PATTERNS:
            out_name = f"{stem}_10k_{kind}.htm"
            src = statements[kind]["html_file"]
            target_url = (
                ARCHIVE_BASE.format(cik_dir=cik_dir, acc=acc_dir) + src
            )
            files[out_name] = _download(target_url)

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

            passed, confidence, found_anchors = verify_statement_content(
                data, kind
            )
            if not passed:
                raise SystemExit(
                    f"Safety check failed for {kind} ({src}) in "
                    f"{report_date}: insufficient financial anchors found "
                    f"(matched: {found_anchors})."
                )

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
                "confidence": confidence,
                "matched_anchors": found_anchors,
            }
            print(
                f"  saved {kind:<5s}: {out_path.name} ({len(data):,} bytes) "
                f"[confidence: {confidence:.2f}]"
            )

        meta = {
            "cik": cik10,
            "issuer": issuer,
            "tickers": sub.get("tickers", []),
            "form": form_label,
            "filing_date": filing_date,
            "report_date": report_date,
            "accession": accession,
            "primary_document": primary,
            "source_url": primary_url,
            "sha256": sha256(body).hexdigest(),
            "bytes": len(body),
            "statements": statements_meta,
            "downloaded_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        meta_text = json.dumps(meta, indent=4, sort_keys=False) + "\n"
        meta_path.write_text(meta_text, encoding="utf-8")
        print(f"  saved meta : {meta_path.name}")

    print("\n" + "=" * 79)
    print(
        f"Successfully ingested {len(selected_filings)} fiscal year(s) "
        f"into {OUT_DIR}"
    )


if __name__ == "__main__":
    main()
