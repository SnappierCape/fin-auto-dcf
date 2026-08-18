# Probabilistic DCF Pipeline — Project Document

**Owner:** Ulrico Luigi Nava \
**Status:** Design phase \
**Scope:** Project goal, design, premises, decisions, mechanics, open questions, and the phased build plan. Single source of truth.

## 1. Objective

Given a **US-firm stock ticker** (D20), return a **probability distribution of intrinsic value per share** — not a point estimate — derived from:

- Historical financial statements pulled from **SEC EDGAR (iXBRL)**
- **LLM reclassification** of those statements into a fixed canonical schema
- A **discounted cash flow (DCF)** model
- **Monte Carlo simulation** over the DCF inputs → the fair-value distribution

**Non-goals (v1):** comps/relative valuation, M&A/LBO, banks/insurers/financials, **non-US issuers (D20)**, real-time pricing.

## 2. Why a Distribution, Not a Number

A classic DCF collapses 10 assumptions into 1 number: garbage in, garbage out. This project produces a **distribution** instead — 10 assumptions with plausible standard deviations → **median, p10, p90, P(value > price)**.

- **Scenario DCF as a point-generator:** each named scenario (base / optimistic / pessimistic) yields a median + spread.
- **Monte Carlo** draws correlated parameter sets and produces the full fair-value distribution per scenario.

The scenario DCF stays an explicit point-generator per named scenario; the MC runs are the full distribution.

## 3. Design Premises

| # | Premise |
|---|---------|
| P1 | **Determinism and auditability over cleverness.** Same input + same seed → same output. Every output traceable to a specific assumption.  ⚠️ This may not be possible for the llm phase ⚠️ |
| P2 | **The LLM reclassification stage is the differentiator.** Reading issuer iXBRL and mapping it to a canonical schema is where the project's value — and its main technical risk — lives. |
| P3 | **The LLM reclassification stage is the foundation — built first, proven first, and mandatory.** Its reliability cannot yet be verified, so it is engineered and validated against a labeled set of real US-firm filings **before any DCF or Monte Carlo work** (Phase 1, §12). If it cannot reach the accuracy bar, the project **halts** and the premise is revisited. There is **no deterministic or rule-based fallback reclassification path** in v1 (D8). |
| P4 | **Research tool, not advisor.** Outputs are labeled "research input"; never investment-advice framing. |
| P5 | **Small model, constrained task.** Runs on a mid-size local model via Ollama (D14) — self-hosted, reproducible, cheap. |

## 4. Decisions (authoritative — do not re-litigate)

- **D1.** DCF + terminal alpha-blended **Gordon growth** and **Exit multiple** (terminal g strictly below WACC).
- **D2.** Output is a **distribution**: median, p10, p90, P(intrinsic > price).
- **D3.** **Monte Carlo** with correlated draws (Cholesky), **exactly 10,000 iterations per scenario — a named constant (D27)**; 5 reject-and-redraw rules with visible rejection stats.
- **D4.** **Parameter hierarchy, enforced:**
  1. **Hard constraints** — WACC ≥ 0; g_terminal < WACC (strict); shares > 0; debt ≤ assets + equity; non-negative where physically required (revenue, COGS, taxes) → **hard reject on violation** (D9).
  2. **Range bounds** — each parameter has `[lo, hi]`; out-of-bounds draws are clamped and counted. >10% clamps → warning; >25% → hard reject.
  3. **Scenario defaults** — base/optimistic/pessimistic anchor the center of each draw (D19).
