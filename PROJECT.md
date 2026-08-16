# PROJECT.md — cf-quant: Probabilistic DCF

**Internal working document — base of discussion for contributors.**
This file records what the project *is*, which decisions are **already taken** (and therefore not up for re-discussion without explicit revision), what differentiates it, and what remains open. Implementation specifications, schema details, and module-level notes live in `PLAN.md` and are out of scope here.

---

## 1. Final goal (one sentence)

Given a **US-firm ticker**, produce a **probability distribution of intrinsic value** — a distribution, not a number — computed by a fully transparent, reproducible pipeline over company-reported financials, with fair value estimated per scenario and aggregated statistically.

Concretely, the output is: median FV estimate, percentile bands (10/25/50/75/90), **P(intrinsic value > trading price)**, expected value, and the scenario breakdown with all assumptions visible and attributable to their source.

## 2. Core design premises (taken; do not re-open without revision)

These premises follow directly from the observed failure mode of LLM-assisted valuation: models given financials do not compute DCF — they *narrate* a number. The project's entire structure exists to eliminate that failure mode, and the following consequences are treated as settled:

| # | Premise |
|---|---|
| P1 | **The LLM never touches an arithmetic operator.** All math (discounting, MC sampling, percentile computation) happens in deterministic, versioned code. |
| P2 | **An LLM is a statement reclassifier, not a valuation engine.** Its only role is mapping raw company-reported line items to a canonical schema. |
| P3 | **The LLM reclassification stage is the foundation — built first, proven first, and mandatory.** It is the project's differentiator and the component whose reliability cannot yet be trusted; it is therefore the first engineering phase (after foundation), and if it cannot reach the accuracy bar the project *halts* rather than degrades. No deterministic or rule-based fallback reclassification path exists in v1 (D11). |
| P4 | **Evidence over precision; transparency over accuracy.** A distribution with visible assumptions and known rejection rates outranks a point estimate of false precision. Validation and rejection are *features*, not friction. |
| P5 | **Reproducibility is a requirement, not a property.** Fixed seed ⇒ identical output on identical input, every time.

## 3. Decisions already taken (authoritative)

Contributors should treat these as fixed. Disagreement → raise against the stated rationale, propose a revision.

### Valuation core
- **D1. DCF + Gordon Growth — no multiples, no hybrid.** The valuation model is a multi-stage DCF with Gordon terminal value. EV→FV bridge via net working capital, net debt, cash. Multiples-based cross-checks are explicitly rejected (noisy, industry-dependent, no principled prior).
- **D2. Distribution, not point estimate.** Every assumption is a distribution (or a constraint range); FV is computed per scenario; the output distribution is the product.
- **D3. Monte Carlo over assumption space.** Independent draws per scenario across the full assumption vector — not one "median scenario" — so parameter uncertainty and distribution shape (e.g. P95 revenue growth > 20% under low probability mass) propagate into FV.
- **D4. Scenario-based output, not a single scenario.** Three or more named scenarios (conservative / central / optimistic) derived from historical CAGR, industry context, and company trajectory, each carrying its own assumption ranges.
- **D5. Constraint validation layer with rejection policy.** Every MC draw is checked against hard ranges (e.g. P95 revenue growth ≤ 20% under low prob mass); invalid draws are **rejected and redrawn** (bounded retries) rather than clamped. Rejection rate per assumption is logged and surfaced in output — a high rejection rate is a signal that assumptions are mis-specified, and this signal must remain visible.
- **D6. Correlated draws via Cholesky decomposition.** At minimum: revenue growth ↔ operating margin; revenue growth ↔ capex ratio; operating margin ↔ debt ratio. Independence is a known source of unrealistic scenarios; correlations are estimated from historical co-movement where data exists, otherwise set by judgment and marked `heuristic`.
- **D7. Parameter estimation hierarchy (strict ordering).**
  1. **Hard anchor:** company-reported figures (most recent actuals) — immutable.
  2. **Soft prior:** industry median (S&P GICS classification) — Bayesian prior when company data is sparse or noisy.
  3. **Heuristic default:** only when 1 and 2 are both absent — always flagged with provenance `"assumed"` and low confidence.
  Anything deviating from this order is a bug, not a design choice.
