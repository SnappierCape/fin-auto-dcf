# Probabilistic DCF Pipeline — Project Blueprint

**Status:** Design phase
**Owner:** Ulrico Luigi Nava
**Scope of this doc:** Architecture, data decisions, engine design, open problems. This is the reference document for build decisions and collaborator onboarding.

---

## 1. Objective

Given a **US-firm stock ticker** (D22), return a probability distribution of intrinsic value per share, derived from:
1. LLM-based reclassification of raw financial statements into a fixed canonical schema
2. A DCF engine built on the normalized data
3. Monte Carlo simulation over correlated, constrained assumption draws

Output is not a point estimate. Output is a distribution: median fair value, percentile bands, and probability that fair value exceeds current market price.

**Non-goals (v1):** relative valuation (comps), M&A/LBO modeling, banks/insurers/financials (different statement structure, excluded entirely), **non-US listings (US issuers with SEC EDGAR statements only — `PROJECT.md` D22; non-US tickers are a hard rejection, not an edge case)**, real-time pricing.

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
- **Caching/storage:** flat files (parquet/JSON) vs local DB (SQLite/Postgres). Per `PROJECT.md` D20, v1 is file-based (no database); SQLite/Postgres is a post-v1 candidate if ingestion scale demands it.

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
Not qwen3:35b. Use a smaller model (qwen3:8b class) run through your existing Ollama stack. This is a constrained classification task with a fixed target schema — it does not need a large model, and a smaller model gives you faster iteration during development. Per P3/D11 this layer is built and proven as the first engineering phase — the go/no-go for the whole project is recorded against it, before any downstream stage work starts.

### 5.2 Design (decided)
- Input: full statement block (all raw line items + values for a given fiscal year) as context — not one label at a time. Context lets the model disambiguate items like "Other income" using surrounding structure.
- Output: enforced JSON schema — `{"raw_label": str, "canonical_label": str, "confidence": float}` per line item.
- Structured output enforcement via Ollama's JSON mode / grammar constraints, not prompt-only instruction.

### 5.3 Open problems — not yet solved
- **Low-confidence fields:** what happens when confidence < some threshold (e.g. 0.7)? There is **no deterministic fallback path** in this project (D11); the v1 policy is that such fields are hard-flagged and **excluded from the DCF**, the exclusion is reported in output, and repeated low-confidence fields on a given ticker are a rejection signal for that stock. Threshold value still to be decided (Phase 1, 1.1).
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

**Open problem:** tolerance thresholds. Exact equality will fail constantly due to rounding and the "Other" bucket. Decide a tolerance band (e.g. ±2% of revenue) before this is usable (Phase 0, 0.5). Statements failing validation should be flagged and excluded from the simulation, not silently forced through — and, given that reclassification is mandatory with no fallback (D11), repeated validation failures on a ticker are also a rejection signal.

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
- `edgartools` (or equivalent) for EDGAR access — confirm after evaluation
- yfinance for beta computation
- matplotlib/plotly for distribution output

---

## 11. Open Decisions — Full List (action items before/during build)

Each item below is assigned to the phase in §12 where it must be settled.

