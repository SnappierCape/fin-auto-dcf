#!/usr/bin/env python3
# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Download a company's latest 10-K (primary HTM document) from SEC EDGAR.

Input:  a single CIK (the only required input; 10-digit zero-padded or
        plain digits both accepted).
Output: data/10k/<cik>_<report_date>_10k.htm   the full 10-K HTML
        data/10k/<cik>_<report_date>_10k.json a small source-manifest
        (provenance metadata for the golden set)

Notes
-----
- SEC "fair access" rules require a User-Agent identifying you. Default
  below is a harmless placeholder - edit CONTACT or set EDGAR_CONTACT to
  a real name <email> before heavy use.
- No third-party dependencies (stdlib only).
- Picks the most recent form == "10-K" in the submissions feed
  (falls back to "10-K/A" with a warning if that is all that is present).

Usage:
    uv run scripts/fetch_edgar_10k.py 1108524
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
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

    # 2) download -------------------------------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{cik10}_{report_date}_10k"
    htm_path = OUT_DIR / f"{stem}.htm"
    meta_path = OUT_DIR / f"{stem}.json"

    body = _download(url)
    htm_path.write_bytes(body)

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
        "downloaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    meta_path.write_text(json.dumps(meta, indent=4, sort_keys=False) + "\n", encoding="utf-8")

    print("-" * 60)
    print(f"saved htm    : {htm_path}  ({len(body):,} bytes)")
    print(f"saved meta   : {meta_path}")
    print(f"sha256       : {meta['sha256']}")
    tokens = len(body) // 4  # conservative ~4 bytes/token for HTML markup
    print(f"~tokens      : {tokens:,} (rough, pre-stripped HTML)")


if __name__ == "__main__":
    main()
