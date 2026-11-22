# judge

Grades runs against ground truth. No search or agent code lives here.

- `config.yaml` — Grading rules and how scores are rolled up into the leaderboard.
- `grader.py` — Decides pass or fail for every answer.
- `checks.py` — Code extraction, citation checks, and sandbox checks used on the public questions.
- `retrieval.py` — Checks whether the search tool actually returned the needed API docs, separate from whether the model got the answer right.
- `verifier_prompt.txt` — Prompt for the second-opinion LLM check that flags borderline cases for human review.

## What the answer grader checks

The answer grader in this repo (`checks.grade`, called from `judge/obkit.py`) passes an answer when its call matches one of the question's right calls. For REST, that means the same method and path, and the call must expand every name the right call expands: each top-level name in an `$expand` in its address, and the name in its `require_query` field. For SOAP, it means the same operation sent to the Workday service that owns it. Apart from those expands, the grader does not read query parameters or body fields, even when the ticket asks for them, so a call to the right endpoint with a missing or misnamed field still passes. `required_query` is a different field: query parameter values that `retrieval.py` looks for in the returned docs. The answer grader ignores it.
