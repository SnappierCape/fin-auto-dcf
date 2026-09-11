# Copyright 2026 Ulrico Luigi Nava
# SPDX-License-Identifier: Apache-2.0

"""Maps one converted statement into the canonical schema's item names.

Reads a record array from "convert.py" ("data/converted/"), pairs it with
the "prompt.md" system prompt, injects the canonical names and
3 hand-made example mappings into
the system prompt, and asks a local Ollama "/api/chat"
endpoint (plain HTTP, stdlib "urllib", no dependencies) for one thing
only: a mapping of each raw line to a canonical target.

The model uses the four-field contract defined in "src/llm/prompt.md" and
links every item using the "id" key from the input file:

    id        ─ unique identifier for each input item
    target    ─ "<bucket>.<item>" for mapped lines, else null
    transform ─ the transformation applied to the item
    reason    ─ a short reason; mandatory when the line is unmapped

The LLM never sees or prints a number.  When several raw items together
form one canonical item, it points them all at the same target.
It is a mapper, not a calculator.

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
CANONICAL_SCHEMA = ROOT / "data" / "canonical_schema.json"

CONVERTED_DIR = ROOT / "data" / "converted"
EXAMPLE_DIR = ROOT / "data" / "example_mappings"
GOLDEN_DIR = ROOT / "data" / "golden"
MAPPED_DIR = ROOT / "data" / "mapped"

# This is useful to find where to insert the canonical names block and the
# few-shots block.
FEW_SHOT_ANCHOR = "<few_shots_anchor>"
CANONICAL_NAMES_ANCHOR = "<canonical_names_anchor>"

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.8:27b-agent"

TIMEOUT_SECONDS = 590
KEEP_ALIVE = "30m"

ENCODING = "utf-8"

STATEMENTS = ("is", "bs", "cf")
MAP_KEYS = ("id", "target", "transform", "reason")

# The 7 attributes that "convert.py" specifies for each financial item.
INPUT_KEYS = ("id", "stmt", "order", "level", "label", "tag", "has_value")

# The exact structure of the Ollama response, not just a generic json.
# The following MAP_SCHEMA enforces this exact format in the Ollama output:
#
#   {
#      "mappings": [     
#         {  
#            "id": "34088_is_001",
#            "target": "is.revenue",
#            "transform": "renamed",
#            "reason": "total net sales is the pipeline's revenue line"
#         },
#         {
#            "id": "34088_is_002",
#            "target": null,
#            "transform": null,
#            "reason": "no counterpart in the is buckets"
#         },
#         ...
#      ]
#   }
#
MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "target": {"type": ["string", "null"]},
                    "transform": {"type": ["string", "null"]},
                    "reason": {"type": ["string", "null"]},
                },
                "required": ["id", "target", "transform", "reason"],
            },
        }
    },
    "required": ["mappings"],
}


# =============================================================================
# Input loading and system prompt assembly
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


def build_canonical_names_block() -> dict[list[str]]:
    """Builds the canonical names block to insert in the system prompt.
    
    Fetches "data/canonical_schema.json", extracts only the "statements"
    sub-dict, and returns only the possible canonical names for every bucket
    and every item in this format:
    
    {
        'is': ['revenue', 'cost_of_revenue', 'gross_profit', ...],
        'current_assets': ['cash_and_eq', 'st_investments', 'a_r', ...],
        'non_current_assets': ['p_p_e', 'lease', 'goodwill', ...],
        ...
    }
    """
    # Open the canonical schema .json file and load it into a python dict.
    with open(CANONICAL_SCHEMA, "r", encoding=ENCODING) as canon_file:
        canon = json.load(canon_file)

    # Ectract only the 'statements' sub-dict we are interested in.
    statements = canon['statements']

    canonical_names = {}

    # Cycle through each bucket and collect all the canonical names of
    # the items inside the bucket in a simple list.  This way, the LLM
    # will know what possible bucket and item names are available.
    for bucket_name, bucket_content in statements.items():
        items = list(bucket_content.keys())  # extract all the possible items for this bucket
        canonical_names[bucket_name] = items
    
    return str(canonical_names)
    

def build_system_prompt() -> str:
    """Injects the few-shots and the canonical names section in "prompt.md".
    
    Fetches the naked "prompt.md", and fills it
    with the blocks created by build_canonical_names_block() and
    build_few_shots_block().

    Everything above the "<canonical_names_anchor>" is byte-identical
    to the file on disk.
    """
    text = SYSTEM_PROMPT.read_text(encoding=ENCODING)
    
    # Check if there is more than one caonical names anchor.
    if text.count(CANONICAL_NAMES_ANCHOR) != 1:
        found = text.count(CANONICAL_NAMES_ANCHOR)
        raise ValueError(
            f"{SYSTEM_PROMPT.name} must contain exactly one "
            f"{CANONICAL_NAMES_ANCHOR!r}, found {found}"
        )
        
    # Same check for the few shots anchor.
    if text.count(FEW_SHOT_ANCHOR) != 1:
        found = text.count(FEW_SHOT_ANCHOR)
        raise ValueError(
            f"{SYSTEM_PROMPT.name} must contain exactly one "
            f"{FEW_SHOT_ANCHOR!r}, found {found}"
        )
    
    # ── Splitting strategy ───────────────────────────────────────────────────
    # Here the strategy is simple: we split the text in 3 parts:
    #   1. Head:     The part that goes from the beginning to the canonical
    #                names anchor
    #   2. Mid:      The part that is in between the canonical names anchor and
    #                the few-shots anchor
    #   3. Tail (_): Everythig below the few-shots anchor
    
    # Split with respect to canonical names anchor.
    head, tail = text.split(CANONICAL_NAMES_ANCHOR, maxsplit=1)    

    # Split the tail with respect to few-shots anchor.
    mid, _ = tail.split(FEW_SHOT_ANCHOR, maxsplit=1)
    
    # Return the head plus the mid with the rights blocks plugged
    # in between.
    return (
        head
        + build_canonical_names_block()
        + mid
        + build_few_shots_block(load_mapped_examples())
    )


def call_ollama(
    system_prompt: str,
    converted_input: list[dict],
    url: str,
    model: str,
) -> dict:
    """Calls Ollama, sends the message, and collects the JSON response.

    One statement per call, non-streaming, "format": MAP_SCHEMA so the reply
    has the expected structure, "temperature 0" because this is a lookup,
    not a generation.  "keep_alive" keeps the model warm when a batch of
    statements runs back to back.
    """
    payload = json.dumps({
        "model": model,
        "stream": False,  # avoid unnecessary reasoning output
        "format": MAP_SCHEMA,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(converted_input, ensure_ascii=False)
            },
        ],
    }).encode(ENCODING)
    
    # Http request metadata.
    request = urllib.request.Request(
        url.rstrip("/") + "/api/chat",  # build the "http://127.0.0.1:11434/api/chat" url
        data=payload,
        headers={"Content-Type": "application/json"},  # inform Ollama that the input is json
        method="POST",  # explixitly set HTTP for intake
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
    
    The output is standardized as a flat list of dicts, the extra "mappings" key
    is removed.
    """
    # Check that the model has actually finished reasoning and outputting.
    if body.get("done") is not True or body.get("done_reason") != "stop":
        raise RuntimeError(
            "incomplete generation: "
            f"done={body.get('done')!r} "
            f"done_reason={body.get('done_reason')!r}"
        )

    # ── Ollama output contract ───────────────────────────────────────────────
    # Ollama outputs a json-like payload with this structure:
    #
    #   {
    #   "model": "qwen3.8:27b-agent",
    #   "created_at": "2026-09-02T14:29:00Z",
    #   "message": {
    #       "role": "assistant",
    #       "content": "{\n  \"mappings\": [\n    {\n      \"id\": \"34088_is_01\",\n      \"target\": null,\n      \"transform\": null,\n      \"reason\": \"section heading\"\n    }\n  ]\n}"
    #   },
    #   "done_reason": "stop",
    #   "done": true
    #   }
    #
    # We are interested in the "message.content" value.
    
    message = body.get("message")
    
    # Check that the response is a dict.
    if not isinstance(message, dict):
        raise RuntimeError(f"message must be 'dict', received: {body!r}")
    
    content = message.get("content")
    
    # Check if the "content" is a list or try to extract from json.
    if isinstance(content, list):
        raise RuntimeError(f"content is 'list', should be 'dict'")
    
    # Check if the JSOn is ready to be parsed.
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
    # For this reason, instead of fighting it, we decided to force the
    # "mappings" key.
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
        
        # Case A: Everything under "mappings".
        for key in ("mappings", "records", "decisions", "data", "results"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]  # we extract the inner list of dicts
                break
            
        else:
            
            # Case B: Keyed by ID -- this should not happen since MAP_SCHEMA
            # is enforced.
            if all(isinstance(v, dict) for v in parsed.values()):
                normalized = []
                for k, v in parsed.items():
                    item = dict(v)
                    item.setdefault("id", k)
                    normalized.append(item)
                parsed = normalized
    
    # Check if the inner structure is a list.
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
            # max 80 charachters.
            dec_id = repr(decision)[:80]
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
        
        # Check if the LLM invented some ids.
        if dec_id not in input_ids:
            bad(decision, "id not present in the input records")
            continue
        
        # Check if the LLM duplicated some ids.
        if dec_id in by_id:
            bad(decision, "duplicate id")
            continue
        
        # If everything is right up to this point.
        target = decision["target"]
        transform = decision["transform"]
        reason = decision["reason"]
        
        # ── Reason, Target and Transform check ───────────────────────────────
        # If the line item is unmapped (it can happen) then the reason is
        # mandatory.  The LLM must explain why the item was unmapped.
        #
        # The 'target' and the 'transform' fields are either both present
        # or both null.  There is no case where one can be present and the
        # other is null.
        
        # Case A: Valid unmapped item
        if target is None and transform is None:
            if not (isinstance(reason, str) and reason.strip()):
                bad(decision, "unmapped lines need a non-empty reason")
                
        # Case B: Valid mapped item
        elif isinstance(target, str) and target and isinstance(transform, str) and transform:
            if not isinstance(reason, (str, type(None))):  # reason optional
                bad(decision, "reason must be a string or null")
        
        # Case C: Invalid options     
        else:
            if target is not None and transform is None:
                bad(decision, "mapped target provided, but transform is null")
            elif target is None and transform is not None:
                bad(decision, "transform provided, but target is null")
            else:
                bad(decision, "target and transform must be non-empty strings")
                
        by_id[dec_id] = decision

    # Double check if some ids were skipped.
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

    The LLM gives us 4 fields:
    
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
    
    Unmapped rows (a null target) never form a group.
    """
    groups: dict[str, list[str]] = {}
    for rec in merged:
        target = rec["target"]
        
        # Crucial: the unmapped items never form a group and don't get summed
        # in the end.
        if target is not None:
            
            # Append only if the id does not exist yet in groups.
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
