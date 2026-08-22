# PROJECT — Decision Log

Decisions only. **D#** = closed and frozen. **O#** = open, with an explicit default -- work proceeds against the default until it settles into a D-number. The one line under each item is the *motivation*. Pipeline mechanics, schema details and contribution rules live in **README.md** and **CONTRIBUTING.md** and are deliberately not repeated here.

*Replaces the 2026-08-16 PROJECT.md+PLAN.md merge and its open-question table (old O2/O4/O9/O12/O15-O23 of that table were already closed by D-decisions and are not resurrected here).*

## Closed decisions

- **D1 -- Terminal value: alpha-blended Gordon growth + exit multiple, with g terminal strictly below WACC.**
  Motivation: a pure Gordon or pure exit value lets one assumption dominate the valuation; blending keeps neither block decisive on its own.
- **D2 -- Output is a distribution** (median, p10, p90, P(intrinsic value > price))**, not a point estimate.**
  Motivation: a DCF multiplies roughly ten uncertain assumptions; a single number hides the uncertainty, a distribution makes it the actual output.
- **D3 -- Monte Carlo with correlated draws (Cholesky), exactly 10,000 iterations per scenario (D27), 5 reject-and-redraw rules with visible rejection statistics.**
  Motivation: correlation reflects real economic co-movement (margin, capex, growth); visible rejection counts are the evidence the filter is doing anything.
- **D4 -- Parameter hierarchy, enforced:** 1) hard constraints (D9) -> 2) clamped range bounds (warn above 10% clamped, reject above 25%) -> 3) scenario midpoints (D19).
  Motivation: unconstrained draws produce economically impossible valuations; a hard reject beats a silently-clamped nonsense value.
- **D5 -- Scenario DCF** produces an explicit point estimate per named scenario; **the MC run** produces the full distribution.
  Motivation: named scenarios stay individually auditable ("what does pessimistic mean?"); the MC covers the space between them.
- **D6 -- Correlation structure: hardcoded named constants (Cholesky), visible and counted in every run (D26).**
  Motivation: correlation is a structural choice and must be inspectable in every run, not a hidden learned artifact.
- **D7 -- Full provenance on every assumption:** `source` in {estimated, user, llm, heuristic}, `seed`, `range`, `confidence`, `model`, `field`.
  Motivation: without provenance a number cannot be audited; every assumption must be traceable back to where it came from.
- **D8 -- LLM reclassification is the core of the project and is mandatory in every v1 run; a deterministic or rule-based fallback is explicitly rejected and out of scope. If the stage fails the accuracy gate, the project stops and its premise is revisited.**
  Motivation: mapping iXBRL onto the canonical schema is where the project creates value and where it can fail; a fallback would be a permanent low-quality path hiding that failure. Proving the gate first is the cheapest possible test of the premise.
- **D9 -- Hard-reject rules, non-negotiable:** g >= WACC; shares <= 0; negative revenue or COGS (non-financial); confidence < 0.3 on a core field; any key not in the frozen schema (anti-hallucination gate).
  Motivation: a DCF run on impossible inputs yields a plausible-looking wrong number; a reject with a reason beats a silent pass.
- **D10 -- JSON-first I/O:** input = JSON (ticker + optional scenario overrides); output = structured JSON (distributions, assumptions, provenance, scenario breakdown).
  Motivation: files inspect and diff better than a UI, stay the source of truth, and keep the repo free of a web/DB stack the project never needs.
- **D11 -- Canonical schema is versioned and frozen before LLM work begins; after the freeze, additive-only changes.**
  Motivation: the schema is the LLM's target -- moving it mid-run would silently invalidate all previously trained prompts and results, so freeze first then evolve additively.
- **D12 -- No hidden state:** every derived field carries `derived_from` + `rule_version`; any output must be recomputable from raw inputs.
  Motivation: reproducibility (D17) is impossible if a value exists only in memory; full re-derivation is the audit guarantee.
- **D13 -- Python 3.12 + NumPy; no ML framework beyond Ollama's client; no pandas; no web framework.**
  Motivation: a DCF needs linear algebra, not a data-science framework; fewer deps means smaller audit surface and zero framework lock-in.
