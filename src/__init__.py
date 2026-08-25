# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""fin-auto-dcf — probabilistic DCF over LLM-reclassified EDGAR 10-Ks.

Package root. The staged pipeline modules (``cli``, ``schemas``, ``ingest``,
``llm``, ``dcf``, ``mc``) land with their Phase 0–6 work; for now this file
makes the project importable under ``uv run`` so the environment contract
(``tests/test_env.py``) can pin the interpreter + required libraries.
"""

__version__ = "0.1.0"