- **D5.** Scenario DCF = explicit point-generator per named scenario; MC runs = full distribution.
- **D6.** **Correlation structure: hardcoded named constants** (Cholesky), visible in every run with counts (D26).
- **D7.** **Provenance on every assumption:** `source ∈ {estimated, user, llm, heuristic}`, `seed`, `range`, `confidence` (0–1), `model`, `field`. No assumption enters without full provenance.
- **D8.** **LLM reclassification is the core** (P2) — the main value-add and main technical risk. **Mandatory in every v1 run** — a deterministic or rule-based fallback reclassification path is **explicitly rejected and out of scope.** If the stage cannot meet the Phase 1 accuracy gate the **project halts** and the premise is revisited.
- **D9.** **Hard-reject rules are non-negotiable:** g ≥ WACC → reject; shares ≤ 0 → reject; negative COGS / negative revenue (non-fintech) → reject; `confidence < 0.3` on a core field (revenue, net income, total assets) → reject with reason.
- **D10.** **JSON-first I/O:** input = JSON file (ticker + optional scenario overrides); output = structured JSON (distributions, assumptions, provenance, scenario breakdown).
- **D11.** **Canonical schema is versioned and frozen before LLM work begins** (Phase 0); additive-only changes after. The LLM prompt and tests are written against the frozen schema.
- **D12.** **No hidden state.** Every derived field carries `derived_from` + `rule_version`; recomputation from raw inputs must always be possible.
- **D13.** **Python 3.12 + NumPy.** No ML framework beyond Ollama's client; no pandas; no web framework.
- **D14.** **v1 LLM backend: local Ollama only, model-agnostic.** Thin provider-agnostic interface (OpenAI-compatible protocol) wraps Ollama so a different local model/server can be swapped without touching pipeline logic. **No cloud APIs in v1.** Default model: qwen3:8b class; prompt, schema, and accuracy bar are written model-agnostic.
- **D15.** **CLI only for v1.** Single entry point with `parse` / `run` / `report` subcommands. No web UI, no API server.
- **D16.** **Flat files, no database in v1.** Statements and outputs are JSON under a local data dir (e.g. `data/{ticker}/fiscal-{YYYY}.json`). No SQLite, no Postgres — a DB is explicitly **post-v1**.
- **D17.** **Deterministic by default:** `--seed` honored; all stochastic behavior seeded.
- **D18.** **License: Apache 2.0.** Model weights are not redistributed; only prompts + pipeline code are licensed.
- **D19.** **Scenario presets:** base / optimistic / pessimistic, each a JSON object of midpoints + ranges.
- **D20. US firms only, in v1.** Ticker must be a US issuer with statements on SEC EDGAR (XBRL). Non-US listings are out of scope → hard rejection.
- **D21. History depth:** 10 fiscal years when available; otherwise use available history, **but never fewer than 5.** Fewer than 5 usable years → hard reject (insufficient history), reason surfaced.
- **D22. Use restated figures when available** (as-originally-reported only when a year has no restated statement).
- **D23. Non-canonical items → `Other`.** Items that do not map to a canonical field go into the first-class `Other` bucket (D11) — never a failure, never silently dropped.
- **D24. LLM validation:** back-testing of the reclassification stage is done against a **frozen set of manually reclassified statements** (the golden set, Phase 0 item 0.4).
- **D25. Share count: diluted shares outstanding, as reported.**
- **D26. Correlations between DCF inputs: hardcoded named constants (Cholesky) in v1** — not fitted, not user-tunable.
- **D27. Monte Carlo iterations: exactly 10,000 per scenario**, named constant, not tunable in v1.
- **D28. Fiscal-year policy (v1): stub periods are ignored; the valued period is the last completed fiscal year.** Partial years excluded.
- **D29. Scenario realism filter (core feature).** During MC extraction, unrealistic scenarios are **discarded before** any DCF evaluation — or over-weighted only above a set severity bar. This keeps every MC draw economically coherent: e.g. a **decreasing profit margin co-occurring with a capex spike in the same year** is a contradictory draw and is rejected (not merely penalized). The filter is a set of named, hardcoded rules with **visible rejection counts** (D6/D26 style) printed on every run; thresholds are fixed constants in v1.

## 5. LLM Reclassification Stage — Core, Proven First

**The most technically demanding part of the project — and the first thing built and validated** (Phase 1, §12; P3/D8).

### 5.1 Model choice (decided)

Not qwen3:35b. A mid-size local model (qwen3:8b class) through the existing Ollama stack, behind the thin model-agnostic client (D14). This is a constrained classification task with a fixed target schema — it does not need a frontier model. Running locally is the point: reproducible, offline, cheap (P5).

### 5.2 Inputs

Per-statement iXBRL blocks, the frozen canonical schema (D11), and the schema description in the system prompt. No retrieval augmentation in v1.

### 5.3 Outputs, per statement

```json
{
  "canonical_map": { "<schema_key>": { "raw_key": "...", "confidence": 0.0-1.0, "transform": "renamed|reordered|reclassified|other" } },
  "other_bucket": [ "raw_key_1", "raw_key_2" ],
  "confidence_summary": { "high": 12, "medium": 3, "low": 1 }
}
```

**Decided policy (old open question closed):**
- **Non-canonical / low-confidence items:** no deterministic fallback. **Non-canonical items are classified into `Other` (D23).** A genuinely canonical field that comes back low-confidence is either hard-rejected (core fields, D9) or flagged below.
- **Confidence bands:** `< 0.3` on a core field → **hard reject** (D9); `0.3–0.7` → emitted but **flagged** in provenance and surfaced in the report; `≥ 0.7` → clean acceptance.
- **Hallucinated fields:** any key not in the frozen schema → hard reject (D9) — the anti-hallucination gate.

### 5.4 Identity / reconciliation checks (the permanent fixture of the project, D24)

Every statement in the golden + holdout set — and every reclassified statement on every run — must satisfy the **12 identity / reconciliation checks of §6.7**, including:

- Σ (classified items) = total revenue — within tolerance `tol_rev`
- Σ (classified items) + `Other` = total as reported — within `tol_total`
- No classified item appears twice
- Every non-canonical item lands in `Other` (D23)
- plus the rest of the §6.7 list: the profit-ladder identities, the asset-side subtotals, the balance-sheet identity (total assets = total liabilities + equity), the CFS cash roll-up, and the **Δcash (CFS) = Δcash (BS) headline reconciliation**

