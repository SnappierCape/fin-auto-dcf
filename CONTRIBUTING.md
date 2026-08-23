# Contributing

Thanks for contributing to **fin-auto-dcf**. Keep it small, tested, and
licensed.

## Before you start
Read [PROJECT.md](PROJECT.md) — that is the source of
truth for what's in scope and *why*. Match that, not your local assumptions.

## License & file headers
This project is licensed under **Apache 2.0** ([LICENSE](LICENSE)). Apache requires the
copyright notice to be preserved in every file you ship, so **every new
`.py` / shell / `.toml` file must begin with**:

```text
# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
```

(A `#` comment style is conventional here; adapt to the file's comment
token.) If you vendor code from elsewhere, keep its original source
attribution above yours.

*By contributing to this repository, you agree that your contributions will be licensed under its Apache 2.0 License.*

## Header check
A stdlib-only guard enforces the rule (no third-party deps):

```bash
python scripts/check_license_header.py             # scan the whole repo
python scripts/check_license_header.py <file>      # check specific file(s)
```

To run it automatically on every commit (requires `pip install pre-commit`):

```bash
pre-commit install
```

## Code style — PEP 8 & PEP 257
A project rule: all Python code, including comments and docstrings,
follows **PEP 8** and **PEP 257**. Please match this in
contributions — it keeps the codebase uniform without any tooling
enforcement.

## Commenting and line length
For simplicity, we use a 3-level header system:

```python
# =============================================================================
# Header 1
# =============================================================================

# -----------------------------------------------------------------------------
# Header 2
# -----------------------------------------------------------------------------

# ── Header 3 ─────────────────────────────────────────────────────────────────

# Normal comment.

a = 2  # inline comment
```

We try to keep every line of code below or equal to 79 charachters. Header's lines are exactly 79 charachters long.

## Branches
We only push to branches, the branch name follows a specific naming convention and the issue's number. 

The naming convention is: `{type}/{issue n°}_short-descriptive-title`.

Example:

```Plaintext
fix/35_download-statement-broken-url
```

## Committing
- Small, single-purpose commits; describe *why* in the message.
- Every module lands with a test in `tests/` before it's considered done.
- No secrets, no `data/` dumps, no model weights committed.
- No stale code, every piece of code that is not activly used and maintained must be deleted.
- The commit message follows [Conventional Commits](https://gist.github.com/qoomon/5dfcdf8eec66a051ecd85625518cfd13) with no imperative tone.