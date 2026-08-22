# 💹 Auto-DCF — Probabilistic DCF Pipeline

> A fair-value pipeline for US equities that returns a **probability distribution of intrinsic value per share** — not a point estimate.
> Real filings pulled from **SEC EDGAR (iXBRL)**, mapped onto a frozen canonical schema by a **local LLM (Ollama)**, valued by a classic **DCF** kernel, and turned into a full distribution by **10,000 correlated Monte Carlo extraction**; the DCF parameters are drawn on a per-extraction basis with Cholesky correlation matrix, economically irrealistic combinations are rejected and re-drawn.
> Deterministic, seedable, and provenanced down to every assumption — a research tool, *not* investment advice.

[![Status: Design Phase](https://img.shields.io/badge/Status-Design%20Phase-yellow?style=flat-square)]()
[![Python](https://img.shields.io/badge/Python-3.12-blue?style=flat-square&logo=python&logoColor=white)]()
[![NumPy](https://img.shields.io/badge/NumPy-2.x-013243?style=flat-square&logo=numpy&logoColor=white)]()
[![LLM Runtime](https://img.shields.io/badge/LLM%20Runtime-Ollama-3f8cff?style=flat-square&logo=ollama&logoColor=white)]()
[![I/O](https://img.shields.io/badge/I%2FO-JSON-7d868e?style=flat-square&logo=json&logoColor=white)]()
[![License](https://img.shields.io/badge/License-Apache%202.0-5a6b7c?style=flat-square&logo=apache&logoColor=white)]()

---

## 💡 Motivation

A classic DCF funnels roughly ten assumptions — WACC, terminal growth, margin trajectory… — into a single number.
Each assumption carries uncertainty; multiplying them together just hides it. Notoriously, the DCF final output is heavily sensitive to any of the inputs (usually a sensitivity matrix addresses this problem). This project makes the uncertainty *the output*:

- **Scenario DCF** produces an explicit valuation per named scenario (base / optimistic / pessimistic).
- **Monte Carlo** draws 10,000 correlated parameter sets and returns the full fair-value distribution: **median, p10, p90, and P(intrinsic value > current price)**, as well as a **clustered histogram**.

The differentiator — and the main technical risk — is the **LLM reclassification stage**: reading each issuer's iXBRL filing and mapping it onto one frozen canonical schema, so the DCF kernel always sees the same keys. It is built first, validated against a hand-reclassified golden set *before* any valuation work, and is a hard GO/NO-GO gate for the whole project.

---

## 🔁 Pipeline

```
 SEC EDGAR               Local LLM (Ollama)             DCF kernel                       Monte Carlo
┌──────────────┐        ┌─────────────────────┐        ┌────────────────────┐           ┌─────────────────────┐
│ 10y iXBRL    │──────> │ reclassify onto the │──────> │ WACC · growth ·    │─────────> │ 10,000 correlated   │
│ filings      │        │ frozen canonical    │        │ terminal value →   │           │ draws (Cholesky),   │
└──────────────┘        │ schema              │        │ fair value / share │           │ reject-and-redraw   │
                        └─────────────────────┘        └────────────────────┘           └─────────────────────┘
```

---

## 📦 Repository Structure

The repository is documentation-only at this stage — the layout below is the intended structure starting with Phase 0.

```text
fin-auto-dcf/
│
├── PROJECT.md              # Decision log — the authoritative D# (closed) and O# (open) list.
├── README.md               # This file.
├── CONTRIBUTING            # Guidelines for contributors.
├── LICENSE                 # Apache 2.0.
│
├── src/                    # Pipeline code (from Phase 0 onward)
│   ├── cli.py              #   Single entry point: parse / run / report subcommands
│   ├── schemas/            #   Frozen canonical schema (code + JSON)
│   ├── ingest/             #   EDGAR iXBRL → canonical JSON statements
│   ├── llm/                #   Ollama reclassification stage (thin, model-agnostic)
│   ├── dcf/                #   Valuation kernel (WACC, growth, terminal value)
│   └── mc/                 #   Monte Carlo + distribution engine
|
├── scripts                 # Helper scripts, mainly for development/testing
│
├── data/                   # data/{ticker}/fiscal-{YYYY}.json — flat files, no database
│   └── golden/             #   Hand-reclassified fixture set (the validation backbone)
│
├── tests/                  # A test file for each module in /src
└── pyproject.toml          # UV project configuration
```

---

## 🚀 Getting Started

**Status: design phase.** The frozen decisions are in [PROJECT.md](PROJECT.md) (authoritative D# / O# list); pipeline code lands phase by phase. When it does, the planned CLI looks like this:

```bash
git clone git@github.com:SnappierCape/fin-auto-dcf.git
cd /your-path/fin-auto-dcf

uv lock
uv sync

# Running (use CIK not Ticker ─ example for Microsoft Corp.)
uv run fin-auto-dcf parse 0000789019              # fetch filing → canonical JSON
uv run fin-auto-dcf run 0000789019 --seed 42      # full pipeline → fair-value distribution
uv run fin-auto-dcf report 0000789019             # human-readable summary of the run
```

Requires a local [Ollama](https://ollama.com) instance with at least 64.000 token of context. Recommended at least an 8b-class model.

---

## 🔭 Scope

**v1 is deliberately narrow:**

- US issuers only, non-financial companies
- 10 years of statement history (minimum 5 usable years)
- Scenario presets: base / optimistic / pessimistic
- Local LLM only — self-hosted, reproducible, no cloud APIs
- CLI + JSON first — no web UI, no database

**Non-goals (v1):** relative valuation / comps, M&A & LBO, banks & insurers, non-US issuers, real-time pricing.

---

## 🗺️ Roadmap

- [x] **Design & freeze** — objectives, premises, decision list (D1–D29), canonical schema
- [ ] **Phase 0** — Foundation: scaffolding, CI, frozen canonical schema, golden fixture set
- [ ] **Phase 1** — LLM reclassification — the **GO/NO-GO gate**
- [ ] **Phase 2** — Data ingestion (real EDGAR statements)
- [ ] **Phase 3** — Estimation & DCF kernel
- [ ] **Phase 4** — Monte Carlo & fair-value distribution
- [ ] **Phase 5** — Backtest against realized outcomes
- [ ] **Phase 6** — Hardening & v1 release

---

## ⚠️ Disclaimer

This is a research and learning tool. All outputs are labeled **research input** and must not be construed as investment advice, a recommendation, or an offer to buy or sell any security.

---

## 📖 Documentation

**[PROJECT.md](PROJECT.md)** is the **decision log**: the authoritative list of all decisions, closed (D1–D29) and open (O1–O9), each with its motivation. It contains no other content — deliberately.

Contributors: see **[CONTRIBUTING.md](CONTRIBUTING.md)** — license policy, the required file-header convention, and the header check.

---

## 👤 Author

Built as a self-directed research project at the intersection of **quantitative finance** and **LLM tooling**, with the explicit goal of making a full valuation pipeline readable, reproducible, and auditable.
Every design decision is tracked as a numbered entry in the project document.

---

## 📃 License

Apache 2.0 © 2026 Ulrico Luigi Nava — see [LICENSE](LICENSE) for details.
Model weights are not redistributed; only prompts + pipeline code are licensed.

*By contributing to this repository, you agree that your contributions will be licensed under its Apache 2.0 License.*

---

## 🙏 Acknowledgements

- [SEC EDGAR](https://www.sec.gov/cgi-bin/browse-edgar) — the primary source of issuer financial data (iXBRL).
- [Ollama](https://ollama.com) — local, open-weight LLM serving.
- The hand-reclassified golden fixture set: the thing the whole project is validated against before a single dollar of DCF math gets a say.