1. XBRL parsing library choice (evaluate `edgartools` first) — *settled in Phase 2 (2.1)*
2. Minimum historical depth threshold for a valid ticker — *settled in Phase 0 (0.2), applied in Phase 2*
3. Restated vs as-originally-reported financials policy — *settled in Phase 0 (0.3)*
4. Exact canonical schema — write it out completely, freeze it before the LLM stage is built — *settled in Phase 0 (0.1), gates Phases 1–4*
5. LLM confidence threshold / low-confidence field policy (no fallback path exists; flagged-and-excluded per D11) — *settled in Phase 1 (1.1)*
6. Handling of unmapped/non-recurring line items ("Other" bucket policy) — *settled in Phase 1 (1.4)*
7. Cross-year consistency strategy for LLM mapping — *settled in Phase 1 (1.5)*
8. Golden validation set — label 20–30 statements across ≥ 4 sectors from real filings before trusting the pipeline — *settled in Phase 1 (1.3); built as the LLM accuracy gate for the Phase 1 exit; a smaller hand-curated fixture set is built in Phase 0 (0.4)*
9. Accounting identity tolerance bands — *settled in Phase 0 (0.5)*
10. Share count/dilution treatment (static vs sampled buyback rate) — *settled in Phase 3 (3.1)*
11. Valuation date convention (stub period handling) — *settled in Phase 3 (3.2)*
12. Growth/margin fade pattern into terminal year (linear recommended) — *settled in Phase 3 (3.4)*
13. Terminal-g anchor (macro vs industry vs company history) — *settled in Phase 3 (3.5)*
14. Correlation coefficients — fixed priors vs empirically estimated — *settled in Phase 4 (4.2); empirical estimation deferred to post-v1*
15. Rejection rate threshold for flagging miscalibration — *settled in Phase 4 (4.3)*
16. Monte Carlo scenario count and vectorization approach — *settled in Phase 4 (4.4)*
17. Scenario definitions (conservative/central/optimistic) — *settled in Phase 4 (4.5)*
18. **Reclassification go/no-go** — the Phase 1 exit gate: if the LLM stage cannot meet the accuracy bar, halt and iterate; the project does not continue with a degraded reclassifier (D11) — *decided at the end of Phase 1 (1.2)*
19. Output format (CLI/notebook vs dashboard) — *resolved per `PROJECT.md` D19 (CLI-first, no dashboard); dashboard is a post-v1 candidate*
20. Backtesting methodology — how will you know if the model's historical predictions were any good? (Not addressed anywhere yet. Needs a plan: e.g. run the pipeline on 3-year-old data and compare predicted distribution against what the stock actually did.) — *settled in Phase 5 (5.1–5.3)*

**Item 20 is the most important gap in the current design** (a backtest of realized outcomes). **Item 18 is the most important gate in the development sequence**: the LLM reclassification layer is the component whose reliability is not yet proven, it is the project's differentiator, and it is deliberately built as the first engineering phase — if it cannot reach the accuracy bar, the project halts (D11) instead of wasting subsequent phase work on the wrong foundation.

---

## 12. Phased Development Plan (0 → fully implemented)

**Reading this section.** Phases are strictly sequential and cumulative: a phase may be entered only when the previous phase's exit gate is signed off, and each phase's work may begin before its decisions are all settled *only* for the "non-blocking" decisions listed (work on those items proceeds under the stated default, and the decision can be revisited cheaply). "Blocking" decisions gate the phase: implementation of the affected component starts after they are recorded in `PROJECT.md` §3.

**Exit-gate rule.** Each phase ends with a written entry in the phase log appended below (§12.8): date, decisions made (with the option chosen), test results, fixture outputs, and the sign-off line. A gate is passed when the tests named in the phase pass and every decision required by that phase is recorded with an owner.

**Definition of "done" per phase.** A phase is done when: (a) its deliverables exist and pass its named tests; (b) its decisions are recorded; (c) the next phase's input contract is demonstrated with at least one real artifact (not a stub). "Runs without errors" is explicitly *not* done.

**Decisions already taken** (do not re-litigate; see `PROJECT.md` §3): DCF + Gordon terminal value (D1); distribution, not point estimate (D2); MC with correlated draws (D3/D6); parameter hierarchy file → industry prior → heuristic (D7); WACC from CAPM (D8); `terminal_g ≥ WACC` hard reject (D9); LLM confined to reclassification (D10); **LLM reclassification mandatory, no fallback path (D11)**; frozen ~15–20 field schema (D12); provenance on every field (D13); accounting-identity gates (D14); pluggable LLM provider, Ollama default (D15); **US firms only (D22)**; Python/NumPy vectorized (D16); seeded determinism (D17); provenance + confidence in every output (D18); CLI-first (D19); no DB for v1 — file-based (D20); stage-as-module architecture (D21).

---

