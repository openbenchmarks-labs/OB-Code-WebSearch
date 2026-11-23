# judge

Grades answers against ground truth, and builds the question the agent is asked.

- `checks.py` — The question text, the system prompts and answer format the agent gets, the recorder that runs a starter or reference fix against a local stand-in to see the request it sends, and the answer grader (`grade`).
- `obkit.py` — The obkit hooks: `grade`, `verify` (the LLM verifier, which never changes a score) and `key_check` (is each accepted request on its docs page).
- `config.yaml` — The verifier's model and prices.
- `cases.jsonl` — Planted answers with the verdict the grader must give.
- `verifier_prompt.txt` — Prompt for the LLM verifier.

## What the answer grader checks

The answer grader in this repo (`checks.grade`, called from `judge/obkit.py`) passes an answer when its call matches one of the question's right calls. For REST, that means the same method and path, and the call must expand every name the right call expands: each top-level name in an `$expand` in its address, and the name in its `require_query` field. For SOAP, it means the same operation sent to the Workday service that owns it. Apart from those expands, the grader does not read query parameters or body fields, even when the ticket asks for them, so a call to the right endpoint with a missing or misnamed field still passes. The grader does not read the key's `required_query` field.
