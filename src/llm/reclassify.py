# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0

"""Reclassifies one converted 10-K statement into the canonical schema via Ollama.

Reads a record array from ``convert.py`` (``data/converted/``), sends it
with the ``prompt.md`` system prompt to a local Ollama
``/api/chat`` endpoint over plain HTTP (stdlib ``urllib``, no extra
dependencies), and lands the annotated array in ``data/reclassified/``.

The model's reply uses the prompt's own four-field contract and is joined
back onto the input records by ``id``:

    id        ─ echo of the input record's id
    target    ─ "<bucket>.<line_item>" for mapped lines, else null
    transform ─ canonical transform value for mapped lines, else null
    reason    ─ short reason; mandatory when the line is unmapped

Every input record must be answered exactly once; duplicate, missing, or
invented ids (or a wrong field shape) abort the run loudly — nothing is
dropped or silently accepted.  Classification is deterministic work, so
the call runs at ``temperature 0`` and ``format: "json"`` forces the
reply to be parseable JSON.

CLI:
    uv run src/llm/reclassify.py 104169 cf
    uv run src/llm/reclassify.py 0000104169 bs --model gemma3:27b

The reclassified file ``data/reclassified/`` is the output of this
pipeline stage; ``data/converted/`` (the input) stays disposable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


# =============================================================================
# Paths and constants
# =============================================================================


ROOT = Path(__file__).resolve().parents[2]

SYSTEM_PROMPT = ROOT / "src" / "llm" / "prompt.md"
SCHEMA_FILE = ROOT / "data" / "schema.json"
CONVERTED_DIR = ROOT / "data" / "converted"
RECLASSIFIED_DIR = ROOT / "data" / "reclassified"

PLACEHOLDER = "{{CANONICAL_SCHEMA}}"

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.8:27b-agent"

TIMEOUT_SECONDS = 590
KEEP_ALIVE = "30m"

ENCODING = "utf-8"

STMTS = ("is", "bs", "cf")
DECISION_KEYS = ("id", "target", "transform", "reason")

#: The 7 fields ``convert.py`` writes per record; the reclassifier never
#: touches them, it only appends the three decision fields.
INPUT_KEYS = ("id", "stmt", "order", "level", "label", "tag", "has_value")

#: Metadata keys that mark a dict as a *line item* (a leaf) rather than
#: a container of line items.  A container's values are line items; a
#: leaf's keys are these.  This is what lets the renderer tell
#: ``net_cash`` (a leaf bucket) from ``income_statement`` (a container).
_META_KEYS = {
    "raw_key", "value", "transform", "provenance",
    "derived_from", "rule_version", "notes", "label"
}


# =============================================================================
# Helpers
# =============================================================================


def normalize_cik(cik: str) -> str:
    """Returns ``cik`` zero-padded to 10 digits (EDGAR CIK form).

    Accepts ``"104169"`` or the already-padded ``"0000104169"``.
    Non-digit input is rejected rather than silently mangled.
    """
    if not cik.isdigit() or len(cik) > 10:
        raise ValueError(f"CIK must be 1-10 digits, got {cik!r}")
    return cik.zfill(10)


def load_records(path: Path) -> list[dict]:
    """Loads and validates a converted record array.

    Fails loud on: unreadable file, not a JSON array, empty array, a
    record missing any of the 7 ``convert.py`` fields (or an unknown
    extra field), missing/non-string ids, and duplicate ids.
    """
    try:
        raw = json.loads(path.read_text(encoding=ENCODING))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read records from {path}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path} must be a non-empty JSON array")
    seen: set[str] = set()
    for i, rec in enumerate(raw):
        if not isinstance(rec, dict) or set(rec) != set(INPUT_KEYS):
            keys = set(rec) if isinstance(rec, dict) else type(rec).__name__  # NOTE: Understand.
            raise ValueError(
                f"{path}[{i}]: record must have exactly the "
                f"fields {INPUT_KEYS}, found {sorted(keys)}"
            )
        rid = rec["id"]
        if not isinstance(rid, str) or not rid:
            raise ValueError(f"{path}[{i}]: 'id' must be a non-empty string")
        if rid in seen:
            raise ValueError(f"{path}[{i}]: duplicate id {rid!r}")
        seen.add(rid)
    return raw


def _leaf_paths(node, prefix: str, out: list[tuple[str, dict]]) -> None:  # NOTE: Not sure why this is needed.
    """Recursively collects ``(dotted_path, metadata)`` for every leaf."""
    if not isinstance(node, dict):
        out.append((prefix, {"value": node}))
    elif set(node) & _META_KEYS:
        # A dict whose keys include metadata is a leaf line item.
        out.append((prefix, node))
    else:
        for key, val in node.items():
            _leaf_paths(val, f"{prefix}.{key}" if prefix else key, out)


def build_schema_block(schema: dict) -> str:
    """Renders the canonical-schema dictionary for ``prompt.md`` injection.

    Emits every leaf line item as a dotted path (e.g.
    ``cfo.d_a``, ``net_cash``) annotated with the schema's own
    ``raw_key`` (the XBRL concept — the model maps rows by concept),
    ``transform`` and ``provenance``/``derived_from``.  The full set of
    valid ``target`` strings is thus enumerated verbatim, plus the
    ``computed_values`` leaves and the canonical ``transform``
    vocabulary.  Faithful to ``data/schema.json`` regardless of which
    buckets are containers and which are leaves.
    """
    lines: list[str] = []
    statements = schema.get("statements", {})
    leaves: list[tuple[str, dict]] = []
    for bucket, body in statements.items():
        _leaf_paths(body, bucket, leaves)
    for path, meta in leaves:
        bits: list[str] = []
        for key in ("raw_key", "transform"):
            val = meta.get(key)
            if val not in (None, ""):
                bits.append(f"{key}={val}")
        src = meta.get("provenance") or meta.get("derived_from")
        if src not in (None, "", []):
            bits.append("src=" + json.dumps(src, ensure_ascii=False))
        suffix = "  " + "  ".join(bits) if bits else ""
        lines.append(f"  {path}{suffix}")

    computed = schema.get("computed_values", {}) or {}
    for name, spec in computed.items():
        label = spec.get("label", name) if isinstance(spec, dict) else str(spec)
        lines.append(f"  computed.{name}: {label} (computed, not a row)")

    transform = schema.get("conventions", {}).get("transform", "")
    if transform:
        lines.append(f"transform values: {transform}")

    if not statements:
        raise ValueError("schema has no 'statements' to render")
    return "\n".join(lines).rstrip()


def load_system_prompt() -> str:
    """Returns ``prompt.md`` with the canonical schema injected.

    The placeholder is substituted exactly once; anything else (missing
    placeholder, several of them, an unreadable prompt, a schema that
    cannot be rendered) is a hard error — the pipeline must never send a
    prompt whose dictionary is missing or duplicated.
    """
    text = SYSTEM_PROMPT.read_text(encoding=ENCODING)
    if text.count(PLACEHOLDER) != 1:
        found = text.count(PLACEHOLDER)
        raise ValueError(
            f"{SYSTEM_PROMPT.name} must contain exactly one "
            f"{PLACEHOLDER}, found {found}"
        )
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    block = build_schema_block(schema)
    if not block.strip():
        raise ValueError(
            "canonical-schema block rendered empty from "
            f"{SCHEMA_FILE.name}"
        )
    return text.replace(PLACEHOLDER, block)


def call_ollama(system: str, records: list[dict], url: str, model: str) -> dict:
    """Posts the chat payload and returns the server's JSON response.

    One statement per call, non-streaming, ``format: "json"`` so the
    reply is structurally valid JSON, ``temperature 0`` because this is
    a lookup, not a generation.  ``keep_alive`` keeps the model warm
    when a batch of statements runs back to back.
    """
    payload = json.dumps({
        "model": model,
        "stream": False,
        "format": "json",
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(records, ensure_ascii=False)},
        ],
    }).encode(ENCODING)
    request = urllib.request.Request(
        url.rstrip("/") + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", "replace")
        raise RuntimeError(f"Ollama HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"cannot reach Ollama at {url}: {exc}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        snippet = body[:300]
        raise RuntimeError(
            f"Ollama at {url} returned non-JSON: "
            f"{snippet!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Ollama at {url} returned a non-object payload")
    return parsed


def parse_response(body: dict) -> list[dict]:
    """Extracts and parses the assistant's JSON array from a /api/chat reply.

    Enforces the completed-generation markers from the API contract
    (``done`` true, ``done_reason`` "stop"), then parses
    ``message.content`` as JSON.  A non-list payload or unparsable
    content is reported with an excerpt so the model's exact reply is
    visible in the log.
    """
    if body.get("done") is not True or body.get("done_reason") != "stop":
        raise RuntimeError(
            "incomplete generation: "
            f"done={body.get('done')!r} "
            f"done_reason={body.get('done_reason')!r}"
        )
    message = body.get("message")
    if not isinstance(message, dict):
        raise RuntimeError(f"response has no 'message' object: {body!r}")
    content = message.get("content")
    if isinstance(content, list):
        parsed = content
    else:
        if not isinstance(content, str):
            raise RuntimeError(f"message.content is not set: {message!r}")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"model reply is not valid JSON: {content[:300]!r}"
            ) from exc
    if not isinstance(parsed, list):
        raise RuntimeError(
            f"model reply is a JSON {type(parsed).__name__}, "
            f"expected an array: {content[:300]!r}"
        )
    return parsed


def validate_decisions(
    records: list[dict],
    decisions: list[dict]
) -> dict[str, dict]:
    """Enforces the 4-field contract and returns an id-keyed decision map.

    Mapped line: target & transform non-null, reason optional.
    Unmapped line: target & transform null, reason a non-empty string.
    All contract violations are collected and reported together with the
    offending ids, so a bad batch explains itself in one pass.
    """
    by_id: dict[str, dict] = {}
    problems: list[str] = []
    input_ids = {rec["id"] for rec in records}

    def bad(decision: dict, reason: str) -> None:
        rid = decision.get("id", "<missing id>") if isinstance(decision, dict) else repr(decision)[:40]
        problems.append(f"{rid}: {reason}")

    for position, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            problems.append(f"[{position}]: not an object")
            continue
        if set(decision) != set(DECISION_KEYS):
            bad(
                decision,
                f"fields must be exactly {DECISION_KEYS}, "
                f"got {sorted(decision)}"
            )
            continue
        rid = decision["id"]
        if rid not in input_ids:
            bad(decision, "id not present in the input records")
            continue
        if rid in by_id:
            bad(decision, "duplicate id")
            continue
        target = decision["target"]
        transform = decision["transform"]
        reason = decision["reason"]
        if target is None or transform is None:
            if not (isinstance(reason, str) and reason.strip()):
                bad(decision, "unmapped lines need a non-empty reason")
        else:
            if not (isinstance(target, str) and target) or not (isinstance(transform, str) and transform):
                bad(decision, "mapped lines need non-empty target and transform")
            elif not isinstance(reason, (str, type(None))):
                bad(decision, "reason must be a string or null")
        by_id[rid] = decision

    for rid in sorted(input_ids - set(by_id)):
        problems.append(f"{rid}: never answered by the model")
    if problems:
        raise RuntimeError(
            "decision contract violations:\n  "
            + "\n  ".join(problems)
        )
    return by_id


def annotate(records: list[dict], by_id: dict[str, dict]) -> list[dict]:
    """Returns input-ordered records extended with the decision fields.

    Each original record is emitted unchanged (same 8 fields, same
    order) with ``target``, ``transform`` and ``reason`` appended in
    that order.
    """
    out: list[dict] = []
    for rec in records:
        decision = by_id[rec["id"]]
        merged = dict(rec)
        merged["target"] = decision["target"]
        merged["transform"] = decision["transform"]
        merged["reason"] = decision["reason"]
        out.append(merged)
    return out


# =============================================================================
# Main
# =============================================================================


def main() -> int:
    """CLI entry point: reclassifies one statement, lands the output file."""
    parser = argparse.ArgumentParser(
        description="Reclassify a convert.py output via Ollama."
    )
    parser.add_argument("cik", help="EDGAR CIK, e.g. 104169")
    parser.add_argument("stmt", choices=STMTS, help="statement: is | bs | cf")
    parser.add_argument(
        "--url", default=DEFAULT_URL,
        help=f"Ollama base URL (default {DEFAULT_URL})"
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"model name (default {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--out", default=None,
        help="output path (default data/reclassified/<cik>_<stmt>.json)"
    )
    args = parser.parse_args()

    cik = normalize_cik(args.cik)
    stem = f"{cik}_{args.stmt}"
    src = CONVERTED_DIR / f"{stem}.json"
    out = Path(args.out) if args.out else RECLASSIFIED_DIR / f"{stem}.json"
    if not src.is_file():
        print(
            f"error: {src} not found — run "
            f"uv run src/llm/convert.py {cik} {args.stmt} first",
            file=sys.stderr
        )
        return 1

    records = load_records(src)
    system = load_system_prompt()

    started = time.monotonic()
    body = call_ollama(system, records, args.url, args.model)
    decisions = parse_response(body)
    by_id = validate_decisions(records, decisions)
    merged = annotate(records, by_id)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False)
        + "\n", encoding=ENCODING
    )

    elapsed = time.monotonic() - started
    mapped = sum(1 for rec in merged if rec["target"] is not None)
    print(
        f"reclassified {stem}: {len(merged)} records "
        f"({mapped} mapped, {len(merged) - mapped} unmapped) "
        f"in {elapsed:.1f}s -> {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