### Phase 0 — Foundation (repo, contracts, golden fixtures)

**Scope.** Repo skeleton, module layout per D21 (one package per pipeline stage), CI (lint + test on every commit), and the **frozen canonical schema as a machine-readable artifact** (§4 item: exact field list, names, units, provenance values). Golden fixtures for Stage 3–4: ≥ 2 companies' hand-curated 5-year statements in canonical JSON, with expected values hand-computed.

**Input.** Repo, `PROJECT.md`, this plan. **Output.** Repo layout, `schema.json` + dataclass mirror, CI green, ≥ 2 golden fixtures, one end-to-end *empty* pipeline (stub modules wired, `run(ticker)` returns a structured "not implemented" per stage).

**Decisions required in this phase:**

| # | Decision | Blocking? | Options / default | Deadline |
|---|---|---|---|---|
| 0.1 | Canonical schema: exact field list (~15–20), names, units, derivation rules for computed fields | **Blocking** (gates Phases 2–4) | Draft now from §4 minimum set; freeze | Before any stage code |
| 0.2 | Minimum historical depth | Non-blocking (gates Phase 4) | `<5y` hard reject / `5–7y` reduced-confidence flag | Before Phase 4 |
| 0.3 | Restated vs as-originally-reported values | Non-blocking (affects Phase 2 ingestion) | Default: most recent restatement | Before Phase 2 |
| 0.4 | Fixture universe: which ≥ 2 companies for golden sets (pick 1 stable-industry, 1 growth/tech) | Non-blocking | Pick in this phase; both must have ≥ 5y clean history | Before Phase 2 |
| 0.5 | Accounting-identity tolerance band | **Blocking** (gates Stage 3) | Default: ±2% of revenue, re-tune after first real runs | Before Phase 3 exit |

**Exit gate.** CI green; `schema.json` committed and referenced by every stage's I/O contract; ≥ 2 golden fixtures with hand-verified expected outputs; module boundaries demonstrable (each stage callable standalone in a test).

---

### Phase 1 — LLM reclassification (the go/no-go gate)

**Why first.** LLM reclassification is the project's differentiator (P2) and the one stage whose reliability is not yet proven. D11 makes it mandatory in every v1 run — there is no fallback reclassification path. So it is built and proven *before* any DCF or MC work: if the LLM cannot reach the accuracy bar on a labeled set of real US-firm filings, the project halts and the premise is revisited (P3/D11) — and zero time has been spent on the estimation, DCF, or MC layers.

**Scope.** Stage 2 LLM reclassification per §5: Ollama small model (§5.1), full-statement-block input, JSON-mode output, confidence score per mapped field, against the frozen schema of Phase 0. A statement is "correctly classified" when every canonical field required by the schema is mapped with confidence ≥ threshold (1.1), no canonical field is mapped to a wrong bucket, and no hallucinated field appears.
US-firm scope per D22: all test material is real SEC EDGAR 10-K/10-Q filings from US issuers (non-US filings never enter this project).

**Input.** Phase 0 artifacts: frozen `schema.json`, golden fixtures (0.4). **Output.** Working reclassification stage on ≥ 2 fixture companies; accuracy report against the labeled golden set; per-field confidence scores and provenance tags in the output.

**Decisions required in this phase:**

