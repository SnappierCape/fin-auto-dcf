# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0

"""Maps one converted statement into the canonical schema's item names.

Reads a record array from "convert.py" ("data/converted/"), pairs it with
the "prompt.md" system prompt, injects 3 hand-made example mappings into
the system prompt, and asks a local Ollama "/api/chat"
endpoint (plain HTTP, stdlib "urllib", no dependencies) for one thing
only: a mapping of each raw line to a canonical target.

The model uses the four-field contract defined in "src/llm/prompt.md" and
links every item using the "id" key from the input file:

    id        ─ unique identifier for each input item
    target    ─ "<bucket>.<line_item>" for mapped lines, else null
    transform ─ the transformation applied to the item
    reason    ─ a short reason; mandatory when the line is unmapped

The LLM never sees or prints a number.  When several raw items together
form one canonical item, it points them all at the same target; the code
then sums them deterministically.  It is a mapper, not a calculator.

The canonical schema itself never enters the prompt.  The model calibrates
from hand-made converted --> mapped examples injected into "prompt.md" at
runtime: each example comes from "data/example_converted/".

Every input item must be answered exactly once; duplicate, missing, or
invented ids (or a wrong field shape) abort the run loudly - nothing is
dropped or silently accepted.  Classification is deterministic work, so the
call runs at temperature 0 and asks Ollama for JSON-only output.

Usage:

    uv run src/llm/map.py 104169 cf
    uv run src/llm/map.py 0000104169 bs --model gemma3:27b

The mapped file in "data/mapped/" is the output of this pipeline stage;
"data/converted/" (the input) stays disposable.
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
CONVERTED_DIR = ROOT / "data" / "converted"
EXAMPLE_DIR = ROOT / "data" / "example_mappings"
GOLDEN_DIR = ROOT / "data" / "golden"
MAPPED_DIR = ROOT / "data" / "mapped"

# This is useful to find the section of the prompt where the few-shots exaplme are located.
FEW_SHOT_ANCHOR = "<start_few_shots>"

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.8:27b-agent"

TIMEOUT_SECONDS = 590
KEEP_ALIVE = "30m"

ENCODING = "utf-8"

STATEMENTS = ("is", "bs", "cf")
MAP_KEYS = ("id", "target", "transform", "reason")

# The 7 attributes that "convert.py" specifies for each financial item.
INPUT_KEYS = ("id", "stmt", "order", "level", "label", "tag", "has_value")


# =============================================================================
# Input loading and few-shot assembly
# =============================================================================

def normalize_cik(cik: str) -> str:
    """Returns company's CIK zero-padded to 10 digits (EDGAR CIK form).

    Accepts "104169" or the already-padded "0000104169".
    Non-digit input is rejected rather than silently mangled.
    """
    if not cik.isdigit() or len(cik) > 10:
        raise ValueError(f"CIK must be numeric 1-10 digits, got {cik!r}")
    return cik.zfill(10)


def load_records(path: Path) -> list[dict]:
    """Loads and validates a converted record array.

    Fails loud on: unreadable file, not a JSON array, empty array, a
    record missing any of the 7 attributes (or carrying an unknown extra
    attribute), a missing/non-string id, and duplicate ids.
    """
    try:
        raw = json.loads(path.read_text(encoding=ENCODING))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read records from {path}: {exc}") from exc
    
    # The converted file has to be a list at the first level.
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path} must be a non-empty JSON array")

    # Initialize set to track duplicate ids.
    seen: set[str] = set()
    for i, rec in enumerate(raw):
        
        # Check in the dict has all the 7 attributes.
        if not isinstance(rec, dict) or set(rec) != set(INPUT_KEYS):
            
            # Fallback to the string name of the data type (str | int | list | etc...)
            keys = set(rec) if isinstance(rec, dict) else type(rec).__name__
            raise ValueError(
                f"{path}[{i}]: record must have exactly the fields "
                f"{INPUT_KEYS}, found {sorted(keys)}"
            )
            
        rec_id = rec["id"]
        if not isinstance(rec_id, str) or not rec_id:
            raise ValueError(f"{path}[{i}]: 'id' must be a non-empty string")
        if rec_id in seen:
            raise ValueError(f"{path}[{i}]: duplicate id {rec_id!r}")
        seen.add(rec_id)
        
    return raw


def load_mapped_examples() -> list[dict[str, list[dict], list[dict]]]:
    """Builds the 3 few-shot (company, input_records, output_map) pairs.

    Inputs and outputs come from "data/example_mappings/".
    Inputs are the "..._converted.json" files, outputs are the 
    "..._mapped.json" files.  A pair forms when both the
    "converted" and "mapped" files are found.
    
    The output of this method is fed into build_few_shots_block() to render
    the markdown block of examples to feed into the LLM.
    """
    
    # Initialize empty dict to collect example files.    
    examples: dict[str, dict] = {}

    if EXAMPLE_DIR.is_dir():
        
        # Cycle each file in the dir.
        for path in sorted(EXAMPLE_DIR.glob("*.json")):
            tick, _, file_type = path.stem.split("_", maxsplit=2)  # split by "_"
            
            # Add the cik only one time.
            if tick not in examples:
                examples[tick] = {"tick": tick}
            
            content = json.loads(path.read_text(encoding=ENCODING))
            
            # Group by cik and divide input from output.
            if file_type == "converted":
                examples[tick]["input"] = content
            elif file_type == "mapped":
                examples[tick]["output"] = content

        # Check if all the files are present.
        if len(examples) != 3:
            raise ValueError(
                f"Expected 3 example CIK pairs, found {len(examples)}"
            )
    else:
        print(f"warning: {EXAMPLE_DIR} not found", file=sys.stderr)

    return list(examples.values())


def build_few_shots_block(examples: list[tuple[str, list[dict], list[dict]]]) -> str:
    """Renders the "## Few-shot examples" section of the system prompt.

    Mirrors the hand-crafted structure in "prompt.md": one
    "### Example N (COMPANY)" block per pair, each with an
    "Input" fence (the converted records) and an "Output" fence (the
    4-field map).
    
    The block gets plugged in below the "<start_few_shots>".
    """
    if not examples:
        raise ValueError(f"Few-shot examples not found.")
    
    # Initialize empty list to hold the 3 blocks.
    blocks = []
    
    # Populate with the 3 examples one below the other.
    for i, example in enumerate(examples, start=1):
        tick = example["tick"]
        input_json = json.dumps(example["input"], ensure_ascii=False, indent=2)
        output_json = json.dumps(example["output"], ensure_ascii=False, indent=2)
        
        # Append new block to the blocks.
        blocks.append(
            f"### Example {i} ({tick})\n\n"
            f"**Input - 10-k extracted records:**\n\n"
            f"```json\n{input_json}\n```\n\n"
            f"**Output — mapped statements:**\n\n"
            f"```json\n{output_json}\n\n```"
        )
    
    # Signal the end of the "few-shots" section.
    blocks.append(f"<end_of_examples>\n\n")
    
    # Join blocks together in a single markdown text.
    return "\n\n".join(blocks) + "\n"


def build_system_prompt() -> str:
    """Returns "prompt.md" with the few-shot section filled from "pairs".
    
    Fetches the naked "prompt.md" without the few-shots block, and fills it
    up with the block created by build_few_shots_block().

    Everything above the "<start_few_shots>" anchor is byte-identical
    to the file on disk.
    """
    text = SYSTEM_PROMPT.read_text(encoding=ENCODING)
    
    # Check if there is more than one anchor.
    if text.count(FEW_SHOT_ANCHOR) != 1:
        found = text.count(FEW_SHOT_ANCHOR)
        raise ValueError(
            f"{SYSTEM_PROMPT.name} must contain exactly one "
            f"{FEW_SHOT_ANCHOR!r}, found {found}"
        )
    
    # Extract everything before the anchor.
    head, _tail = text.split(FEW_SHOT_ANCHOR, maxsplit=1)
    
    # Return the head plus the few-shots block in a single string.
    return head + build_few_shots_block(load_mapped_examples())


def call_ollama(
    system_prompt: str,
    records: list[dict],
    url: str,
    model: str,
) -> dict:
    """Posts the chat payload and returns the server's JSON response.

    One statement per call, non-streaming, "format": "json" so the reply
    is structurally valid JSON, "temperature 0" because this is a lookup,
    not a generation.  "keep_alive" keeps the model warm when a batch of
    statements runs back to back.
    """
    payload = json.dumps({
        "model": model,
        "stream": False,  # avoid unnecessary streaming output
        "format": "json",
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(records, ensure_ascii=False)},
        ],
    }).encode(ENCODING)
    
    # The actual ollama call.
    request = urllib.request.Request(
        url.rstrip("/") + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS
        ) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", "replace")
        raise RuntimeError(
            f"Ollama HTTP {exc.code} from {url}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"cannot reach Ollama at {url}: {exc}") from exc
    
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        snippet = body[:300]
        raise RuntimeError(
            f"Ollama at {url} returned non-JSON: {snippet!r}"
        ) from exc
        
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Ollama at {url} returned a non-object payload")
    return parsed


def parse_response(body: dict) -> list[dict]:
    """Extracts and parses the JSON array from the Ollama reply.

    Enforces the completed-generation markers from the API contract ("done"
    true, "done_reason" "stop"), then parses "message.content" as JSON.
    
    A non-list payload or unparsable content is reported with an excerpt
    so the model's exact reply is visible in the log.
    """
    # Check that the model has actually finished reasoning and outputting.
    if body.get("done") is not True or body.get("done_reason") != "stop":
        raise RuntimeError(
            "incomplete generation: "
            f"done={body.get('done')!r} "
            f"done_reason={body.get('done_reason')!r}"
        )

    message = body.get("message")
    
    # Check that the response is a dict.
    if not isinstance(message, dict):
        raise RuntimeError(f"response has no 'message' object: {body!r}")
    
    content = message.get("content")
    
    # Check if the "content" is a list or try to extract from json.
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
            
    # ── Normalize dict into list ─────────────────────────────────────────────
    # The LLM likes to create a dict at the first level, even if prompted to
    # create a list of dicts.  In particular, it ofter creates a "mappings" key
    # and place the list of dicts from the output contract inside that key.
    #
    #   {
    #       "mappings": [
    #           {
    #           "id": "34088_is_01",
    #           "target": null,
    #           "transform": null,
    #           "reason": "section heading with no numeric value, no canonical counterpart"
    #           },
    #           {
    #           "id": "34088_is_02",
    #           "target": "income_statement.revenue",
    #           "transform": "renamed",
    #           },
    #           ...
    #       ]
    #   }
    #
    # This part of the method removes the extra fabrication and normalizes into a list of dicts.

    if isinstance(parsed, dict):
        
        # Case A (most common): Everything under "mappings".
        for key in ("mappings", "records", "decisions", "data", "results"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]  # we extract the inner list of dicts
                break
            
        else:
            
            # Case B: Keyed by ID.
            if all(isinstance(v, dict) for v in parsed.values()):
                normalized = []
                for k, v in parsed.items():
                    item = dict(v)
                    item.setdefault("id", k)
                    normalized.append(item)
                parsed = normalized
    
    if not isinstance(parsed, list):
        raise RuntimeError(
            f"model reply is a JSON {type(parsed).__name__}, "
            f"expected an array: {content[:300]!r}"
        )
        
    return parsed


def validate_decisions(records: list[dict], decisions: list[dict]) -> dict[str, dict]:
    """Enforces the 4-field contract and returns an id-keyed decision map.

    Mapped line: target & transform non-null, reason optional.
    Unmapped line: target & transform null, reason a non-empty string.
    
    All contract violations are collected and reported together with the
    offending ids, so a bad batch explains itself in one pass.
    """
    by_id: dict[str, dict] = {}
    problems: list[str] = []
    input_ids = {rec["id"] for rec in records}

    # ── Little helper ────────────────────────────────────────────────────────
    def bad(decision: dict, reason: str) -> None:
        if isinstance(decision, dict):
            dec_id = decision.get("id", "<missing id>")
        else:
            # Create a developer-friendly printable string of the decision of
            # max 40 charachters.
            dec_id = repr(decision)[:40]
        problems.append(f"{dec_id}: {reason}")

    # ── Validate decisions ───────────────────────────────────────────────────
    for position, decision in enumerate(decisions):
        
        if not isinstance(decision, dict):
            problems.append(f"[{position}]: not an object")
            continue
        
        if set(decision) != set(MAP_KEYS):
            bad(
                decision,
                
                # Inject the error we want into bad().
                f"fields must be exactly {MAP_KEYS}, got {sorted(decision)}"
            )
            continue
        
        dec_id = decision["id"]
        
        if dec_id not in input_ids:
            bad(decision, "id not present in the input records")
            continue
        
        if dec_id in by_id:
            bad(decision, "duplicate id")
            continue
        
        # If everything is right up to this point.
        target = decision["target"]
        transform = decision["transform"]
        reason = decision["reason"]
        
        # If the line item is unmapped (it can happen) then the reason is
        # mandatory.  The LLM must explain why the item was unmapped.
        if target is None or transform is None:
            if not (isinstance(reason, str) and reason.strip()):
                bad(decision, "unmapped lines need a non-empty reason")
        else:
            needs_target = not isinstance(target, str) or not target
            needs_transform = not isinstance(transform, str) or not transform
            if needs_target or needs_transform:
                bad(decision, "mapped lines need non-empty target and transform")
                
            # The decision can't be anything else than a string or None.
            elif not isinstance(reason, (str, type(None))):
                bad(decision, "reason must be a string or null")
                
        by_id[dec_id] = decision

    # Check if the model skipped some ids.
    missing_ids = sorted(input_ids - set(by_id))
    for missing_id in missing_ids:
        problems.append(f"{missing_id}: never answered by the model")
    if problems:
        raise RuntimeError(
            "decision contract violations:\n  "
            + "\n  ".join(problems)
        )
        
    return by_id


def merge_records(records: list[dict], by_id: dict[str, dict]) -> list[dict]:
    """Returns converted statements extended with the LLM mappings.

    The LLM only emits a 4-field list of dicts:
    
        [
            {
                "id": "104169_bs_02",
                "target": "cash_and_equivalents.cash",
                "transform": "direct",
                "reason": "Matches cash line item."
            },
            ...
        ]
    
    This method merges it back with the converted json file creating a 10-field
    list of dicts.  The "id" field is what links the 2 files and it stays.
    
        [
            {
                "id": "104169_bs_02",
                "stmt": "bs",
                "order": 2,
                "level": 1,
                "label": "Cash and cash equivalents",
                "tag": "defref_us-gaap_CashAndCashEquivalentsAtCarryingValue",
                "has_value": true,
                "target": "cash_and_equivalents.cash",
                "transform": "direct",
                "reason": "Matches cash line item."
            },
            ...
        ]
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