Tolerance bands are fixed in **Phase 0 (0.5)**; failures on a golden fixture are hard rejects (D9), not warnings.

## 6. Canonical Schema (frozen — D11)

The reclassification stage (§5) maps every issuer's iXBRL statement **onto exactly this structure**. It is the contract that Phase 0 freezes (0.3): the LLM stage, the DCF kernel (§8) and the MC layer (§9) all read the same keys. Non-canonical items map into the `Other` line of the relevant block (D23) — never dropped, never new keys (D9). A line type of **subtotal** is a computed sum of its block's lines; **computed** values (§6.8) are derived, never reclassified.

**Label history (2026-08-18, accepted):** `Net interest expense` (was "Interest balance"); `Other non-operating income/(expense)` (was "Non operating balance"). The Liabilities & Equity block below is the canonical label set as specified 2026-08-18 (`ST debt`, `A / P`, `Lease obligations`, `Non controlling interest` — US-labeling kept verbatim).

### 6.1 Income statement

| Line | Type | Notes |
|---|---|---|
| Revenue | line | core field — hard-reject rules (D9) |
| Cost of revenue (incl. D&A) | line | — |
| Cost of revenue (excl. D&A) | line | — |
| Depreciation & amortization | line | feeds EBITDA (§6.8), CFO add-back, capex cross-check |
| Other | line | cost-of-revenue family |
| Gross profit | subtotal | = Revenue − Cost of revenue (incl. D&A) − Other (cor) (§6.7) |
| R&D | line | — |
| SG&A | line | — |
| Other | line | operating-expense family |
| EBIT | subtotal | = Gross profit − R&D − SG&A − Other (opex) |
| **Net interest expense** | line | *label fix 2026-08-18* (was "Interest balance") |
| **Other non-operating income/(expense)** | line | *label fix 2026-08-18* (was "Non operating balance") |
| Other | line | non-operating family |
| EBT | subtotal | = EBIT ± non-operating block |
| Income tax | line | — |
| Other items after tax | line | — |
| Net consolidated profit | line | — |
| Minority interest | line | — |
| Net profit (attributable) | line | core field (D9) |
| Basic shares outstanding | line | — |
| Diluted shares outstanding | line | core (D25) |
| Basic EPS | line | reported line; cross-checked against §6.8 when the issuer reports it |
| Diluted EPS | line | reported line; cross-checked against §6.8 when the issuer reports it |

### 6.2 Balance sheet — Current assets

| Line | Type | Notes |
|---|---|---|
| Cash investment | line | — |
| Cash & equivalents | line | core asset family |
| ST investments | line | — |
| Trading assets | line | — |
| A / R | line | accounts receivable (US label kept) |
| Inventory — Raw materials | line | — |
| Inventory — Work in progress | line | — |
| Inventory — Finished goods | line | — |
| Inventory — Other | line | — |
| Current assets (total) | subtotal | — |

### 6.3 Balance sheet — Non-current assets

| Line | Type | Notes |
|---|---|---|
| Net PP&E | line | — |
| Gross PP&E | line | — |
| Accumulated depreciation | line | — |
| Goodwill | line | — |
| Intangible assets | line | — |
| Other | line | — |
| Non-current assets (total) | subtotal | — |
| **Total assets** | subtotal | = current + non-current (§6.7) |

### 6.4 Balance sheet — Liabilities & Equity

| Block | Line | Type | Notes |
|---|---|---|---|
| Current liabilities | ST debt | line | interest-bearing (current portion) |
| Current liabilities | A / P | line | US label kept |
| Current liabilities | Dividends payable | line | — |
| Current liabilities | Tax payable | line | — |
| Current liabilities | Accrued liabilities | line | — |
| Current liabilities | Other | line | — |
| Current liabilities | Current liabilities (total) | subtotal | — |
| Non-current liabilities | LT debt | line | interest-bearing |
| Non-current liabilities | Bonds | line | interest-bearing |
| Non-current liabilities | Deferred liabilities | line | — |
| Non-current liabilities | Lease obligations | line | ASC 842 recognized lease liabilities (US issuers, D20) |
| Non-current liabilities | Other | line | — |
| Non-current liabilities | Non-current liabilities (total) | subtotal | — |
| Equity | Share capital | line | — |
| Equity | Reserves | line | — |
| Equity | Retained earnings | line | — |
| Equity | Non controlling interest | line | *label fix 2026-08-18*; equity-side counterpart of the IS "Minority interest" line |
| Equity | Other | line | — |
| Equity | Equity (total) | subtotal | — |
| — | **Interest-bearing debt (total)** | computed | = ST debt + LT debt + Bonds (§6.7) — the *stock* level the CFF deltas alone cannot supply; feeds net debt, WACC D/E, D4 debt ≤ assets + equity |

**Balance-sheet identity (gate):** Total assets = Current liabilities (total) + Non-current liabilities (total) + Equity (total) (with NCI inside equity, per the block above).

### 6.5 Cash-flow statement

