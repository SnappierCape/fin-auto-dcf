from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EXAMPLE_DIR = Path("/code/fin-auto-dcf/data/example_mappings")
ENCODING = "utf-8"

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

def load_mapped_examples() -> list[dict[str, list[dict], list[dict]]]:
    """Builds the 3 few-shot (company, input_records, output_map) pairs.

    Inputs and outputs come from "data/example_mappings/".
    Inputs are the "..._converted.json" files, outputs are the 
    "..._mapped.json" files.  A pair forms when both the
    "converted" and "mapped" files are found.
    """
    
    # Initialize empty dict to collect example files.    
    examples: dict[str, dict] = {}

    if EXAMPLE_DIR.is_dir():
        
        # Cycle each file in the dir.
        for path in sorted(EXAMPLE_DIR.glob("*.json")):
            tick, _, file_type = path.stem.split("_", maxsplit=2)  # split by "_"
            
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
    "### Example N (COMPANY ─ STATEMENT)" block per pair, each with an
    "Input" fence (the converted records) and an "Output" fence (the
    4-field map).
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
    
    # Join blocks together in a single markdown text.
    return "\n\n".join(blocks) + "\n"

def load_system_prompt() -> str:
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
    
    head, _tail = text.split(FEW_SHOT_ANCHOR, 1)
    return head + build_few_shots_block(load_mapped_examples())

print(load_system_prompt())