| # | Decision | Blocking? | Options / default |
|---|---|---|---|
| 1.1 | Low-confidence field policy (no fallback path exists, D11): threshold + treatment of sub-threshold fields | **Blocking** (stage contract) | Threshold default 0.7 per field; sub-threshold field hard-flagged and excluded from the DCF; exclusion reported in output; ≥ N such fields → ticker rejected. N decided in this phase |
| 1.2 | **Go/no-go on the LLM layer** | **Blocking** (gates Phases 2–6) | GO: golden set ≥ 90% field coverage, ≥ 95% correct mappings, no hallucinations, confidence spread sane — proceed. NO-GO: iterate prompt/model/schema and re-test; a third failed attempt → stop the project and revisit P3 (decision owner) |
| 1.3 | Golden test set (the accuracy gate): labeling protocol — which statements, sectors, who labels, how correctness is defined | **Blocking** | ~20–30 statements (≥ 2 years each of the Phase 0 fixture companies + ≥ 4 sectors across additional US issuers), labeled by project owner (Snapp), accuracy criterion = 1.1/1.2 above |
| 1.4 | Unmapped/non-recurring line-item policy | **Blocking** (stage completeness) | Default: mandatory `Other` bucket, tracked and reported, never silently dropped |
| 1.5 | Cross-year consistency: process years independently vs. pass prior-year mappings as context | Non-blocking | Default: years independent in v1; flag as known limitation |
| 1.6 | Model choice: which Ollama model (≤ 8B class per §5.1) | Non-blocking (subject to 1.2 bar) | Default: qwen3:8b or equivalent; upgrade/downgrade only as a remedy to a 1.2 NO-GO |

**Entry criteria.** Phase 0 gate passed. **Exit gate.** 1.3 golden set labeled; 1.2 GO recorded in `PROJECT.md` §3 with the measured accuracy; 1.1 policy implemented and demonstrated in output (sub-threshold fields visible, excluded); confidence scores present on every LLM-mapped field; no hallucinated fields (test: mapping a statement must never produce canonical fields that were not in the raw statement).

---

### Phase 2 — Data ingestion

**Scope.** Stage 1: real EDGAR ingestion (XBRL) of the raw statement blocks that feed the (already proven) Phase 1 LLM reclassifier. US issuers only (D22): ticker resolution rejects non-US / non-EDGAR tickers as a hard reject, not a warning. Minimum-history gate (§3.2), market data (current price, diluted share count), end-to-end `run(ticker)` = ingest → LLM reclassify → canonical JSON.

**Input.** Phase 0 + Phase 1 (proven LLM stage). **Output.** `run(ticker)` → canonical schema JSON + confidence report for ≥ 2 **real US tickers** (one stable-industry, one growth/tech), no hand-curated fixtures needed.

**Decisions required in this phase:**

| # | Decision | Blocking? | Options / default |
|---|---|---|---|
| 2.1 | EDGAR access: `edgartools` vs. `python-xbrl` vs. custom XBRL parser | **Blocking** (ingestion build) | Default: evaluate `edgartools` first (§3.2) |
| 2.2 | Historical depth floor | Non-blocking (0.2 default) | `<5y` hard reject; `5–7y` reduced-confidence flag |
| 2.3 | Market data source (price, share count) | Non-blocking | Default: yfinance; cache locally per D20 |
| 2.4 | "Primary" real ticker set (≥ 2, different industries) | Non-blocking | One stable-industry, one growth/tech US issuer (aligns with D22) |

**Entry criteria.** Phase 1 GO recorded. **Exit gate.** End-to-end run on ≥ 2 real US tickers: ingestion → LLM reclassification → canonical schema output, fully provenance-tagged; non-US ticker input produces a clean hard rejection; ingestion failure modes (missing year, restatement, non-GAAP label) handled and surfaced.

---

### Phase 3 — Parameter estimation + DCF engine (no MC yet)

**Scope.** Stage 4 (historical parameter estimation: means, stdevs, CAGR, capex/NWC ratios, effective tax rate) over the reclassified statements; Stage 7: FCFF projection (5-yr horizon, fade per §7.2), Gordon terminal value, EV → equity bridge, per-share FV — deterministic, single path on median-path inputs. Includes the `terminal_g ≥ WACC` hard reject (D9) and accounting-identity validation (Stage 3, tolerance 0.5).

**Input.** Phase 2 canonical outputs. **Output.** Per ticker: parameter-estimation report (all values provenance-tagged) + single-path FV with full bridge breakdown.

**Decisions required in this phase:**