| Block | Line | Type | Notes |
|---|---|---|---|
| CFO | Net profit (attributable) | line | starting point, per issuer |
| CFO | Depreciation & amortization | line | add-back |
| CFO | ΔWC — A / P | line | working-capital components |
| CFO | ΔWC — A / R | line | — |
| CFO | ΔWC — Tax payable | line | — |
| CFO | ΔWC — Inventories | line | — |
| CFO | ΔWC — Other | line | — |
| CFO | Other | line | — |
| CFO | **CFO (total)** | subtotal | — |
| CFI | CapEx | line | — |
| CFI | Purchase of investments | line | — |
| CFI | Maturities of investments | line | — |
| CFI | Other | line | — |
| CFI | **CFI (total)** | subtotal | — |
| CFF | Dividends | line | — |
| CFF | Stock issuance | line | split from "Stock issuance / buyback" (label fix 2026-08-18) |
| CFF | Stock buyback | line | — |
| CFF | Debt issuance | line | — |
| CFF | Debt repayment | line | — |
| CFF | Other | line | — |
| CFF | **CFF (total)** | subtotal | — |
| — | Net cash | subtotal | = CFO + CFI + CFF |

### 6.6 Cash reconciliation (checksum block)

| Line | Type | Notes |
|---|---|---|
| ForEx effects | line | FX impact on cash |
| Δcash from cash-flow statement | computed | = net cash + ForEx effects |
| Δcash from balance sheet | computed | = (cash + ST investments) end of period − prior period |

**Reconciliation identity (the permanent fixture, D24/§5.4):** Δcash (CFS) = Δcash (BS) within tolerance.

### 6.7 Checksum identities (Phase 1 go/no-go gate, D24)

Deterministic checks run on the LLM's reclassified output **on every statement, every run** — the LLM is never trusted, it is arithmetic-checked. Tolerances fixed in Phase 0 (0.5); failure on a golden fixture is a hard reject (D9).

| # | Identity |
|---|---|
| 1 | Cost of revenue (incl. D&A) = Cost of revenue (excl. D&A) + D&A |
| 2 | Gross profit = Revenue − Cost of revenue (incl. D&A) − Other (cor) |
| 3 | EBIT = Gross profit − R&D − SG&A − Other (opex) |
| 4 | EBT = EBIT + Net interest expense + Other non-operating income/(expense) + Other (non-op) |
| 5 | Net consolidated profit = EBT − Income tax + Other items after tax |
| 6 | Net profit (attributable) = Net consolidated profit − Minority interest |
| 7 | Net PP&E ≈ Gross PP&E − Accumulated depreciation (tolerance) |
| 8 | Total assets = Current assets (total) + Non-current assets (total) |
| 9 | Total assets = Current liabilities (total) + Non-current liabilities (total) + Equity (total) |
| 10 | Net cash = CFO (total) + CFI (total) + CFF (total) |
| 11 | **Δcash (CFS) = Δcash (BS)** (tolerance) — the headline reconciliation |
| 12 | Every `Other` line that receives ≥ 1 raw key carries mapping provenance (D12); an unexplained non-zero `Other` is a gate failure |

### 6.8 Computed values (derived — never reclassification targets)

Derived from reclassified lines; each carries `derived_from` + `rule_version` (D12). **Not** valid `canonical_map` keys (D9): if an issuer *reports* one as a line (e.g. an EBITDA XBRL extension), it is mapped into the relevant `Other` line and cross-checked here — not accepted into itself.

| Value | Formula | Consumer |
|---|---|---|
| EBITDA | EBIT + D&A | diagnostic / ratios — never a direct DCF input |
| Effective tax rate | Income tax ÷ EBT (where EBT > 0) | `t` in FCF = EBIT·(1−t) + D&A − ΔWC − CapEx (§8) |
| Total interest-bearing debt | ST debt + LT debt + Bonds (§6.4) | net debt, WACC D/E, D4 constraint |
| Net debt | Total interest-bearing debt − (Cash & equivalents + Cash investments + ST investments) | sanity check, bridging |
| D/E, E/D, D/V, E/V | from total interest-bearing debt + Equity (total) | WACC weights (§8) |
| Basic / Diluted EPS | Net profit (attributable) ÷ basic / diluted shares | cross-check vs reported EPS lines |

### 6.9 Freeze & change policy

Frozen at end of Phase 0 (D11): additive-only after; every computed field carries `derived_from` + `rule_version` (D12). The prompt, the golden set, and all tests are written against **this exact structure** (§5, Phase 0.3/0.4).

## 7. Data Ingestion — EDGAR, US Issuers

- **Source:** SEC EDGAR via iXBRL — US issuers only (D20).
- **Depth:** 10 fiscal years where available; otherwise use available history down to a **5-year hard floor** (D21). Fewer → hard reject (insufficient history).
- **Stubs:** **ignore stub periods; value the last completed fiscal year** (D28).
- **Restatements:** **restated figures preferred**; as-originally-reported only when no restated statement exists for the year (D22).
- **Extensions:** issuer-specific XBRL extensions are normalized to canonical tags *before* the LLM stage (tag normalization only — not reclassification, D8 is untouched).
- **Exclusions:** banks, insurers, financials → hard reject (D9 premise).
- **Storage:** flat JSON files, no database (D16): `data/{ticker}/fiscal-{YYYY}.json`; re-fetch is idempotent, provenance recorded (D12).