- **D8. WACC from CAPM**, with beta and ERP estimated from data where feasible and marked by provenance; country-risk adjustment where applicable. Debt structure drives the capital-structure leg.
- **D9. Hard reject on terminal-growth inconsistency:** `terminal_g >= WACC` terminates the valuation — the model is structurally invalid for that company.

### LLM boundary & data
- **D10. LLM used only for statement reclassification** (raw line items → canonical schema fields, with a confidence score per mapping). It may *propose* plausible assumption ranges as **context input** to the estimation layer, but it does not decide them.
- **D11. LLM reclassification is mandatory in every v1 run.** A deterministic or rule-based fallback reclassification path is explicitly rejected and out of scope. If the LLM stage cannot reach the accuracy bar (see §8, criterion 1), the project halts and the premise is revisited (P3) — it does not ship with a degraded reclassifier. This is why the LLM stage is built and proven first, before any other stage logic (`PLAN.md` §12, Phase 1 go/no-go).
- **D12. Canonical schema is frozen and versioned** (~15–20 fields, full spec in `PLAN.md` §4): income statement, cash flow, balance sheet, share count, and market data. All field names are stable identifiers; the LLM is instructed against inventing synonyms.
- **D13. Provenance is mandatory on every field**: `"file"` (extracted from report) / `"calculated"` (derived) / `"assumed"` (defaulted) / `"heuristic"` (industry prior). No field is written without a provenance tag.
- **D14. Accounting-identity validation between stages** (e.g. cash-flow reconciliation, balance-sheet identity) acts as a gate: hard fail on broken identities; soft warning on borderline values.
- **D15. LLM provider is pluggable** via a thin interface (Ollama by default; OpenAI-compatible backends permitted). No provider-specific logic in pipeline code.
- **D22. US firms only, in v1.** Tickers must be US issuers with statements on SEC EDGAR (XBRL). Non-US listings are out of scope for v1 and are a hard rejection, not an edge case to be tolerated.

### Engineering
- **D16. Python + NumPy** for all numerics; DCF/MC computation is vectorized and loop-free over scenarios.
- **D17. Determinism** — seeded RNG, deterministic ordering, stable sorting. Two runs of the same pipeline state produce byte-identical distributions.
- **D18. Every output value carries provenance and confidence** down to the assumption level; the distribution output is the minimum contract — scenario breakdown and rejection stats are additive.
- **D19. No GUI, no API, no web UI.** CLI-first, scriptable, composable. Frontends (including any dashboard) are explicitly out of scope for this project.
- **D20. No database for v1.** File-based inputs (JSON/PDF) and file-based artifacts (JSON distributions, CSV scenarios, plots). Database-backed data loading is a post-v1 extension.
- **D21. Multi-stage pipeline as the architectural unit** (ingest → reclassify → estimate → discount → MC → aggregate → validate → report), each stage independently testable against golden fixtures.

## 4. Key differentiating functionalities

What this is *not*, functionally, is as important as what it is. Distinctive capabilities, in order of importance:

1. **Distributional output as the product.** Most valuation tools output a number with a confidence the user has to infer. Here the distribution *is* the answer: `median_fv`, `p10/p25/p50/p75/p90`, `p(fv > price)`, `expected_fv`, scenario decomposition, rejection-rate report.
2. **LLM in the data layer, excluded from the math layer.** Inversion of the common "LLM analyst" pattern: the model handles the messy part (non-canonical financial language) and is structurally prevented from the part it fails at (arithmetic under instruction).
3. **Constraint-aware Monte Carlo with visible failure modes.** Rejection-and-redraw with per-assumption rejection statistics turns assumption mis-specifiedness into a measurable, surfaced quantity rather than a silent bias.
4. **Provenance everywhere.** Every number is traceable to `file` / `calculated` / `assumed` / `heuristic` — an audit property of the output, not a feature of the report.
5. **Deterministic, reproducible, testable pipeline.** Golden-fixture tests per stage; seeded MC; `terminal_g >= WACC` hard-reject; accounting-identity gates. Reproducibility is enforced in CI, not assumed.
6. **The untrusted stage is proven first.** LLM reclassification — the one component whose reliability is not yet verified — is built as Phase 1, validated against a labeled golden set, and gated by a recorded go/no-go decision before any subsequent phase starts (D11). On failure the project halts; it does not ship a degraded reclassifier.

## 5. Explicitly out of scope (rejected alternatives)