| # | Decision | Blocking? | Options / default |
|---|---|---|---|
| 3.1 | Share count treatment: static diluted vs. sampled buyback/dilution drift | **Blocking** (bridge inputs) | Default: static diluted as of latest filing; buyback drift deferred to Phase 4 |
| 3.2 | Valuation date convention (stub handling) | **Blocking** | Default: latest completed fiscal year-end, no stubs in v1 |
| 3.3 | Parameter estimation set: which ratios (revenue CAGR, EBIT margin, Capex/Rev, ΔNWC/Rev, effective tax) and windows (3y vs 5y) | Non-blocking | Default: 5y windows, 3y fallback when history is short |
| 3.4 | Fade pattern: linear vs step (flat then jump at terminal); projection horizon (3y vs 5y) | **Blocking** (structure) | Default: 5y horizon, linear fade over projection window |
| 3.5 | `terminal_g` anchor: macro (GDP) vs. industry vs. company history | **Blocking** (D1 anchor per D7) | Default: macro (developed-market GDP growth ~2%), range `1–3%`; company history rejected as anchor |

**Entry criteria.** Phase 2 gate passed. **Exit gate.** Parameter-estimation outputs match hand-computed values for ≥ 2 fixture companies (0.4); DCF on ≥ 2 companies matches hand-computed single-path FV within tolerance (0.5 band); hard-reject path (`terminal_g ≥ WACC`) demonstrated in a test; full bridge (EV → equity → per-share) present in output with provenance.

---

### Phase 4 — Monte Carlo layer (distribution, constrained, correlated)

**Scope.** Stage 5 (assumption sampling per §8.1 distribution table), Stage 6 (constraint filter + reject-and-redraw, rejection-rate logging), Stage 7 vectorized multi-scenario mode (D16/D17), Stage 8 (aggregation: median, p10/25/50/75/90, P(FV > price), expected FV).

**This phase converts point estimates into distributions — the actual product.**

**Input.** Phase 3 deterministic engine over Phase 2 ingestion. **Output.** Full distribution output per real ticker: percentile bands, P(FV > price), per-assumption rejection rates, scenario breakdown (conservative/central/optimistic per D4).

**Decisions required in this phase:**

| # | Decision | Blocking? | Options / default |
|---|---|---|---|
| 4.1 | Distribution table per §8.1 — confirm all 9 parameters + their distributions | **Blocking** (MC layer) | Already drafted in §8.1; confirm or amend before coding |
| 4.2 | Correlation structure: fixed heuristic matrix vs. estimate from historical data | **Blocking** (D6) | Default: fixed heuristic matrix (revenue↔margin ~0.3, revenue↔capex ~0.4, terminal_g↔terminal-year growth ~0.5), documented as `heuristic` provenance; empirical estimation deferred to post-v1 |
| 4.3 | Rejection-rate threshold: what % triggers a "mis-specified" flag | **Blocking** | Default: >25% rejection → flag for review; >80% → hard fail the run |
| 4.4 | MC draw count: N (default 10,000) and vectorization approach | Non-blocking (N); **vectorization is blocking** (D16) | Default: N=10,000; vectorized NumPy across all scenarios simultaneously |
| 4.5 | Scenario definitions (conservative/central/optimistic): how assumption ranges are set per scenario | **Blocking** (D4) | Default: scenario = perturbation of assumption centers (e.g. −1σ / 0 / +1σ on growth and margin) with per-scenario constraint ranges |

**Entry criteria.** Phase 3 gate passed. **Exit gate.** MC distribution on ≥ 2 real US tickers; rejection rates < 25% on at least one ticker; P(FV > price) computed and reported; scenario breakdown present; distribution matches theoretical expectations (e.g. wider band on higher-σ assumptions); determinism: two runs with same seed ⇒ byte-identical output.


---

### Phase 5 — Backtest (validation against realized outcomes)

**This is O1 — the difference between a working pipeline and a validated one.**

**Scope.** Historical backtest: select 10–20 US tickers (D22) with ≥ 3 years of history behind a chosen past date; run the pipeline using only data available at that date (point-in-time); compare the predicted distribution (median, P(FV > price) at the past price) against what the stock actually did over the following 1–3 years. Report: calibration (did P(FV > price) correlate with realized outperformance?), rank correlation of predicted FV vs. realized price change, and a per-sector breakdown.