## 8. DCF Mechanics

Two-stage: explicit forecast period (5 years typical) + terminal.

- **FV = Σ PV(FCF_t) + PV(Terminal), Terminal = FCF_{n+1} / (WACC − g_terminal)**
- **FCF = EBIT·(1−t) + D&A − ΔWC − CapEx**
- **WACC = E/(E+D)·r_e + D/(E+D)·r_d·(1−t)**; `r_e = r_f + β·ERP`
  - `r_f` = 4.25% default, overridable; `ERP` = 5.5% base (5.0–6.0%), overridable
  - `β` = industry-prior blend (heuristic, §10); ticker's own β when 10y history exists (D21)
  - `t` = tax rate from statements if available, else a sourced default
- **Shares:** diluted outstanding, as reported (D25).
- **Terminal growth:** hard-reject if `g_terminal ≥ WACC` (D9/D1).

## 9. Monte Carlo Mechanics

Per scenario:

1. Draw **N = 10,000 (D27)** correlated parameter sets via Cholesky sampling of the hardcoded correlation matrix (D26).
2. **Scenario-realism filter (D29):** discard economically incoherent draws **before** any DCF evaluation — e.g. a **decreasing profit margin co-occurring with a capex spike in the same year**. Rules are named hardcoded constants (contradiction pairs + a severity bar); draws below the bar survive, above it are rejected. **Rejection counts are printed on every run** alongside the D3 reject-and-redraw stats.
3. Run the DCF for every surviving draw — vectorized NumPy.
4. Fair-value distribution (over surviving draws): median, p10, p90, `P(FV > current_price)`, plus the distributions of the key drivers (WACC, g_terminal, FCF CAGR).
5. **Reject-and-redraw rules (hardcoded constants):** WACC < r_f → reject; g_terminal ≥ WACC → reject; FCF_t < 0 (t < n, no recovery) → flag; terminal value > 80% of FV → flag; any parameter clamped on >25% of draws → hard reject (>10% → warning, D4). **Rejection stats (parametric and realism-filter) printed on every run (D6/D26/D29).**

**Correlation matrix (D26, named constants in code):** WACC↔FCF_CAGR = −0.5, WACC↔g_terminal = −0.3, FCF_CAGR↔g_terminal = +0.4.

**Scenario breakdown:** median, p10, p90, P(FV > price), top-3 driver contributions via a rank-1 finite-difference sensitivity sweep (not full Sobol), rejection stats.

## 10. Assumption Sourcing

Every parameter carries a full provenance record (D7): `value`, `lo`, `hi`, `source`, `confidence` (0–1), `seed`, `model`.

- **`estimated`** — derived from statements (tax rate, capex ratio). Confidence = f(variance over last 3 years).
- **`user`** — JSON override (D10). Highest authority.
- **`llm`** — proposed by the reclassification stage where a relevant estimate can be extracted. Confidence = LLM confidence blended with reconciliation pass rate (D24).
- **`heuristic`** — a **small curated set of industry-prior constants** (e.g. "software β ≈ 1.2"), explicitly labeled heuristics, never silent defaults.

## 11. Output Contract & CLI (v1)

CLI subcommands (D15): `parse <ticker>` (fetch + canonicalize), `run <ticker> [--scenario base|optimistic|pessimistic] [--seed N] [--json]`, `report <run_id>`.

```json
{
  "ticker": "...", "run_id": "...", "seed": "...", "ts": "...",
  "statements_years": [2015, 2016, "...", 2024],
  "reclassification": {
    "canonical_map": { ... },
    "other_bucket": [ ... ],
    "confidence_summary": { "high": 1, "medium": 2, "low": 0 },
    "reconciliation": { "revenue_sum_ok": true, "total_sum_ok": true, "other_ok": true, "tol_rev": 0.02, "tol_total": 0.01 }
  },
  "scenario_results": {
    "base": {
      "inputs": { "WACC": { "value": "...", "lo": "...", "hi": "...", "source": "estimated", "confidence": "..." }, "...": "..." },
      "distributions": { "intrinsic_value": { "median": "...", "p10": "...", "p90": "...", "p(FV>price)": "..." } },
      "drivers_top3": [ ... ],
      "rejection_stats": { "attempts": 10000, "accepted": "...", "rejection_rate": "...", "clamped_draws_pct": { ... } }
    }
  },
  "provenance": [ "...full D7 records..." ]
}
```

## 12. Phased Build Plan

