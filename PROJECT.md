# PROJECT — Decision Log

Decisions only. **D#** = closed and frozen. **O#** = open, with an explicit default -- work proceeds against the default until it settles into a D-number. The one line under each item is the *motivation*. Pipeline mechanics, schema details and contribution rules live in **README.md** and **CONTRIBUTING.md** and are deliberately not repeated here.

## Closed decisions

- **D1 -- Terminal value: alpha-blended Gordon growth + exit multiple, with g terminal strictly below WACC.**
  Motivation: a pure Gordon or pure exit value lets one assumption dominate the valuation; blending keeps neither block decisive on its own.
- **D2 -- Output is a distribution** (median, p10, p90, P(intrinsic value > price))**, not a point estimate.**
  Motivation: a DCF multiplies roughly ten uncertain assumptions; a single number hides the uncertainty, a distribution makes it the actual output.
- **D3 -- Monte Carlo with correlated draws (Cholesky), exactly 10,000 iterations per scenario.**
  Motivation: correlation reflects real economic co-movement (margin, capex, growth).
- **D4 -- Parameter hierarchy, enforced:** 1) hard constraints (D9) -> 2) clamped range bounds (warn above 10% clamped, reject above 25%) -> 3) scenario midpoints (D19).
  Motivation: unconstrained draws produce economically impossible valuations; a hard reject beats a silently-clamped nonsense value.
- **D5 -- Scenario DCF (pessimistic / base / optimistic)** produces an explicit point estimate per named scenario; **the MC run** produces the full distribution.
  Motivation: named scenarios stay individually auditable ("what does pessimistic mean?"); the MC covers the space between them.
- **D6 -- Correlation structure: hardcoded named constants (Cholesky), visible and counted in every run (D26).**
  Motivation: correlation is a structural choice and must be inspectable in every run, not a hidden learned artifact.
- **D7 -- Full provenance on every assumption:** `source` in {estimated, user, llm, heuristic}, `seed`, `range`, `confidence`, `model`, `field`.
  Motivation: without provenance a number cannot be audited; every assumption must be traceable back to where it came from.
- **D8 -- LLM reclassification is the core of the project and is mandatory in every v1 run; a deterministic or rule-based fallback is explicitly rejected and out of scope. If the stage fails the accuracy gate, the project stops and its premise is revisited.**
  Motivation: mapping iXBRL onto the canonical schema is where the project creates value and where it can fail; a fallback would be a permanent low-quality path hiding that failure. Proving the gate first is the cheapest possible test of the premise.
- **D9 -- Hard-reject rules, non-negotiable:** g >= WACC; shares <= 0; negative revenue or COGS (non-financial); any key not in the frozen schema (anti-hallucination gate).
  Motivation: a DCF run on impossible inputs yields a plausible-looking wrong number; a reject with a reason beats a silent pass.
- **D10 -- JSON-first I/O:** input = JSON; output = structured JSON.
  Motivation: files inspect and diff better than a UI, stay the source of truth, and keep the repo free of a web/DB stack the project never needs.
- **D11 -- Canonical schema is versioned and frozen before LLM work begins; after the freeze, additive-only changes.**
  Motivation: the schema is the LLM's target -- moving it mid-run would silently invalidate all previously trained prompts and results, so freeze first then evolve additively.
- **D12 -- Scenario realism filter (core feature):** contradictory draws are discarded *before* any DCF evaluation (not merely penalized) -- e.g. a decreasing profit margin co-occurring with a capex spike in the same year; a set of named, hardcoded rules with visible rejection counts on every run; thresholds are fixed constants in v1.
  Motivation: an MC that evaluates impossible scenarios wastes draws and flattens the tails; rejecting them up front keeps every evaluated draw economically coherent.
- **D13 -- Python 3.12 + NumPy; no ML framework beyond Ollama's client; no pandas; no web framework.**
  Motivation: a DCF needs basic algebra, not a data-science framework; fewer deps means smaller audit surface and zero framework lock-in.
- **D14 -- v1 LLM runtime: local Ollama only, behind a thin model-agnostic client (OpenAI-compatible protocol); default model class qwen3.8:27b; no cloud APIs.**
  Motivation: local inference is the point -- reproducible, offline, cheap; 30b-class is enough for constrained classification against a fixed schema; the thin client keeps the model swappable.
- **D15 -- CLI only for v1:** single entry point with `parse` / `run` / `report` subcommands.
  Motivation: a CLI is testable, scriptable and needs no server process; a web UI would add attack surface and maintenance for no v1 value.
- **D16 -- Flat JSON files, no database in v1** (`data/{ticker}/fiscal-{YYYY}.json`).
  Motivation: at 10 tickers x 10 years a file layout is the simplest store that supports diff and audit; a DB earns its complexity only after that.
- **D17 -- Deterministic by default:** `--seed` honored; all stochastic behavior seeded.
  Motivation: a research tool must produce identical output for identical input + seed or results cannot be compared across versions.
- **D18 -- License: Apache 2.0; model weights are not redistributed; only prompts + pipeline code are licensed.**
  Motivation: Apache is permissive-but-attributed, matches the research-tool spirit and keeps the file-header convention auditable; redistributing weights would collide with model licenses.
- **D19 -- Fiscal-year policy: stub periods are ignored; the valued period is the last completed fiscal year; partial years are excluded, v1.**
  Motivation: a stub period is not a real year -- valuing on it skews every annualized assumption; trailing-12-month as-of the last completed FY is the only clean basis (also closes non-standard calendar ends).
- **D20 -- US issuers only, v1.** Ticker must be a US issuer with statements on SEC EDGAR (XBRL); non-US listings are hard-rejected.
  Motivation: XBRL tagging quality, GAAP vocabulary and filing format are the reclassification stage's whole assumption; non-US issuers would force a second schema.
- **D21 -- History depth: 10 fiscal years where available, below 10 usable years -> hard reject** with the reason surfaced.
  Motivation: the DCF is trained on the issuer's own trajectory -- 10 years is the floor for a stable margin/capex/growth pattern.
- **D22 -- Correlations between DCF inputs: hardcoded named constants (Cholesky) in v1 -- not fitted, not user-tunable.**
  Motivation: learning a covariance matrix from a 10-ticker universe is not statistically defensible; constants are honest, documented and changeable in one place.
- **D23 -- Non-canonical items map into a first-class `Other` bucket; never a failure, never silently dropped.**
  Motivation: the schema is intentionally narrower than a full filing; the only honest place for the rest is a visible `Other` line, and reconciliation still forces the total to balance.
- **D24 -- LLM validation against a frozen golden set of manually reclassified statements; the identity/reconciliation checks pass on every run, failures on golden fixtures are hard rejects.**
  Motivation: the golden set is the ground truth for the reclassification stage and the permanent regression fixture -- every model or prompt change is measured against the same statements, forever.
- **D25 -- Share count: diluted shares outstanding, as reported.**
  Motivation: DCF value is per-share value; diluted is the conservative, comparable choice and what the market prices.
- **D26 -- EDGAR filings: R-files over companyfacts.**
  Motivation: R-files are more difficult to fetch and impose the use of regexes, but the preserve the original financial items names attributed to the company.


## Open decisions (default in force until settled)

- **O1 -- Beta source: ticker-computed (10y beta) vs industry prior.** Default: blend -- the ticker's own 10y beta where data quality is sufficient, else the industry prior from O3.
  Motivation: a ticker-computed beta will predict future beta better for the same company, but an industry prior is the floor for tickers with noisy or short histories.
