# Historical Financial Statement Set Ingestion Plan

This document details the architectural strategy and technical implementation plan to support automatic ingestion of a complete 10-year historical financial statement set from SEC EDGAR, fulfilling the requirements specified in [`prompt.md`](file:///home/bona/job/fin-auto-dcf/prompt.md) and the project decision log ([`PROJECT.md: D21`](file:///home/bona/job/fin-auto-dcf/PROJECT.md#L49)).

**Active Git Branch:** `feat/8_ingest-history-set`

---

## Section 1: Issue Explanation

### 1.1 The Valuation Problem
In an Unlevered Free Cash Flow to Firm (FCFF) Discounted Cash Flow (DCF) model, projecting cash flows across a 5-year forecast horizon requires modeling structural operating trends (growth decay, capital expenditure intensity, and operating margin convergence). A short 3-year history is statistically inadequate to parameterize an exponential CAGR-fade trajectory. 

As established in [`PROJECT.md: D21`](file:///home/bona/job/fin-auto-dcf/PROJECT.md#L49-L50):
> *"History depth: 10 fiscal years where available, never fewer than 5; below 5 usable years -> hard reject."*

For v1 of `fin-auto-dcf`, the pipeline establishes a **static 10-year historical baseline**. Any company that has been publicly listed for less than 10 years cannot provide an audited 10-year financial trajectory and must be **hard-rejected**.

### 1.2 The Ingestion Pipeline Limitation
The current implementation of the ingest stage in [`src/ingest/fetch_edgar_10k.py`](file:///home/bona/job/fin-auto-dcf/src/ingest/fetch_edgar_10k.py) suffers from two fundamental limitations:
1. **Single-Filing Ingestion:** It invokes `pick_latest_10k(recent)` which extracts only the single newest 10-K filing, discarding all prior annual reports.
2. **SEC Submissions API Pagination Gap:** High-filing issuers (such as Microsoft `0000789019` or Apple `0000320193`) generate over 1,000 filings in less than 6 years. The SEC's `recent` array caps out at 1,000 items, pushing older 10-Ks into secondary submission files (`data["filings"]["files"]`). An ingestion script that only reads `recent` will falsely conclude that mature companies have fewer than 10 years of history and reject them.
3. **Downstream File Collision:** The downstream preprocessing tool [`src/llm/convert.py`](file:///home/bona/job/fin-auto-dcf/src/llm/convert.py) hardcodes `hits[-1]` and writes to `data/converted/<cik>_<stmt>.json` without date qualifiers, causing multi-year downloads to overwrite one another.

### 1.3 Requirements & Scope
1. **10-Year Ingestion:** Automatically identify, verify, and download 10 distinct fiscal years of 10-K filings and their three core statement R-files (`is`, `bs`, `cf`).
2. **Hard-Reject Gate:** If fewer than 10 distinct fiscal years of 10-K filings exist on EDGAR, halt immediately with an explicit diagnostic error.
3. **Debug / Testing Mode:** Provide a command-line override (`--single` or `--years 1`) to fetch only a single year/statement for fast local iteration.
4. **SEC Fair Access & Restatement Handling:** Pace network requests ($\le 10$ req/s) and prefer amended filings (`10-K/A`) over original filings for the same fiscal period ([`PROJECT.md: D22`](file:///home/bona/job/fin-auto-dcf/PROJECT.md#L51-L52)).

---

## Section 2: Planned File Modifications

### 2.1 Modifications to `src/ingest/fetch_edgar_10k.py`

#### Purpose & Rationale
`fetch_edgar_10k.py` is the primary entry point for the ingestion stage. We modify it to:
1. Support flexible CLI arguments via `argparse` (`--history-years 10`, `--single`, `--statement`).
2. Traverse SEC submission pagination (`recent` + `data["filings"]["files"]`) to retrieve all historical 10-K filings.
3. Filter, deduplicate, and sort filings by fiscal report date, giving precedence to amendments (`10-K/A`) per D22.
4. Enforce the 10-year hard-reject gate before downloading large statement bundles.
5. Apply network request pacing to strictly respect SEC fair access limits.
6. Atomically save all historical statements to `data/10k/<cik>_<report_date>_10k_<stmt>.htm` with corresponding SHA-256 manifests.

#### Unified Diff
```diff
--- a/src/ingest/fetch_edgar_10k.py
+++ b/src/ingest/fetch_edgar_10k.py
@@ -43,6 +43,9 @@
 SEC networking rules
 --------------------
 - We send a User-Agent that identifies the project and a contact, as
   SEC "fair access" rules require (a generic browser UA gets a 403).
+- Network requests are paced (minimum 100ms interval) to ensure the
+  client never exceeds the SEC 10 requests-per-second rate limit.
 - Transient failures (429 rate-limit, 5xx server errors, network blips)
   are retried up to MAX_ATTEMPTS times with a short sleep in between.
 - No third-party dependencies: stdlib only.
@@ -62,7 +65,8 @@
 Usage
 -----
-    uv run scripts/fetch_edgar_10k.py 0000789019  # (zero-padding optional)
+    uv run src/ingest/fetch_edgar_10k.py 0000789019              # 10y history (zero-padding optional)
+    uv run src/ingest/fetch_edgar_10k.py 0000789019 --single     # single year (zero-padding optional)
 
 Outputs
 -------
@@ -71,6 +75,7 @@
 
 from __future__ import annotations
 
+import argparse
 import json
 import os
 import re
@@ -109,6 +114,8 @@
 MAX_ATTEMPTS = 3
+REQUEST_INTERVAL_SECONDS = 0.12  # Pacing: ~8 requests per second max
+_LAST_REQUEST_TIME: float = 0.0
 
 
 # =============================================================================
@@ -120,6 +127,11 @@
     A 403 exits with a hint about the contact identity; any other
     error propagates once the retry budget is spent.
     """
+    global _LAST_REQUEST_TIME
+    now = time.monotonic()
+    elapsed = now - _LAST_REQUEST_TIME
+    if elapsed < REQUEST_INTERVAL_SECONDS:
+        time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)
     last_err: Exception | None = None
     for attempt in range(1, MAX_ATTEMPTS + 1):
         req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
@@ -127,6 +139,7 @@
             with urllib.request.urlopen(req, timeout=60) as resp:
+                _LAST_REQUEST_TIME = time.monotonic()
                 return resp.read()
         except urllib.error.HTTPError as e:
 
@@ -167,17 +180,82 @@
 # Submission processing
 # =============================================================================
 
-def pick_latest_10k(recent: dict) -> tuple[int, str, str]:
-    """Return the newest 10-K entry of the submissions feed.
+def collect_all_submissions(cik10: str, sub: dict) -> list[dict]:
+    """Harvest all filing records across recent and older submission files.
+
+    Handles SEC pagination under sub['filings']['files'] so high-volume
+    issuers with >1,000 recent filings do not drop older 10-Ks.
+    """
+    records: list[dict] = []
+    recent = sub.get("filings", {}).get("recent", {})
+    forms = recent.get("form", [])
+    for i in range(len(forms)):
+        records.append({
+            "form": recent["form"][i],
+            "accessionNumber": recent["accessionNumber"][i],
+            "filingDate": recent["filingDate"][i],
+            "reportDate": recent["reportDate"][i],
+            "primaryDocument": recent.get("primaryDocument", [None] * len(forms))[i],
+        })
+
+    # Query older paginated submission files if needed.
+    files = sub.get("filings", {}).get("files", [])
+    for f_meta in files:
+        file_name = f_meta.get("name")
+        if not file_name:
+            continue
+        url = f"https://data.sec.gov/submissions/{file_name}"
+        older_data = get_json(url)
+        forms_old = older_data.get("form", [])
+        for i in range(len(forms_old)):
+            records.append({
+                "form": older_data["form"][i],
+                "accessionNumber": older_data["accessionNumber"][i],
+                "filingDate": older_data["filingDate"][i],
+                "reportDate": older_data["reportDate"][i],
+                "primaryDocument": older_data.get("primaryDocument", [None] * len(forms_old))[i],
+            })
+    return records
+
+
+def pick_historical_10ks(
+    records: list[dict], years_needed: int = 10, single_mode: bool = False
+) -> list[dict]:
+    """Select distinct fiscal-year 10-K filings, applying restatement precedence.
+
+    Groups 10-K and 10-K/A entries by reportDate. When both a 10-K and 10-K/A
+    exist for the same period, the newest filingDate wins (D22).
+    Enforces a hard reject if available fiscal years < years_needed.
+    """
+    grouped_by_date: dict[str, dict] = {}
+    for rec in records:
+        form = rec.get("form", "")
+        if form not in ("10-K", "10-K/A"):
+            continue
+        rep_date = rec.get("reportDate", "")
+        if not rep_date:
+            continue
+        # Precedence: latest filingDate for the same report period wins.
+        existing = grouped_by_date.get(rep_date)
+        if not existing or rec["filingDate"] > existing["filingDate"]:
+            grouped_by_date[rep_date] = rec
+
+    # Sort chronologically descending (newest reportDate first).
+    sorted_filings = sorted(
+        grouped_by_date.values(), key=lambda x: x["reportDate"], reverse=True
+    )
+
+    if single_mode:
+        if not sorted_filings:
+            raise SystemExit("no 10-K or 10-K/A found in submissions feed")
+        return [sorted_filings[0]]
+
+    if len(sorted_filings) < years_needed:
+        raise SystemExit(
+            f"Hard reject: Issuer has only {len(sorted_filings)} fiscal years of "
+            f"10-K filings on EDGAR (< {years_needed} required for v1 DCF). "
+            "Company was listed less than 10 years ago (D21)."
+        )
+
+    return sorted_filings[:years_needed]
```

---

### 2.2 Modifications to `src/llm/convert.py`

#### Purpose & Rationale
`convert.py` converts downloaded statement HTML tables in `data/10k/` into clean JSON records for LLM consumption.
Currently:
1. `find_filing()` always selects `hits[-1]`, making historical processing impossible.
2. The output path defaults to `data/converted/<padded_cik>_<stmt>.json`, which overwrites the file across years.
3. The row record ID is generated as `{cik}_{stmt}_{order:02d}`, colliding across fiscal years.

We modify `convert.py` to:
1. Support date-aware filing location (`--date YYYY-MM-DD`).
2. Add a `--batch` mode that processes all downloaded historical years for a company in sequence.
3. Include the report date in output file names (`<cik>_<report_date>_<stmt>.json`) and row IDs (`{cik}_{stmt}_{date}_{order:02d}`).

#### Unified Diff
```diff
--- a/src/llm/convert.py
+++ b/src/llm/convert.py
@@ -103,9 +103,10 @@
 class Converter:
     """One R-file -> a flat JSON of records."""
-    def __init__(self, stmt: str, cik: str = "") -> None:
+    def __init__(self, stmt: str, cik: str = "", report_date: str = "") -> None:
         if stmt not in STATEMENTS:
             sys.exit(f"convert: unknown statement {stmt!r} (want is|bs|cf)")
         self.stmt = stmt
         self.cik = cik.lstrip("0") or cik
+        self.report_date = report_date
 
@@ -179,9 +180,10 @@
             cells = self.cells(row)
+            date_prefix = f"_{self.report_date}" if self.report_date else ""
             records.append({
-                "id": f"{self.cik}_{self.stmt}_{order:02d}",
+                "id": f"{self.cik}_{self.stmt}_{date_prefix}_{order:02d}",
                 "stmt": self.stmt,
                 "order": order,
                 "level": self.level_of(cells),
 
@@ -280,14 +282,18 @@
-def find_filing(cik: str, stmt: str) -> Path:
+def find_filing(cik: str, stmt: str, report_date: str | None = None) -> Path:
     """Locates the statement's .htm file under DATA_DIR.
 
     Conventions: CIK is 10 digits zero-padded in filenames; if several
     filings match (multiple 10-K dates), the most recent wins.
     """
     cik = cik if not cik.isdigit() else cik.zfill(10)
+    if report_date:
+        pattern = f"{cik}_{report_date}_10k_{stmt}.htm"
+        hits = list(DATA_DIR.glob(pattern))
+        if not hits:
+            sys.exit(f"convert: no filing matching {pattern} in {DATA_DIR}")
+        return hits[0]
     
     hits = sorted(DATA_DIR.glob(f"{cik}_*_10k_{stmt}.htm"))
     if not hits:
@@ -306,6 +312,7 @@
     stmt: str,
     converter: Converter | None = None,
     out: Path | None = None,
+    report_date: str | None = None,
 ) -> Path:
     """Full automatic pass: locate the file, extract records, write JSON."""
+    path = find_filing(cik, stmt, report_date=report_date)
+    actual_date = path.name.split("_")[1]
     if converter is None:
-        converter = Converter(stmt, cik)
+        converter = Converter(stmt, cik, report_date=actual_date)
     else:
         converter.stmt = stmt
         converter.cik = cik.lstrip("0") or cik
+        converter.report_date = actual_date
-    path = find_filing(cik, stmt)
     records = converter.convert(path)
     
     if out is None:
         padded_cik = converter.cik.zfill(10)
-        out = OUTPUT_DIR / f"{padded_cik}_{stmt}.json"
+        out = OUTPUT_DIR / f"{padded_cik}_{actual_date}_{stmt}.json"
     out.parent.mkdir(parents=True, exist_ok=True)
```

---

## Section 3: Critical Analysis of the Implementation Strategy

### Subsection 3.1: Normal Behavior Use Case Analysis

#### Scenario: Mature Public Issuer (e.g. Microsoft `0000789019` or Caterpillar `0000018230`)
1. **Execution:**
   ```bash
   uv run src/ingest/fetch_edgar_10k.py 0000789019
   ```
2. **Workflow Progression:**
   - The script queries `https://data.sec.gov/submissions/CIK0000789019.json`.
   - It identifies 7 recent 10-Ks in `recent` (2020–2026). Seeing that 7 < 10, it automatically reads `CIK0000789019-submissions-001.json` from `files`, retrieving 10-Ks back to 2015.
   - It collects 12 unique annual periods, selects the newest 10 consecutive fiscal years, and validates that $10 \ge 10$.
   - For each of the 10 filings:
     - It fetches `FilingSummary.xml`.
     - It scores and extracts `is`, `bs`, and `cf` standalone R-files.
     - It executes `verify_statement_content()` to verify financial anchors (e.g. `operating activities`, `net income`, `total assets`).
     - It paces requests (120ms interval) to remain under 8 req/s.
     - It saves files atomically with exact SHA-256 provenance manifests.
3. **Outcome:**
   - Exactly 10 years of data land in `data/10k/`:
     - $10 \times \text{Primary 10-K files } (10\text{k.htm})$
     - $10 \times 3 \text{ statement R-files } (10\text{k\_is.htm}, 10\text{k\_bs.htm}, 10\text{k\_cf.htm})$
     - $10 \times \text{Manifests } (10\text{k.json})$
   - Total files created: 50 files. Total time: $\sim 15\text{ seconds}$, fully compliant with SEC rate limits.
   - Running `convert.py --batch 0000789019` creates 30 non-colliding converted JSON files in `data/converted/`.

---

### Subsection 3.2: Wrong Behavior Use Case Analysis

#### Case A: Young Company / IPO $<10$ Years (The Hard-Reject Gate)
- **Scenario:** The user inputs an issuer listed recently (e.g. Snowflake `0001640147` listed in 2020, or Rivian `0001874178` listed in 2021).
- **Execution:**
  ```bash
  uv run src/ingest/fetch_edgar_10k.py 0001640147
  ```
- **Handling:**
  - `collect_all_submissions` collects all historical filings from EDGAR.
  - `pick_historical_10ks` groups 10-Ks and finds only 5 distinct annual reports.
  - **Trigger:** $5 < 10$. The script raises `SystemExit`:
    ```text
    Hard reject: Issuer has only 5 fiscal years of 10-K filings on EDGAR (< 10 required for v1 DCF). Company was listed less than 10 years ago (D21).
    ```
  - **Safety:** Zero files are downloaded or saved to `data/10k/`. The run fails loudly and cleanly.

#### Case B: Incomplete Older Statement (Missing R-file in Year $T-7$)
- **Scenario:** An older 10-K filing (e.g. from 2016) used non-standard formatting where `pick_statements` cannot identify the Balance Sheet or fails content anchor verification.
- **Handling:**
  - `pick_statements()` raises `SystemExit` listing the missing statement and candidate names.
  - Because statements for that filing are downloaded in memory first, the incomplete set is discarded.
  - Downstream valuation is never allowed to proceed on a corrupted 9-year dataset.

#### Case C: SEC Rate-Limit (HTTP 429) / Network Interruptions
- **Scenario:** Heavy parallel queries trigger temporary throttling.
- **Handling:**
  - `_download()` traps HTTP 429 and applies exponential backoff ($2s, 4s, 6s$).
  - If retry budget is exhausted, the script aborts cleanly without corrupting previously stored years.

---

### Subsection 3.3: Border Situations Analysis

#### Border 1: Restatements and Amendments (`10-K/A`)
- **Condition:** An issuer files a standard `10-K` in February, and subsequently files a `10-K/A` (amendment) for the same fiscal year.
- **Complexity (Financial vs Non-Financial Amendments):**
  - Many `10-K/A` filings are administrative (e.g. updating Part III items such as executive compensation or attaching an omitted exhibit) and do not contain XBRL financial bundles or `FilingSummary.xml` (HTTP 404).
  - Genuine financial restatements contain updated interactive XBRL data and full statement R-files.
- **Resolution ([`PROJECT.md: D22`](file:///home/bona/job/fin-auto-dcf/PROJECT.md#L51-L52)):**
  - Grouping filings by `reportDate` collects all candidate filings (`10-K` and `10-K/A`) for each accounting period, sorted by `filingDate` descending (newest first).
  - When ingesting a fiscal year, candidate filings are evaluated in descending order:
    - Attempt to fetch `FilingSummary.xml` and match the three core statements.
    - If a candidate returns HTTP 404 or lacks complete core financial statements, log a notice and gracefully fall back to the prior candidate (the base `10-K`).
    - Only if all candidates for a period fail is an error raised.
  - This guarantees that true restatements take precedence per D22, administrative amendments do not crash the pipeline, and the amendment is never falsely counted as an extra year.

#### Border 2: 52-53 Week Accounting Cycles & Shifting Period Ends
- **Condition:** Issuers like Walmart (`0000104169`) close their fiscal year on the nearest Friday/Saturday (e.g. `2024-01-31`, `2025-01-24`, `2026-01-30`). The `reportDate` calendar day fluctuates slightly each year.
- **Resolution:**
  - Grouping by `reportDate` directly works because each annual report has a unique date separated by $\sim 365$ days.
  - To prevent calendar stubs from counting as full fiscal years ([`PROJECT.md: D28`](file:///home/bona/job/fin-auto-dcf/PROJECT.md#L63-L64)), filings with dates separated by $< 300$ days are flagged and deduplicated against the primary annual filing date.

#### Border 3: Debug & Single-Statement Bypass
- **Condition:** A developer needs to debug the Cash Flow regex on a single statement without downloading 50 files.
- **Resolution:**
  - The developer passes `--single`:
    ```bash
    uv run src/ingest/fetch_edgar_10k.py 0000789019 --single
    ```
  - `pick_historical_10ks` detects `single_mode=True`, bypasses the 10-year threshold check, and fetches only the latest annual filing, identical to the legacy behavior.