**Input.** Phases 1–4 (full pipeline). **Output.** Backtest report: per-ticker table (predicted median FV, P(FV > price), realized price change, hit/miss), calibration curve, written conclusion on model quality and known failure modes.

**Decisions required in this phase:**

| # | Decision | Blocking? | Options / default |
|---|---|---|---|
| 5.1 | Backtest universe: which US tickers (D22), what past date, what forward window | **Blocking** | Default: 15 US tickers across 4 sectors, past date = 3 years ago, forward window = 3 years |
| 5.2 | Point-in-time data discipline: which API/version to ensure no future data leaks | **Blocking** | Use as-of-date query on EDGAR; cache at run time, not at backtest time |
| 5.3 | Success metric: what "validated" means numerically (e.g. rank correlation ≥ 0.4, calibration slope 0.7–1.3) | **Blocking** | Define before running; record in PROJECT.md |
| 5.4 | Handling of backtest failures (tickers where pipeline hard-rejects, insufficient history) | Non-blocking | Log and exclude, but report the exclusion rate |

**Entry criteria.** Phase 4 gate passed. **Exit gate.** Backtest report written; success metric (5.3) evaluated and recorded; backtest artifacts (per-ticker JSON + summary) committed; conclusion states whether the pipeline is validated, conditionally validated, or not validated for the given universe.

---

### Phase 6 — Generalization, hardening, v1 release

**Scope.** Robustness pass (edge cases: banks/insurers excluded cleanly, negative-earnings US companies, very young companies), documentation (README with run instructions, pipeline diagram, decision log), final acceptance test per `PROJECT.md` §8 for ≥ 3 US companies across ≥ 2 industries, performance optimization (vectorization verification, MC runtime < acceptable threshold for N=10k), and the release tag.

**Input.** Phases 1–5. **Output.** `v1.0.0` tag, README, acceptance test report, performance profile.

**Decisions required in this phase:**

| # | Decision | Blocking? | Options / default |
|---|---|---|---|
| 6.1 | Exclusion policy for non-applicable companies (banks/insurers/REITs): hard reject vs. reduced confidence | **Blocking** | Default: hard reject (different statement structure, excluded per non-goals) |
| 6.2 | Acceptable runtime: what MC runtime is "acceptable" for N=10k | Non-blocking | Default: < 60s end-to-end per ticker on a 3090-class machine; measure and record |
| 6.3 | Final acceptance: sign-off against `PROJECT.md` §8 all 8 criteria | **Blocking** | All 8 criteria must pass for ≥ 3 companies / ≥ 2 industries |

**Entry criteria.** Phase 5 gate passed (backtest report exists; conclusion recorded). **Exit gate.** `v1.0.0` tag; all §8 acceptance criteria pass and are recorded in the phase log; README accurate and current.

---

### Phase 7 (post-v1, out of scope for this plan)

- Multi-company comparative analytics / portfolio mode (PROJECT.md §5, explicitly deferred).
- Empirical correlation estimation (deferred from Phase 4).
- GUI / dashboard (D19 explicitly rejects for v1; candidate post-v1).
- Real-time / streaming data (D20 rejects for v1; candidate post-v1).
- Extended statement coverage (non-GAAP, segment-level, quarterly).
- Cross-border / non-US listings (excluded per non-goals, D22 — US-only is a v1 scope decision, not a permanent limit).

Each item has its own decision set and must be re-scoped as a new phase before work starts.

---

### §12.8 Phase log

Append an entry for each phase as it is entered and exited. Format:

```
### Phase N — <name>
- Entered: <date>  Sign-off: <name>
- Decisions made: <list with option chosen, and the alternative considered>
- Test results: <pass/fail count, notable failures>
- Artifacts: <commit / file paths>
- Exit gate: PASSED / FAILED — <reason if failed>
```