- Point-estimate valuations presented without assumptions (rejected: P4).
- Multiples / LBO / sum-of-parts as primary methods (rejected: D1).
- LLM-generated valuation narratives or direct number output (rejected: P1, P2).
- GUI / web API / SaaS-style deployment (rejected: D19; dashboards are out of scope even as v1 extras).
- Real-time market data, streaming ingestion, or database-backed serving (rejected for v1: D20).
- Portfolio-level or multi-company comparative analytics (post-v1 candidate; keep pipeline single-company per run).
- Auto-generation of financial statements from filings (out of scope: the pipeline's input is the issuer's own filed, structured XBRL statements on SEC EDGAR — D22).
- Non-US listings / issuers (US firms only — D22).

## 6. Open questions (genuine discussion items — not settled)

These are the *only* items contributors should treat as open. Anything not listed here and not in §3 is either decided (→ §3) or unspecified (→ `PLAN.md`).

| # | Question | Current lean | Needs resolution before |
|---|---|---|---|
| O1 | Historical backtest as the first validation step (score historical valuations vs. actual outcomes)? | Yes — identified in `PLAN.md` item 20; it is the difference between a validated tool and an engineering exercise. | v1 exit |
| O2 | Historical statements source: SEC EDGAR/iXBRL pull (Phase 2) vs local XBRL file upload? | EDGAR/iXBRL pull, D22 US issuers only; local file upload is kept as a test harness, never as an alternative pipeline. | Phase 2 gate |
| O3 | Default industry-prior dataset (S&P GICS median financials): build or license? | Build a small curated set for the target universe; mark all priors `heuristic`. | Phase 3 gate |
| O4 | Correlation estimation: historical covariance of assumptions vs. fixed heuristic matrix? | Hybrid: estimate where ≥ 5 years of co-movement data exists, otherwise heuristic matrix with source documented. | Phase 4 gate |
| O5 | Minimum acceptable rejection rate before a scenario is flagged as mis-specified? | Propose hard threshold (e.g. 50%) + per-assumption warning (e.g. 20%). | Phase 4 gate |
| O6 | Should scenario naming / assumption ranges be a JSON config per company, or always LLM-derived? | JSON override supported; the LLM may propose ranges as context input (D10). | Phase 3 gate |

## 7. Relationship between this document and `PLAN.md`

- **`PLAN.md`** = implementation plan: stage order, module decomposition, data schema (full field list), distribution definitions (normal / log-normal / triangular), correlation structure, test strategy, risks, phase timeline. It answers *"how do we build it."*
- **`PROJECT.md` (this file)** = decision record and shared definition of what the project is. It answers *"what are we building, why, and what is settled."*

**Conflict rule:** if the two documents disagree on a *decision* (scope, method, boundary), this file wins and `PLAN.md` must be corrected. If they disagree on *mechanics* (module names, schema fields, distribution parameters), `PLAN.md` wins. If this file and a contributor's recollection disagree, this file wins.

## 8. Definition of "v1 done" (acceptance criteria)

v1 is complete when **all** of the following are true for at least **three** distinct companies across **two different industries**:

1. The (mandatory) LLM reclassification stage produces a canonical schema with ≥ 90% field coverage and confidence scores per field for US issuers; no fallback reclassification path exists in v1 (D11).
2. The estimation stage produces per-scenario assumption distributions with provenance tags on every field; no `assumed` field without an explicit flag in the output.
3. The DCF engine produces FV per scenario matching hand-computed golden fixtures (tests, deterministic under fixed seed).
4. The MC layer runs ≥ 1,000 scenarios per company, applies the constraint-and-rejection policy, logs per-assumption rejection rates, and uses the correlation structure.
5. Aggregation produces: median FV, p10/p25/p50/p75/p90, `p(fv > price)`, `expected_fv`, scenario breakdown.
6. Validation reports include accounting-identity checks and the `terminal_g >= WACC` reject behavior demonstrated in at least one test case.
7. A run-to-run determinism test (same input + same seed ⇒ identical output artifact) passes in CI.
8. All output artifacts are self-describing JSON (schema version, provenance, rejection stats, scenario definitions) — no undocumented fields.

The **backtest (O1) is required before v1 is declared validated** — but *the pipeline* can ship before it, clearly labeled as unvalidated in all output artifacts until O1 lands.
