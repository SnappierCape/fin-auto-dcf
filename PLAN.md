# Probabilistic DCF Pipeline — Project Blueprint

**Status:** Design phase
**Owner:** Ulrico Luigi Nava
**Scope of this doc:** Architecture, data decisions, engine design, open problems. This is the reference document for build decisions and collaborator onboarding.

---

## 1. Objective

Given a stock ticker, return a probability distribution of intrinsic value per share, derived from:
1. LLM-based reclassification of raw financial statements into a fixed canonical schema
2. A DCF engine built on the normalized data
3. Monte Carlo simulation over correlated, constrained assumption draws

Output is not a point estimate. Output is a distribution: median fair value, percentile bands, and probability that fair value exceeds current market price.

**Non-goals (v1):** relative valuation (comps), M&A/LBO modeling, banks/insurers/financials (different statement structure, excluded entirely), non-US listings, real-time pricing.

---

## 2. Pipeline Architecture

```
[Ticker] 
   → Stage 1: Ingestion (raw statements, 10Y history)
   → Stage 2: LLM Reclassification (raw labels → canonical schema)
   → Stage 3: Validation Layer (accounting identity checks)
   → Stage 4: Historical Parameter Estimation (means, stdevs, historical ratios)
   → Stage 5: Assumption Sampling (correlated Monte Carlo draws)
   → Stage 6: Constraint Filter (reject/redraw invalid scenarios)
   → Stage 7: DCF Calculation per scenario
   → Stage 8: Aggregation & Output (distribution, percentiles, flags)
```

Each stage should be a separable module with a defined I/O contract (JSON in, JSON out), not a monolithic script. This matters because Stage 2 (LLM) will need the most iteration and you don't want to couple it to Stage 7.

---

## 3. Data Layer

### 3.1 Sources (decided)
| Data | Source | Notes |
|---|---|---|
| Raw financial statements | SEC EDGAR (XBRL) | US-listed only, v1. Free, unlimited. |
| Analyst consensus (optional prior) | FMP free tier | 250 req/day cap — cache aggressively |
| Risk-free rate | FRED API | 10Y Treasury, free |
| Equity risk premium | Damodaran (NYU Stern, monthly CSV) | No API — scheduled scrape/download, store locally |
| Beta | Computed in-house | 3–5yr weekly returns vs S&P 500, via yfinance |
| Sector margin/growth benchmarks | Damodaran industry tables | Same source as ERP |

### 3.2 Undecided — flag for early decision
- **EDGAR parsing approach:** XBRL is structured but inconsistent across filers (custom tags, non-standard `us-gaap` extensions). Decide now: build a custom XBRL parser, or use an existing wrapper (`edgartools`, `python-xbrl`)? Recommend evaluating `edgartools` first — building your own XBRL parser is a distraction from the actual project value-add.
- **Historical depth:** 10 years assumed. Confirm — smaller/younger companies won't have this. Define minimum viable history (recommend: reject tickers with <5 years, flag 5–7 years as "reduced confidence").
- **Statement restatements:** companies restate prior-year figures. Decide: always use the most recently reported value for a given fiscal year, or preserve as-originally-reported values? Recommend: most recent restatement — you want the corrected view.
- **Caching/storage:** flat files (parquet) vs local DB (SQLite/Postgres). Recommend SQLite for v1 — no infra overhead, sufficient for single-user tool.

---

## 4. Canonical Schema

This is a decision you have not made yet and it is foundational — every downstream stage depends on it. Define before writing any LLM prompts.

**Action item:** write out the exact canonical line items now, not during development. Suggested minimum set:

**Income Statement:** Revenue, COGS, Gross Profit, SG&A, R&D, EBIT, Interest Expense, Pre-tax Income, Tax Expense, Net Income

**Balance Sheet:** Cash & Equivalents, Short-term Investments, Total Debt (current + long-term), Total Equity, Net Working Capital components (Receivables, Inventory, Payables)

**Cash Flow:** D&A, Capex, Stock-based Comp, Change in NWC, Free Cash Flow (derived, not extracted)

Keep this schema to ~15–20 items. Every item you add is another mapping target the LLM must classify correctly and another thing that can go wrong. Resist the urge to make it comprehensive.

---

## 5. LLM Reclassification Engine

### 5.1 Model choice (decided)
Not qwen3:35b. Use a smaller model (qwen3:8b class) run through your existing Ollama stack. This is a constrained classification task with a fixed target schema — it does not need a large model, and a smaller model gives you faster iteration during development.