- **D14 -- v1 LLM runtime: local Ollama only, behind a thin model-agnostic client (OpenAI-compatible protocol); default model class qwen3:8b; no cloud APIs.**
  Motivation: local inference is the point -- reproducible, offline, cheap; 8b-class is enough for constrained classification against a fixed schema; the thin client keeps the model swappable.
- **D15 -- CLI only for v1:** single entry point with `parse` / `run` / `report` subcommands.
  Motivation: a CLI is testable, scriptable and needs no server process; a web UI would add attack surface and maintenance for no v1 value.
- **D16 -- Flat JSON files, no database in v1** (`data/{ticker}/fiscal-{YYYY}.json`).
  Motivation: at 10 tickers x 10 years a file layout is the simplest store that supports diff and audit; a DB earns its complexity only after that.
- **D17 -- Deterministic by default:** `--seed` honored; all stochastic behavior seeded.
  Motivation: a research tool must produce identical output for identical input + seed or results cannot be compared across versions.
- **D18 -- License: Apache 2.0; model weights are not redistributed; only prompts + pipeline code are licensed.**
  Motivation: Apache is permissive-but-attributed, matches the research-tool spirit and keeps the file-header convention auditable; redistributing weights would collide with model licenses.
- **D19 -- Scenario presets: base / optimistic / pessimistic, each a JSON object of midpoints + ranges.**
  Motivation: named presets make the uncertainty explicit and the assumptions human-readable; JSON keeps them override-able without touching code.
- **D20 -- US issuers only, v1.** Ticker must be a US issuer with statements on SEC EDGAR (XBRL); non-US listings are hard-rejected.
  Motivation: XBRL tagging quality, GAAP vocabulary and filing format are the reclassification stage's whole assumption; non-US issuers would force a second schema.
- **D21 -- History depth: 10 fiscal years where available, never fewer than 5; below 5 usable years -> hard reject** with the reason surfaced.
  Motivation: the DCF is trained on the issuer's own trajectory -- five years is the floor for a stable margin/capex/growth pattern.
- **D22 -- Use restated figures when available** (as-originally-reported only for a year with no restated statement).
  Motivation: restatements are how the audited truth looks in retrospect; comparing across years on originally-reported figures mixes two accounting bases.
- **D23 -- Non-canonical items map into a first-class `Other` bucket; never a failure, never silently dropped.**
  Motivation: the schema is intentionally narrower than a full filing; the only honest place for the rest is a visible `Other` line, and reconciliation (D24) still forces the total to balance.
- **D24 -- LLM validation against a frozen golden set of manually reclassified statements; the 12 identity/reconciliation checks pass on every run, failures on golden fixtures are hard rejects.**
  Motivation: the golden set is the ground truth for the reclassification stage and the permanent regression fixture -- every model or prompt change is measured against the same statements, forever.
- **D25 -- Share count: diluted shares outstanding, as reported.**
  Motivation: DCF value is per-share value; diluted is the conservative, comparable choice and what the market prices.
- **D26 -- Correlations between DCF inputs: hardcoded named constants (Cholesky) in v1 -- not fitted, not user-tunable (implements D6).**
  Motivation: learning a covariance matrix from a 10-ticker universe is not statistically defensible; constants are honest, documented and changeable in one place.
- **D27 -- Monte Carlo iterations: exactly 10,000 per scenario, a named constant, not tunable in v1.**
  Motivation: "how many draws?" is not a modeling question once the distribution is converged; a fixed named constant keeps runs comparable (D17) and removes a fake knob.
- **D28 -- Fiscal-year policy: stub periods are ignored; the valued period is the last completed fiscal year; partial years are excluded.**
  Motivation: a stub period is not a real year -- valuing on it skews every annualized assumption; trailing-12-month as-of the last completed FY is the only clean basis (also closes non-standard calendar ends).
- **D29 -- Scenario realism filter (core feature):** contradictory draws are discarded *before* any DCF evaluation (not merely penalized) -- e.g. a decreasing profit margin co-occurring with a capex spike in the same year; a set of named, hardcoded rules with visible rejection counts on every run; thresholds are fixed constants in v1.
  Motivation: an MC that evaluates impossible scenarios wastes draws and flattens the tails; rejecting them up front keeps every evaluated draw economically coherent (D6/D26 style visibility).