**Decisions already taken (do not re-litigate, §4):** DCF + Gordon (D1); distribution (D2); MC with 10,000 correlated draws via hardcoded constants (D3/D6/D26/D27); parameter hierarchy with hard rejects (D4/D9); determinism & auditability (P1/D7/D17); **LLM reclassification as the core, built first, proven first, mandatory, no fallback (P3/D8)**; US issuers only (D20); 10y / 5y-min history (D21); restated figures (D22); non-canonical → Other (D23); LLM validation against a manually reclassified golden set (D24); diluted shares (D25); fiscal-year / stub policy (D28); scenario-realism filter (D29); **flat files, no DB (D16)**; **local Ollama only, model-agnostic (D14)**; **CLI only (D15)**.

### Phase 0 — Foundation (no pipeline stages)

**Scope.**
- 0.1 Repo scaffolding: Python 3.12 (`uv` or `pip`), CLI entry (D15), JSON I/O, seed plumbing (D17), config.
- 0.2 CI: `ruff`, `mypy --strict`, one happy-path test.
- 0.3 **Freeze the canonical schema (D11)** — the full §6 structure: income statement, balance sheet (current assets / non-current assets / liabilities & equity), cash-flow statement (CFO / CFI / CFF), cash reconciliation block, the §6.7 checksum identities, and the §6.8 computed values, each with `derived_from` + `rule_version` on every computed field (D12). Write it in code + a machine-readable JSON schema. **This is the contract the LLM stage, the DCF, and the MC layer all build against.**
- 0.4 **Golden fixture set:** 5–10 real US-firm statements hand-reclassified into the frozen schema, plus a 10-statement **holdout** (Phase 1) — this is the **manually reclassified set the LLM is validated against (D24)** and the permanent reconciliation fixture (D9).
- 0.5 **Identity-check tolerances** for the §6.7 checksum identity set (§5.4).
- 0.6 Pick the 10 tickers the pipeline will be built against (≥ 3 industries, US issuers, no financials) — **frozen; reused in Phase 5.**

**Gate (blocking):** 0.1 + 0.2 green in CI; 0.3 schema is code + JSON with `derived_from` on every computed field; 0.4 golden set hand-checked; 0.5 tolerances committed to config; 0.6 ticker list committed as a JSON file.

### Phase 1 — LLM Reclassification: the GO/NO-GO gate

**Why first:** P3/D8. This is the project's differentiator and its main risk; it is mandatory in every run with **no fallback path**. If it fails, the project should die here — cheaply — not after three phases of DCF/MC work.

**Scope.**
- 1.1 Prompt + model-agnostic client (D14) against Ollama (qwen3:8b class default).
- 1.2 JSON-output enforcement + schema validation (frozen, D11) + hallucination gate (D9).
- 1.3 Reconciliation harness — **all §6.7 checksum identities on the golden set and holdout** (D24), plus the §5.4 field-level checks.
- 1.4 Confidence-band policy from §5.3 implemented (`<0.3` core-field reject; `0.3–0.7` flagged; `≥0.7` clean).
- 1.5 **Non-canonical → `Other` bucket (D23)** — first-class in the schema, visible in outputs.

**Gate (blocking, go/no-go):** on the golden + holdout set (15 statements):
- ≥ 90% of canonical fields correctly mapped
- 0 hallucinated fields (D9)
- ≥ 85% of fields with confidence ≥ 0.7 (0.3–0.7 band tolerated but flagged)
- All of the §6.7 checksum identities (12 checks) pass within tolerance on every statement

**Failure mode:** **stop.** Record the miss pattern in the phase log. Re-prompt / re-model (the model-agnostic client, D14, makes this a one-line swap). Iterate on **the prompt, not the frozen schema.** Only a pass unblocks Phase 2.

### Phase 2 — Data Ingestion (real statements)

**Scope.** EDGAR/iXBRL pull (D20). 10y / 5y-min depth (D21). Restated-vs-originally-reported handling (D22). Stub exclusion / last-completed-fiscal-year selection (D28). Bank/insurer/financial exclusion (D9). XBRL extension-tag normalization (§7).

**Gate (blocking):** for the 10 tickers (0.6):
- All 10 pull and land in flat JSON (D16)
- 5y-min depth fires on a deliberately-short-history ticker
- A stub period (e.g. a 2-quarter fiscal year) is correctly dropped (D28)
- Restated figures are used where they exist (§7)
- Extension-heavy statements normalize without error

### Phase 3 — Estimation & DCF (math kernel)

**Scope.** Parameter hierarchy (D4/D9). Scenario presets (D19) + JSON override (D10). WACC/β per §8 (weights **D/E, D/V from §6.8** — interest-bearing debt from the §6.4 block — with `r_f` 4.25%, ERP 5.5% base, β from industry priors + own β when 10y history exists). Two-stage FV per D1. Provenance records (D7).

**Gate (blocking):** for the 10 tickers:
- Base scenario produces a full D10-compliant output
- Every field has provenance (D7)
- Hard-reject rules (D9) fire on at least one deliberately-broken input
- Determinism: same seed → bit-identical output (D17)

### Phase 4 — Monte Carlo & Distribution (the payoff)

