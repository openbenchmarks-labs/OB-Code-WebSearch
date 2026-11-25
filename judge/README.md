# judge

Grades answers against ground truth, and builds the question the agent is asked.

- `checks.py` — The question text, the system prompts and answer format the agent gets, the recorder that runs a starter or reference fix against a local stand-in to see the request it sends, and the answer grader (`grade`).
- `obkit.py` — The obkit hooks: `grade`, `verify` (the LLM verifier, which never changes a score) and `key_check` (whether an accepted request appears on its docs pages).
- `config.yaml` — The verifier's two readers (Claude Haiku 5.5 reads every answer; Claude Sonnet 5.5 reads again where Haiku disagrees or is unsure), their prices, and the cost of one verified answer.
- `cases.jsonl` — Planted answers with the verdict the grader must give.
- `verifier_prompt.txt` — Prompt for the LLM verifier.

## What the answer grader checks

The answer grader in this repo (`checks.grade`, called from `judge/obkit.py`) passes an answer when its call matches one of the question's right calls. For REST, that means the same method and path, and the call must expand every name the right call expands: each top-level name in an `$expand` in its address, and the name in its `require_query` field. A right call with `match: prefix` names a service root, and any path under it matches. For SOAP, it means the same operation sent to a Workday service that publishes it; a key lists one right call per such service.

A REST right call may list `fields`: the query parameters and body fields its call must name. A field is listed when the ticket asks for that content and the reference fix sends it under the name the docs give it: in the public question `hard_sn_queue_uxa_export`, "for January 2025" lists `from_date` and `to_date`, and "limited to the modern workspace experience" lists `channel`. Fields the ticket calls optional, and paging the ticket does not ask for, are not listed. Only the right call that is the reference fix's own request lists them; another right call may take other fields. Names compare without case or punctuation, and each dotted, indexed, spaced or quoted part of a field the agent names counts, so `items[0].quantity` names `quantity`. Values are not compared.

The grader does not read the body of a SOAP call, the key's `required_query` or `with` fields, or any header.
