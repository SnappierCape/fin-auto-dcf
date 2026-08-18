# Contributing

Thanks for contributing to **fin-auto-dcf**. Keep it small, tested, and
licensed.

## Before you start
Read [PROJECT.md](PROJECT.md) — the decision list (D1–D29) is the source of
truth for what's in scope and *why*. Match that, not your local assumptions.

## License & file headers
This project is **Apache 2.0** ([LICENSE](LICENSE)). §7.1 requires the
copyright notice to be preserved in every file you ship, so **every new
`.py` / shell / `.toml` file must begin with**:

```text
# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
```

(A `#` comment style is conventional here; adapt to the file's comment
token.) If you vendor code from elsewhere, keep its original source
attribution above yours.

## Header check
A stdlib-only guard enforces the rule (no third-party deps):

```bash
python scripts/check_license_header.py            # scan the whole repo
python scripts/check_license_header.py <file>      # check specific file(s)
```

To run it automatically on every commit (requires `pip install pre-commit`):

```bash
pre-commit install
```

## Committing
- Small, single-purpose commits; describe *why* in the message.
- Every module lands with a test in `tests/` (see the TDD rule in
  PROJECT.md) before it's considered done.
- No secrets, no `data/` dumps, no model weights committed.
