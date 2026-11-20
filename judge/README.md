# judge

Grades runs against ground truth. No search or agent code lives here.

- `config.yaml` — Grading rules and how scores are rolled up into the leaderboard.
- `grader.py` — Decides pass or fail for every answer.
- `checks.py` — Code extraction, citation checks, and sandbox checks used on the public questions.
- `retrieval.py` — Checks whether the search tool actually returned the needed API docs, separate from whether the model got the answer right.
- `verifier_prompt.txt` — Prompt for the second-opinion LLM check that flags borderline cases for human review.