### 5.2 Design (decided)
- Input: full statement block (all raw line items + values for a given fiscal year) as context — not one label at a time. Context lets the model disambiguate items like "Other income" using surrounding structure.
- Output: enforced JSON schema — `{"raw_label": str, "canonical_label": str, "confidence": float}` per line item.
- Structured output enforcement via Ollama's JSON mode / grammar constraints, not prompt-only instruction.

### 5.3 Open problems — not yet solved
- **Confidence threshold policy:** what happens when confidence < some threshold (e.g. 0.7)? Options: (a) auto-flag for manual review, (b) fall back to fuzzy string matching against a lookup table, (c) drop the line item and let the validation layer catch the resulting imbalance. Needs a decision before this is usable unattended.
- **Unmapped items:** raw line items that don't correspond to any canonical bucket (one-time charges, discontinued operations, goodwill impairment). Decide whether these get a canonical "Other/Non-recurring" bucket or are dropped. Dropping silently biases the DCF — recommend a mandatory "Other" bucket that's tracked and reported, not discarded.
- **Multi-year consistency:** the same company may rename a line item across fiscal years (common after a segment restructuring). The LLM has no memory across years unless you explicitly feed it. Decide: process years independently (simpler, risk of inconsistent mapping across years) or pass prior-year mappings as context (more consistent, more complex prompt engineering).
- **Evaluation/testing:** you need a labeled validation set to measure reclassification accuracy before trusting the pipeline. Recommend manually labeling ~20-30 statements across different sectors as a golden test set before building anything downstream of this stage.

---

## 6. Validation Layer

Runs immediately after reclassification, before any parameter estimation.

**Checks (decided, minimum set):**
- Revenue − COGS = Gross Profit
- Gross Profit − SG&A − R&D = EBIT (within tolerance for "Other operating" bucket)
- EBIT − Interest = Pre-tax Income
- Pre-tax Income − Tax = Net Income

**Open problem:** tolerance thresholds. Exact equality will fail constantly due to rounding and the "Other" bucket. Decide a tolerance band (e.g. ±2% of revenue) before this is usable. Statements failing validation should be flagged and excluded from the simulation, not silently forced through.

---

## 7. DCF Engine

### 7.1 Structure (decided)
Standard unlevered FCFF approach:
```
FCFF = EBIT × (1 - tax rate) + D&A - Capex - ΔNWC
```
Discount at WACC, terminal value via Gordon Growth Model, discount back to present, bridge to equity value (subtract net debt), divide by diluted shares outstanding.

### 7.2 Open problems — not yet solved
- **Share count:** diluted or basic shares? Does the model account for stock-based comp dilution and buybacks going forward? Recommend: diluted shares outstanding as of latest filing, with an explicit assumption for share count drift (buyback rate) as one of the sampled parameters — otherwise you're implicitly assuming constant share count, which is wrong for heavy-buyback companies (e.g. most large-cap tech).
- **Valuation date / stub period:** if valuing mid-fiscal-year, do you prorate the current year's partial actuals? Decide the convention now (recommend: always value as of the most recent completed fiscal year-end, ignore stub periods in v1).
- **Projection horizon:** 5 years assumed in your notes. Confirm and decide fade pattern — do growth/margin assumptions converge linearly toward terminal values over the projection window, or stay flat until the terminal year and then jump? Linear fade is more realistic and avoids a discontinuity at the terminal year boundary — recommend building this in from the start.

---

## 8. Monte Carlo & Uncertainty Design

### 8.1 Assumption sources and distributions (decided — see table)

| Parameter | Source | Distribution |
|---|---|---|
| Revenue growth | Historical CAGR + analyst consensus (mean); historical volatility (stdev) | Normal, truncated at -80% |
| EBIT margin | Historical mean/stdev, cross-checked vs Damodaran sector average | Normal, truncated at floor |
| Tax rate | 5yr effective tax rate average | Narrow Normal |
| Capex/Revenue | Historical ratio | Student-t (fat tails — capex is lumpy) |
| ΔNWC/Revenue | Historical ratio | Normal |
| Terminal growth | Macro anchor (developed market GDP growth), NOT company history | Uniform(1%, 3%) or Normal(2%, 0.4%) |
| Risk-free rate | FRED 10Y yield | Point estimate ± small Normal |
| ERP | Damodaran implied ERP | Near-constant, small Normal |
| Beta | In-house regression | Normal(historical_beta, 0.2) |
| Share count drift | Historical buyback/dilution rate | Normal |

### 8.2 Correlation structure (decided — implement in v1, not deferred)
Minimum correlation set via Cholesky decomposition on correlated normals:
- Revenue growth ↔ EBIT margin: ~+0.3
- Revenue growth ↔ Capex/Revenue: ~+0.4
- Terminal growth ↔ Year-5 revenue growth: ~+0.5 (prevents discontinuous terminal assumptions)