**Scope.** Cholesky sampling of the hardcoded correlation matrix (D26). N = 10,000 per scenario (D27), vectorized (D13). Reject-and-redraw rules with **visible stats** (D6). Scenario breakdown (top-3 drivers via rank-1 finite-difference sweep, §9).

**Gate (blocking):**
- 10 tickers × 3 scenarios all produce the full §11 output contract
- `rejection_stats` present and non-trivial on every run, including **realism-filter counts (D29)**
- A planted contradictory draw (margin ↓ + capex ↑ in same year) is verified to be **discarded, not evaluated**
- Top-3 driver output sane (no negative "contributions" to FV growth)
- Determinism holds (D17)

### Phase 5 — Backtest (validation against realized data)

**This is O1 — the difference between a working pipeline and a validated one.**

**Scope.** Historical backtest: select 10–20 US tickers with ≥ 5 years of realized stock-price history (D21 minimum; 10 preferred where available). For each, freeze the pipeline's view at the **last completed fiscal year (D28)** using statements available at that date (as the pipeline would have fetched them), and score:
- `P(FV > price_then)` vs. realized outcome at +12m and +24m
- Calibration: is the distribution spread correctly calibrated against realized moves?
- Failure analysis: which assumptions (WACC, g_terminal, FCF growth) drove the misses?

**Blocking gate (go/no-go):** report a calibration table + Brier score per ticker; **this output ships alongside v1** (it is a validation artifact, not a blocker on the pipeline itself — but v1 release requires it to exist and be honest).

### Phase 6 — Hardening & v1 release

**Scope.** Full §11 validation on the 10-ticker set in CI. `reproduce.sh` + `requirements.txt` + pinned versions. README + a worked example (APL or equivalent). Determinism CI job (same seed twice → identical JSON). Lint/type coverage 100%. `v1.0.0` tag.

**Exit criteria (blocking):**
1. All of Phases 1–5 gates green
2. v1 acceptance list (§14) checks off item-by-item
3. A single `reproduce.sh` takes a user from `git clone` to a full distribution output on one ticker in < 10 minutes
4. License (Apache 2.0, D18) present; `provenance` model documented; US-scope restriction (D20) stated in the README and the CLI `--help`

### Phase log format

```
## Phase N — <name>
Start: <date>
End: <date>
Gate: PASS | FAIL | ITERATING
Decisions taken: <list>
Open items closed: <list>
Notes: <short>
```

## 13. Open Questions (each with a default / decided status)

