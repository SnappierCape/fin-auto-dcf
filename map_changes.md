# Planned Changes for `src/llm/map.py`

This document details the modifications required in [`src/llm/map.py`](file:///home/bona/job/fin-auto-dcf/src/llm/map.py) to support the 10-year historical financial statement dataset introduced in the ingestion and conversion pipeline.

---

## `find_converted`

### Diff

```diff
--- a/src/llm/map.py
+++ b/src/llm/map.py
@@ -625,6 +625,37 @@ def sum_groups(merged: list[dict]) -> dict[str, list[str]]:
 
 
+# =============================================================================
+# Finding converted statements and mapping execution
+# =============================================================================
+
+def find_converted(cik: str, stmt: str, report_date: str | None = None) -> Path:
+    """Locates the converted .json file under CONVERTED_DIR.
+
+    Conventions:
+    - If report_date is specified, matches <cik>_<report_date>_<stmt>.json.
+    - If report_date is omitted, sorts matching files chronologically and
+      returns the most recent fiscal year (hits[-1]).
+    - Falls back to legacy non-dated <cik>_<stmt>.json if present.
+    """
+    cik10 = normalize_cik(cik)
+
+    if report_date:
+        pattern = f"{cik10}_{report_date}_{stmt}.json"
+        hits = list(CONVERTED_DIR.glob(pattern))
+        if not hits:
+            sys.exit(f"map: no converted filing matching {pattern} in {CONVERTED_DIR}")
+        return hits[0]
+
+    hits = sorted(CONVERTED_DIR.glob(f"{cik10}_*_{stmt}.json"))
+    if hits:
+        return hits[-1]
+
+    legacy = CONVERTED_DIR / f"{cik10}_{stmt}.json"
+    if legacy.is_file():
+        return legacy
+
+    known = sorted({p.name.split("_")[0] for p in CONVERTED_DIR.glob("*_*.json")})
+    sys.exit(
+        f"map: no converted {stmt} files for CIK {cik10} in {CONVERTED_DIR}\n"
+        f"  available CIKs: {', '.join(known) or '(none)'}"
+    )
```

### Why

1. **Resolves File Naming Incompatibility:** Downstream from the updated `src/llm/convert.py`, converted files are saved as `data/converted/<cik>_<report_date>_<stmt>.json` (e.g. `0001652044_2025-12-31_is.json`). The legacy code in `map.py` hardcoded `CONVERTED_DIR / f"{cik}_{stmt}.json"`, which fails with a `FileNotFoundError` for any multi-year dataset.
2. **Flexible Historical Access:** It mirrors the design of `find_filing()` in `convert.py`, allowing callers to target an exact historical fiscal year via `report_date`, while defaulting to the latest available annual filing when no date is specified.
3. **Graceful Backward Compatibility:** It checks for the legacy un-dated format (`<cik>_<stmt>.json`) before failing, ensuring existing tests and fixtures continue to function.
4. **Actionable Error Diagnostics:** If a file is missing, it lists the available CIKs found in `data/converted/`, making debugging fast and obvious.

---

## `map_statement`

### Diff

```diff
--- a/src/llm/map.py
+++ b/src/llm/map.py
@@ -655,46 +686,52 @@ def main() -> int:
+def map_statement(
+    cik: str,
+    stmt: str,
+    report_date: str | None = None,
+    url: str = DEFAULT_URL,
+    model: str = DEFAULT_MODEL,
+    out: Path | None = None,
+    system_prompt: str | None = None,
+) -> Path:
+    """Full automatic pass: locate converted file, call Ollama, write mapped JSON."""
+    src = find_converted(cik, stmt, report_date=report_date)
+    actual_date = src.name.split("_")[1] if len(src.name.split("_")) >= 3 else ""
+    cik10 = normalize_cik(cik)
+
+    stem = f"{cik10}_{actual_date}_{stmt}" if actual_date else f"{cik10}_{stmt}"
+    dest = out if out is not None else (MAPPED_DIR / f"{stem}.json")
+
+    records = load_records(src)
+    prompt = system_prompt if system_prompt is not None else build_system_prompt()
+
+    started = time.monotonic()
+    body = call_ollama(prompt, records, url, model)
+    decisions = parse_response(body)
+    by_id = validate_decisions(records, decisions)
+    merged = merge_records(records, by_id)
+
+    dest.parent.mkdir(parents=True, exist_ok=True)
+    dest.write_text(
+        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
+        encoding=ENCODING,
+    )
+    elapsed = time.monotonic() - started
+
+    mapped = sum(1 for rec in merged if rec["target"] is not None)
+    groups = sum_groups(merged)
+    print(
+        f"mapped {stem}: {len(merged)} records "
+        f"({mapped} mapped, {len(merged) - mapped} unmapped) "
+        f"in {elapsed:.1f}s -> {dest}"
+    )
+    if groups:
+        print(f"  sum groups: {len(groups)} target(s) served by 2+ rows")
+        for target, ids in groups.items():
+            print(f"    {target} <- " + ", ".join(ids))
+
+    return dest
```

### Why

1. **Eliminates Output File Collision:** The legacy `map.py` hardcoded the destination as `data/mapped/<cik>_<stmt>.json`. When processing 10 years of history, every year would overwrite the previous year, leaving only 1 year on disk. `map_statement` includes `actual_date` in the destination path (`data/mapped/<cik>_<date>_<stmt>.json`), allowing all 10 years to coexist.
2. **Enables Batch Processing:** Factoring the mapping pipeline out of `main()` turns it into a reusable programmatic building block that can be called in a loop across all 10 historical years.
3. **Caches System Prompt & Few-Shot Assembly:** In a 10-year historical run (30 statements total), rebuilding `system_prompt` from disk 30 times adds unnecessary I/O overhead. By accepting an optional pre-computed `system_prompt`, batch processing can construct the prompt once and reuse it across all 30 Ollama calls.
4. **Returns Destination Path:** Returning `Path` enables downstream stages (e.g. valuation, reconciliation, or feature extraction) to chain directly onto the generated artifacts.

---

## `main`

### Diff

```diff
--- a/src/llm/map.py
+++ b/src/llm/map.py
@@ -630,71 +738,59 @@ def main() -> int:
 def main() -> int:
-    """CLI entry point: maps one statement through Ollama, writes the output."""
+    """CLI entry point: maps statement(s) through Ollama, writes output."""
     
     # ── CLI config ───────────────────────────────────────────────────────────
     parser = argparse.ArgumentParser(
         description="Map a convert.py output via Ollama."
     )
-    parser.add_argument("cik", help="Companie's EDGAR CIK, e.g. 104169")
+    parser.add_argument("cik", help="Company's EDGAR CIK, e.g. 104169")
     parser.add_argument(
-        "stmt", choices=STATEMENTS, help="statement: (is | bs | cf)"
+        "stmt", nargs="?", default=None, choices=STATEMENTS,
+        help="statement: (is | bs | cf)"
     )
+    parser.add_argument(
+        "--date", default=None, help="specific report date (YYYY-MM-DD)"
+    )
+    parser.add_argument(
+        "--all", action="store_true",
+        help="map all converted historical statements for this CIK"
+    )
     parser.add_argument(
         "--url", default=DEFAULT_URL,
         help=f"Ollama base URL (default {DEFAULT_URL})"
     )
     parser.add_argument(
         "--model", default=DEFAULT_MODEL,
         help=f"model name (default {DEFAULT_MODEL})"
     )
     parser.add_argument(
         "--out", default=None,
-        help="output path (default data/mapped/<cik>_<stmt>.json)"
+        help="output path (default data/mapped/<cik>_<date>_<stmt>.json)"
     )
     args = parser.parse_args()
 
     # ── Pipeline ─────────────────────────────────────────────────────────────
     cik = normalize_cik(args.cik)
-    stem = f"{cik}_{args.stmt}"
-    src = CONVERTED_DIR / f"{stem}.json"
-    out = Path(args.out) if args.out else MAPPED_DIR / f"{stem}.json"
-    
-    # If the converted file does not exist.
-    if not src.is_file():
-        print(
-            f"error: {src} not found — run "
-            f"uv run src/llm/convert.py {cik} {args.stmt} first",
-            file=sys.stderr
-        )
-        return 1
-
-    # Build system prompt with examples.
-    records = load_records(src)
-    system_prompt = build_system_prompt()
-
-    # ── Ollama call ──────────────────────────────────────────────────────────
-    started = time.monotonic()
-    body = call_ollama(system_prompt, records, args.url, args.model)
-    decisions = parse_response(body)
-    by_id = validate_decisions(records, decisions)
-    merged = merge_records(records, by_id)
-
-    out.parent.mkdir(parents=True, exist_ok=True)
-    out.write_text(
-        json.dumps(merged, indent=2, ensure_ascii=False)
-        + "\n", encoding=ENCODING
-    )
-    elapsed = time.monotonic() - started
-    
-    mapped = sum(1 for rec in merged if rec["target"] is not None)
-    groups = sum_groups(merged)
-    print(
-        f"mapped {stem}: {len(merged)} records "
-        f"({mapped} mapped, {len(merged) - mapped} unmapped) "
-        f"in {elapsed:.1f}s -> {out}"
-    )
-    if groups:
-        print(f"  sum groups: {len(groups)} target(s) served by 2+ rows")
-        for target, ids in groups.items():
-            print(f"    {target} <- " + ", ".join(ids))
+
+    if args.all:
+        stmts = [args.stmt] if args.stmt else STATEMENTS
+        system_prompt = build_system_prompt()
+        for s in stmts:
+            for p in sorted(CONVERTED_DIR.glob(f"{cik}_*_{s}.json")):
+                rep_date = p.name.split("_")[1]
+                map_statement(
+                    cik, s, report_date=rep_date,
+                    url=args.url, model=args.model,
+                    system_prompt=system_prompt,
+                )
+        return 0
+
+    if not args.stmt:
+        parser.error("the following arguments are required: stmt (or use --all)")
+
+    map_statement(
+        cik, args.stmt, report_date=args.date,
+        url=args.url, model=args.model,
+        out=Path(args.out) if args.out else None,
+    )
     return 0
```

### Why

1. **Parity with `convert.py` CLI:** Users can invoke `map.py` using the same intuitive arguments (`--date` and `--all`) supported by `fetch_edgar_10k.py` and `convert.py`.
2. **Single-Command 10-Year Processing:** Passing `--all` automatically maps all 30 statements (10 years $\times$ 3 statement kinds) sequentially with a single terminal command:
   ```bash
   uv run src/llm/map.py 0001652044 --all
   ```
3. **Optional Statement Argument in Batch Mode:** Making `stmt` optional (`nargs="?"`) allows `--all` to run across all statements (`is`, `bs`, `cf`) by default, while still allowing the user to restrict batch mapping to a single statement kind if desired (e.g. `uv run src/llm/map.py 0001652044 is --all`).
4. **Performance Efficiency:** In `--all` mode, `build_system_prompt()` is executed once upfront and passed to every invocation of `map_statement()`, preventing redundant file reads and prompt interpolations.