**Open problem:** correlation coefficients above are reasonable priors, not estimated. Decide whether to estimate them empirically from a cross-sectional sample of companies (better, more work) or hardcode as fixed priors for v1 (faster, defensible as a documented assumption). Recommend fixed priors for v1, revisit once the pipeline runs end-to-end.

### 8.3 Constraint filter (decided, minimum rule set)
Reject-and-redraw scenarios violating:
- EBIT margin > Gross margin
- Persistent (5yr+) negative FCF for a mature/profitable company
- Capex/Revenue exceeding 2x historical max
- Terminal growth ≥ WACC (hard reject — breaks Gordon Growth Model)
- Terminal EBIT margin outside plausible sector range (Damodaran benchmark ± band)

**Open problem:** rejection rate monitoring. Log rejection rate per ticker run. Threshold not yet defined — recommend treating >25% rejection rate as a signal that the correlation/distribution parameters are miscalibrated for that company (flag for manual review, don't just keep redrawing).

### 8.4 Simulation size
**Open problem — not yet decided:** number of Monte Carlo draws. 10,000 is a reasonable default, but runtime depends on whether DCF calculation per scenario is vectorized (NumPy array ops across all scenarios at once) or looped (slow, avoid). Decide vectorized implementation now — this is a design decision that's expensive to retrofit later.

---

## 9. Output Layer

**Open problem — not yet decided.** Minimum viable output:
- Distribution histogram of fair value per share
- Median, 10th/90th percentile
- Probability(fair value > current market price)
- Rejection rate and flags from constraint filter (transparency, not hidden)

**Undecided:** format. CLI output (fastest to build), Jupyter notebook (good for iteration), or a small web dashboard (better for portfolio/demo purposes, more work). Recommend CLI/notebook for v1, defer dashboard until pipeline logic is proven.

---

## 10. Tech Stack (decided)
- Python, NumPy/Pandas for numerics
- Ollama (existing home server stack) for LLM reclassification
- SQLite for local storage/caching
- `edgartools` (or equivalent) for EDGAR access — confirm after evaluation
- yfinance for beta computation
- matplotlib/plotly for distribution output

---

## 11. Open Decisions — Full List (action items before/during build)

1. XBRL parsing library choice (evaluate `edgartools` first)
2. Minimum historical depth threshold for a valid ticker
3. Restated vs as-originally-reported financials policy
4. Exact canonical schema — write it out completely, freeze it before Stage 2 development
5. LLM confidence threshold policy and fallback behavior
6. Handling of unmapped/non-recurring line items ("Other" bucket policy)
7. Cross-year consistency strategy for LLM mapping
8. Golden validation set — label 20-30 statements manually before trusting the pipeline
9. Accounting identity tolerance bands
10. Share count/dilution treatment (static vs sampled buyback rate)
11. Valuation date convention (stub period handling)
12. Growth/margin fade pattern into terminal year (linear recommended)
13. Correlation coefficients — fixed priors vs empirically estimated
14. Rejection rate threshold for flagging miscalibration
15. Monte Carlo scenario count and vectorization approach
16. Output format (CLI/notebook vs dashboard)
17. Backtesting methodology — how will you know if the model's historical predictions were any good? (Not addressed anywhere yet. Needs a plan: e.g. run the pipeline on 3-year-old data and compare predicted distribution against what the stock actually did.)

Item 17 is the most important gap in the current design. A DCF pipeline that has never been validated against realized outcomes is a nice engineering exercise but has no evidence behind it. Before presenting this project as a portfolio piece, you need at least a basic backtest: pick 10-20 historical tickers, run the pipeline using only data available at a past date, and compare the predicted distribution to what actually happened. This should be planned as a deliverable, not an afterthought.

---

## 12. Build Phases (recommended sequence)

**Phase 1:** Stage 1 + 2 + 3 only. Prove reclassification works reliably on the golden test set (item 8). Do not touch DCF or Monte Carlo until reclassification accuracy is validated.

**Phase 2:** Stage 4-7 without correlation (independent draws, no constraint filter). Get a working end-to-end DCF distribution for one hand-picked, well-understood ticker.

**Phase 3:** Add correlation structure and constraint filter. Re-run same ticker, compare distributions before/after — the tightening should be visible and explainable.

**Phase 4:** Backtest (item 17) on a small basket of historical tickers.

**Phase 5:** Generalize to arbitrary tickers, expand canonical schema edge cases, connect to portfolio tracker (Option A) if pursued.

Do not parallelize these phases. Each depends on the previous one actually working, not just running without errors.