## Open decisions (default in force until settled)

- **O1 -- Ingest layer for production runs: `companyfacts` API (data.sec.gov) vs parsed R-files (`FilingIndex.xml` -> `R*.htm`).**
  Default: keep the R-file path (filing-internal names, printed layout) and treat `companyfacts` as the cross-check/corpus; revisit once the golden set is validated.
  Motivation: this is the one EDGAR question still genuinely open. The 2026-08-21 cross-validation on NVDA / CSCO / F5TH established: (a) values are byte-identical in both sources, only display names differ (join key is the *concept tag*, never the label); (b) the JSON `frame` field is unreliable (NVDA FY2026 sits in `CY2025`) while `end` + `form` are safe; (c) company extensions live in separate namespaces (`invst`/`ffd` for CSCO, issuer tags for F5TH), so any `us-gaap`-only reader silently drops them; (d) per-share facts sit in `shares`/lowercase-`usd` buckets, not `USD`. The API wins on structure (typed, no HTML parsing), the R-files win on presenting exactly what the filer audited and on subtotals the JSON does not carry as tags (CSCO "Total revenue" is a computed sum of Products + Services). Settles when the reclassification stage (D8) has a target to train against.
- **O2 -- Historical backtest as the first validation step (score historical valuations vs realized outcomes).** Default: yes, it runs before anything else after the DCF kernel lands.
  Motivation: the difference between a validated tool and an unvalidated one; backtest failure is cheap information, backtest success after release is marketing.
- **O3 -- Default industry-prior dataset (beta / ERP anchors): build or license.** Default: build a small curated set for the 10-ticker universe; every prior labeled `heuristic` (D7).
  Motivation: licensing a prior dataset adds a dependency the project is designed not to have; 10 tickers is small enough to curate by hand and document.
- **O4 -- Distribution presentation: p10/50/90 vs CDF curve.** Default: p-values in the default output, full CDF in `--json` mode.
  Motivation: p-values are the decision-relevant summary; the CDF is the audit-relevant artifact; default vs flag keeps both without cluttering either.
- **O5 -- Scenario assumption ranges: hand-set per scenario vs learned from issuers in the same sector.** Default: hand-set for v1 (D19), documented per scenario; sector-learned ranges are post-v1.
  Motivation: the same argument as D26 -- with a 10-ticker universe there is not enough data to learn ranges honestly; constants are documented, hand-set values are decisions (D-able), and they can be superseded by a real prior (O3).
- **O6 -- What field `confidence` means: LLM self-reported vs derived from reconciliation pass rate.** Default: both -- LLM self-report + reconciliation-adjusted confidence; the reconciliation number is the one that gates hard rejects (D9).
  Motivation: an LLM's self-assessed confidence is not the same thing as the output passing identity checks; gating on the measured quantity (D24) beats gating on the stated one.
- **O7 -- Minimum-history threshold for a valid ticker.** Default: as D21 states (10y where available, never fewer than 5).
  Motivation: listed as open in the legacy document for transparency; the D21 default governs until it is challenged by a real issuer in the universe.
- **O8 -- Beta source: ticker-computed (10y beta) vs industry prior.** Default: blend -- the ticker's own 10y beta where data quality is sufficient, else the industry prior from O3.
  Motivation: a 10-ticker universe makes a well-estimated ticker beta cheap; an industry prior is the floor for tickers with noisy or short histories.
- **O9 -- SBC treatment (flagged 2026-08-18): keep inside operating expenses (reduces operating income like any other expense, v1 default) vs add back as a non-cash charge (like D&A).**
  Default: keep SBC in operating expenses -- for a US tech-heavy issuer SBC is a real economic cost and removing it would flatter margin; add-back stays available as a documented, per-scenario override.
  Motivation: v1's issuer universe is SBC-heavy; quietly choosing one treatment and not documenting it is the exact silent-assumption failure mode the whole provenance layer (D7) exists to prevent.
