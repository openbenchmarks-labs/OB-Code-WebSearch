# judge

Grades answers against ground truth, and builds the question the agent is asked.

- `checks.py` — The question text, the system prompts and answer format the agent gets, the recorder that runs a starter or reference fix against a local stand-in to see the request it sends, and the answer grader (`grade_task`).
- `obkit.py` — The obkit hooks: `grade`, `verify` (the LLM verifier, which never changes a score) and `key_check` (whether an accepted request appears on its docs pages).
- `config.yaml` — The verifier's two readers (Claude Haiku 5.5 reads every answer; Claude Sonnet 5.5 reads again where Haiku disagrees or is unsure), their prices, and the cost of one verified answer.
- `cases.jsonl` — Planted answers with the verdict the grader must give.
- `verifier_prompt.txt` — Prompt for the LLM verifier.

## What the answer grader checks

The answer grader in this repo (`checks.grade_task`, called from `judge/obkit.py`) passes an answer when its call matches one of the question's right calls. For REST, that means the same method and path, and the call must expand every name the right call expands: each top-level name in an `$expand` in its address, and the name in its `require_query` field. For SOAP, it means the same operation sent to the Workday service that owns it.

A REST call must also name every field the ticket asks for. `checks.asked_by_answer` finds them: the query parameters and top-level body fields the reference fix sends that the starter does not, whose name the ticket's words or the question's required strings spell out (`checks.asked_fields`), leaving out paging parameters, fields the ticket calls optional, and required strings that are part of the address itself (an endpoint named `recordLearningEvents` does not make `recordLearningEvents` a field). Only the right call the reference fix itself makes owes them, with or without its version segment; another right call may take other fields. Names compare without case or punctuation, and each dotted, indexed, spaced or quoted part of a field the agent names counts, so `deviceToken`, `device token` and `root.device_token` all name `deviceToken`. A call to the right endpoint without one of them fails. The grader checks that a field is named, not its value, and it does not read SOAP request bodies. The reference fix is recorded only when an answer is already a right call, and a reference fix that sends no request stops grading with an error instead of skipping the check.

The grader does not read the key's `required_query` field.