def sum_groups(merged: list[dict]) -> dict[str, list[str]]:
    """Identifies the items that the LLM mapped to the same target.

    This is the aggregation the LLM signals with a repeated "target":
    several raw lines that together form one canonical item.
    
    Unmapped rows (a null target) never form a group.  The groups drive
    the CLI report; the joined array itself
    already carries them one target at a time.
    """
    groups: dict[str, list[str]] = {}
    for rec in merged:
        target = rec["target"]
        
        # Crucial: the unmapped items never form a group and don't get summed
        # in the end.
        if target is not None:
            
            # Initialize an empty list only if the id does not exist yet
            # in groups.
            groups.setdefault(target, []).append(rec["id"])
    
    return {
        target: ids for target, ids in groups.items() if len(ids) >= 2
    }


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    """CLI entry point: maps one statement through Ollama, writes the output."""
    
    # ── CLI config ───────────────────────────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="Map a convert.py output via Ollama."
    )
    parser.add_argument("cik", help="Companie's EDGAR CIK, e.g. 104169")
    parser.add_argument(
        "stmt", choices=STATEMENTS, help="statement: (is | bs | cf)"
    )
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
        help="output path (default data/mapped/<cik>_<stmt>.json)"
    )
    args = parser.parse_args()

    # ── Pipeline ─────────────────────────────────────────────────────────────
    cik = normalize_cik(args.cik)
    stem = f"{cik}_{args.stmt}"
    src = CONVERTED_DIR / f"{stem}.json"
    out = Path(args.out) if args.out else MAPPED_DIR / f"{stem}.json"
    
    # If the converted file does not exist.
    if not src.is_file():
        print(
            f"error: {src} not found — run "
            f"uv run src/llm/convert.py {cik} {args.stmt} first",
            file=sys.stderr
        )
        return 1

    # Build system prompt with examples.
    records = load_records(src)
    examples = load_mapped_examples()
    system_prompt = build_system_prompt()

    # ── Ollama call ──────────────────────────────────────────────────────────
    started = time.monotonic()
    body = call_ollama(system_prompt, records, args.url, args.model)
    decisions = parse_response(body)
    by_id = validate_decisions(records, decisions)
    merged = merge_records(records, by_id)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False)
        + "\n", encoding=ENCODING
    )
    elapsed = time.monotonic() - started
    
    mapped = sum(1 for rec in merged if rec["target"] is not None)
    groups = sum_groups(merged)
    print(
        f"mapped {stem}: {len(merged)} records "
        f"({mapped} mapped, {len(merged) - mapped} unmapped) "
        f"in {elapsed:.1f}s -> {out}"
    )
    if groups:
        print(f"  sum groups: {len(groups)} target(s) served by 2+ rows")
        for target, ids in groups.items():
            print(f"    {target} <- " + ", ".join(ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
