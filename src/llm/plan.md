# Plan — `reclassify.py`

## Goal
For one converted statement file, bundle the records with the perfected
`prompt.md` system prompt, send the call to Ollama, and land a
reclassified JSON file in `data/reclassified/`. One statement per call.

## Inputs (all read, nothing but the output written)
- `data/converted/<cik>_<stmt>.json` — array of 8-field records from
  `convert.py`: `id, stmt, order, level, label, tag, has_value, note`.
- `src/llm/prompt.md` — system prompt (perfected by Snapp). Contains one
  placeholder `{{CANONICAL_SCHEMA}}` (line 58) and four empty few-shot
  boxes (lines 85–127) Snapp fills by hand.
- `data/schema.json` — source of truth for the canonical line items and
  the `transform` vocabulary.
- Ollama over HTTP at `OLLAMA_URL` (default `http://127.0.0.1:11434`),
  model from `OLLAMA_MODEL` (default `qwen3.8:27b-agent`).

## Output
- `data/reclassified/<cik>_<stmt>.json` — a fresh array where **every
  input record is preserved in input order** and annotated with the
  prompt's 4-field decision contract:
  - mapped:   `target` = `<bucket>.<line_item>`, `transform` = a canon-
             ical value, `reason` = short (optional).
  - unmapped: `target` = null, `transform` = null, `reason` = short,
             mandatory.
  The decision fields are `target`, `transform`, `reason` — added onto
  each existing record. The module never edits the 8 original fields.

## Verified Ollama /api/chat contract (live probe, v0.32.13)
Request body (JSON):
```
{
  "model": "...",
  "stream": false,
  "format": "json",
  "keep_alive": "30m",
  "options": { "temperature": 0 },
  "messages": [ {"role": "system", "content": "<prompt>"},
                {"role": "user",   "content": "<records json>"} ]
}
```
Non-streaming response (top level): `created_at, done, done_reason,
eval_count, eval_duration, load_duration, message, model,
prompt_eval_count, prompt_eval_duration, total_duration`.
- `message.role` = "assistant"
- `message.content` = a **string** holding the JSON array to parse
- `message.thinking` = present for this agent/thinking model; ignored
- `done` = true, `done_reason` = "stop"
- `format: "json"` forces `content` to be valid JSON (confirmed).

## Pipeline (main flow)
1. `find_converted(cik, stmt)` — `data/converted/<cik>_<stmt>.json`;
   not found -> raise with the exact path + a hint to run convert.py.
2. `load_records(path)` — parse; must be a non-empty JSON array; every
   record must carry `id` (string) and the 8 documented keys; ids must
   be unique. Fail loudly otherwise.
3. `load_system_prompt()` — read `prompt.md`; require `{{CANONICAL_
   SCHEMA}}` present exactly once; build the canonical-schema block from
   `schema.json` (statements buckets+line items, computed_values, and
   the canonical `transform` vocabulary); substitute. Keep the four
   few-shot boxes byte-for-byte (Snapp's).
4. `call_ollama(system, records, model, url)` — POST the chat payload;
   return the parsed response dict. Read/HTTP errors -> raise with the
   status + a body excerpt.
5. `parse_response(body)` — assert `done` and `done_reason=="stop"`;
   take `message.content`; `json.loads` it; must be a list. Otherwise
   raise showing the offending `content`.
6. `decide(records, decisions)` — join by `id`:
   - every input id must appear exactly once in `decisions`;
   - ids in `decisions` not in input -> error (model invented rows);
   - each decision object must be a dict with exactly the keys
     `{id, target, transform, reason}`;
   - mapped: `target` & `transform` non-null strings, `reason` string/
     null;
   - unmapped: `target` & `transform` null, `reason` non-empty string.
     Violations are collected (with the offending ids) and reported as
     one `RuntimeError` — no silent drops, no silent accepts.
7. `annotate(records, decisions)` — return a new list in **input order**,
   each original record unchanged plus `target`, `transform`, `reason`.
8. Write `data/reclassified/<cik>_<stmt>.json` (create dir if missing),
   compact JSON (2-space, like the rest of the repo), trailing newline.

## CLI
```
uv run src/llm/reclassify.py <cik> <is|bs|cf>
    [--url URL] [--model NAME] [--out PATH]
```
Defaults: `url=127.0.0.1:11434`, `model=qwen3.8:27b-agent`,
`out=data/reclassified/<cik>_<stmt>.json`.

## Design rules
- stdlib only (`urllib`), matching `convert.py` — no new dependency.
- No git commit/stage; no linter.
- One call per statement. Numbers never leave the record set; the model
  only classifies.
- Deterministic: id-keyed join, input-order output, no re-ranking.
- PEP8 + PEP257; 79-col limit; `# Copyright` + SPDX headers.

## Self-audit (what I check before considering it done)
- [ ] `python -m py_compile` clean;
- [ ] no import of symbols that don't exist in `convert.py`;
- [ ] 79-col limit holds across the file;
- [ ] the 8 input fields + `id` uniqueness are validated pre-flight;
- [ ] the 4-field decision contract is enforced (mapped vs unmapped);
- [ ] output is input-ordered and the 8 original fields are untouched;
- [ ] a real end-to-end run against `data/converted/` succeeds and lands
      a well-formed file in `data/reclassified/`;
- [ ] malformed / partial / invented-id responses are all rejected.