| # | Question | Default / Leaning | Settles in |
|---|----------|------------------|------------|
| O1 | Historical backtest as the first validation step (score historical valuations vs. actual outcomes)? | **Yes — the difference between a validated tool and an unvalidated one.** | Phase 5 |
| O2 | **Closed (D20/D16).** Statements source = SEC EDGAR (US issuers only); local file upload is a test harness, never an alternative; storage = flat files, no DB in v1. | — | — |
| O3 | Default industry-prior dataset (β/ERP anchors): build or license? | Build a small curated set for the 10-ticker universe; mark all priors `heuristic` (D7). | Phase 3 gate |
| O4 | **Closed (D26/D6).** Correlation constants hardcoded as named values in code in v1; visible per run with counts. | — | — |
| O5 | How to present the distribution (p10/50/90 vs CDF)? | Both — p-values in the default output, full CDF in `--json` mode. | Phase 4 gate |
| O6 | Scenario naming / assumption ranges: JSON config per company, or LLM-derived? | JSON override supported (D10); the LLM may propose ranges as context input (D10), but the defaults are named constants. | Phase 3 gate |
| O7 | What "confidence" number on a field means — LLM self-reported vs. derived from reconciliation pass rate? | Both: LLM self-reported confidence + a reconciliation-adjusted confidence; the reconciliation number is the one that gates hard-rejects (D9). | Phase 3 gate |
| O8 | Handling of issuers with non-standard fiscal calendars (e.g. Walmart's Jan–Feb end)? | Normalize to trailing-12-month as-of the last completed fiscal year (D28); stubs ignored. | Phase 2 gate |
| O9 | Minimum historical depth threshold for a valid ticker? | **Closed (D21)** — 10y where available, never fewer than 5. | — |
| O10 | Parameter range bounds: hand-set per scenario vs. learned from the last N issuers in the same GICS sector? | Hand-set for v1 (D19/D4), documented per scenario; sector-learned ranges = post-v1. | Phase 3 gate |
| O11 | Terminal growth g range per scenario: hard-coded constants vs. LLM-derived? | Hard-coded constants per scenario (D19), LLM may propose as context (O6). | Phase 3 gate |
| O12 | Share-count policy: diluted (D25) vs. basic? | **Closed (D25)** — diluted, as reported. | — |
| O13 | Beta source: industry-prior vs. ticker-computed? | Blend: ticker's own 10y β where available (D21), else industry prior (O3/O10). | Phase 3 gate |
| O14 | Tax rate source? | From statements (effective, trailing 3yr) where available; else GICS-segment default from O3. | Phase 3 gate |
| O15 | **LLM model choice (D14 closed):** v1 = local Ollama only, model-agnostic client; default qwen3:8b class. **No cloud providers in v1.** | — | — |
| O16 | **Storage (D16 closed):** flat JSON files in v1; any DB is post-v1. | — | — |
| O17 | **Non-canonical handling (D23 closed):** classify into `Other`, a first-class schema field; never drop, never hard-fail. | — | — |
| O18 | **Share count (D25 closed):** diluted, as reported. | — | — |
| O19 | **Correlations (D26 closed):** hardcoded named constants in v1; not fitted, not user-tunable. | — | — |
| O20 | **MC iterations (D27 closed):** exactly 10,000 per scenario, named constant. | — | — |
| O21 | **Interface (D15 closed):** CLI only in v1; no web UI, no API server. | — | — |
| O22 | **US scope (D20 closed):** US issuers only, SEC EDGAR. | — | — |
| O23 | **Fiscal-year policy (D28 closed):** ignore stubs; value the last completed fiscal year. | — | — |
| O24 | **SBC treatment (open — flagged 2026-08-18):** US tech issuers carry heavy stock-based comp, which v1 keeps *inside* SGA (i.e. SBC reduces operating income like any other expense). Alternative: add SBC back as a non-cash item (like D&A). | **Default: keep SBC in operating expenses** (SBC is a real economic cost); treat add-back as optional, documented, per-scenario. | Phase 3 gate |

**Resolved by D-decision (not open):** O2, O4, O9, O15–O23 — see the "Closed" rows above or the decisions list in §4.

## 14. v1 Acceptance Checklist (exit criteria for the whole project)

1. The **mandatory** LLM reclassification stage meets the Phase 1 gate (≥90% field accuracy, 0 hallucinated fields, ≥85% confidence ≥ 0.7) on the manually reclassified golden set (D24); **no fallback reclassification path exists in the codebase** (D8).
2. US issuers only: non-US ticker → clean hard rejection (D20).
3. 10 tickers across ≥ 3 industries, all with 5y+ of statements (10y where available, D21), stubs excluded (D28).
4. Each of those 10 tickers, in each of the 3 scenarios, produces the full §11 JSON contract including `rejection_stats` (parametric + **realism-filter, D29**) and `provenance`.
5. Determinism: same input + same seed → identical output across two runs (D17).
6. Backtest (O1) produced a calibration table for the 10 tickers — the pipeline ships with it (Phase 5).
7. A single `reproduce.sh` script takes a user from a clean checkout to a full distribution output on one ticker in < 10 minutes.
8. License (Apache 2.0, D18); `provenance` model documented (D7); US-scope restriction stated in README and CLI `--help` (D20).
9. **No database in the tree (D16); no cloud LLM dependency (D14); no web UI (D15)** — flat files, local Ollama, CLI only.

## 15. Out of Scope (explicit — v1)

- Relative-valuation / comps modules
- M&A / LBO modeling
- Banks, insurers, financials
- **Non-US listings / issuers (D20)**
- Real-time pricing
- **Any database layer — v1 is flat files only (D16)**
- **Cloud LLM providers — v1 is local Ollama only, model-agnostic (D14)**
- **Web UI or API server — v1 is CLI only (D15)**

## 16. Risks & Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| LLM mis-maps a core field, DCF is wrong, user trusts the output | **High** | P3/D8: proven first (Phase 1). D9 hard-reject on low-confidence core fields + reconciliation checks (§5.4) on every run. Provenance (D7) makes any mis-map visible. |
| LLM cannot reach the accuracy bar on real iXBRL | **High** | Phase 1 is a hard go/no-go gate; **the project stops and the premise is revisited** — no time lost on downstream work (P3/D8). |
| Hallucinated schema keys (D9) | High | Hard-reject rule; §5.4 reconciliation catches any key not in the frozen schema (D11) before a DCF draw. |
| Issuer-specific XBRL extensions break ingestion | Medium | Phase 2 gate includes an extension-heavy statement; extension-tag normalization step (§7). |
| WACC / β from a thin industry prior is wrong | Medium | Priors labeled `heuristic` (D7/§10); the distribution (D2) propagates the uncertainty rather than hiding it. |
| Ollama model swap silently changes output | Medium | Every provenance record carries the `model`; CI runs one golden output on the default model and diffs against the committed golden (D17/D11) — a model swap that changes output is caught. |
| Scope-creep into US / DB / UI | Medium | D20 / D16 / D15 make these **decisions, not open questions**; any change requires a new row in §13 and a new D-number. |
| Stub-period / fiscal-calendar mismatch | Low | D28 hard rule; tested at the Phase 2 gate. |

---

*Single document — the former `PROJECT.md` (decisions, premises, acceptance) and `PLAN.md` (mechanics, phases, open items) are merged here. Owner: Ulrico Luigi Nava. Last restructured 2026-08-16.*
