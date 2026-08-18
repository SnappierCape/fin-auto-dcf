#!/usr/bin/env python3
# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0
"""Apache-2.0 license-header check for fin-auto-dcf.

Policy — Apache-2.0 section 7.1 requires that every modified file
carries the copyright notice. In practice: every checkable file must
start with the standard two-line header (comment-stripped match in the
first 10 lines):

    Copyright 2026 Ulrico Luigi Nava
    SPDX-License-Identifier: Apache-2.0

Usage:
    python scripts/check_license_header.py              # scan the whole repo
    python scripts/check_license_header.py FILE [FILE]  # pre-commit mode

Only text formats with comment syntax are checkable (.py .sh .bash
.toml). Markdown, JSON (data), and binary assets are exempt by design —
docs are attributed in-repo via the README/LICENSE footer.
Exit code 0 = all good, 1 = header(s) missing.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories never scanned (VCS, venvs, caches, generated/raw data).
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__",
    ".idea", ".vscode", ".mypy_cache", ".ruff_cache", "node_modules",
    "data", "output",
}

# Files that carry their own license text and are exempt from the header rule.
SKIP_FILES = {"LICENSE", "NOTICE"}

# Comment-bearing extensions we can meaningfully check.
CHECKED_EXTS = {".py", ".sh", ".bash", ".toml"}


def has_header(path: Path) -> bool:
    """True if the file's first 10 lines state the Apache-2.0 copyright."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False  # unreadable/binary -> treat as failure (should not be checked anyway)
    head = "\n".join(raw.splitlines()[:10])
    return "Apache-2.0" in head and "Copyright" in head


def iter_files(args: list[str]):
    if args:
        # pre-commit mode: the hook already filtered by extension, this is a safety net
        for a in args:
            p = Path(a)
            if p.suffix in CHECKED_EXTS and p.is_file():
                yield p
        return
    for p in sorted(REPO_ROOT.rglob("*")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        if p.suffix not in CHECKED_EXTS or p.name in SKIP_FILES:
            continue
        yield p


def main(argv: list[str]) -> int:
    files = list(iter_files(argv[1:]))
    if not files:
        print("check_license_header: no checkable files found — OK")
        return 0

    bad = [f for f in files if not has_header(f)]
    if bad:
        print(f"check_license_header: {len(bad)}/{len(files)} file(s) missing the Apache-2.0 header:")
        for b in bad:
            try:
                rel = b.relative_to(REPO_ROOT)
            except ValueError:
                rel = b
            print(f"  - {rel}")
        print("\nRequired first two lines (adjust comment style as needed):")
        print("    Copyright 2026 Ulrico Luigi Nava")
        print("    SPDX-License-Identifier: Apache-2.0")
        return 1

    print(f"check_license_header: {len(files)} file(s) checked — all carry the header — OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
