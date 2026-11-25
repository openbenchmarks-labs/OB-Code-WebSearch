# data

This folder holds the 30-question public split used for offline verification (`selftest`) and local reproduction.

## Public vs. held-out evaluation sets

- **Public split (`questions.jsonl`):** 30 enterprise API tickets: ServiceNow 16, SAP SuccessFactors 5, Workday 4, UKG 2, Veeva Vault 2 and Genesys Cloud 1. The Hugging Face dataset [`openbenchmarks/OB-Code-Websearch`](https://huggingface.co/datasets/openbenchmarks/OB-Code-Websearch) still holds the version 1 file; the version 2 questions are in review there.
- **Held-out splits (validation and private):** They stay out of this repository so search tools and models cannot train on them; `benchmark.yaml` names each by its sha256.

## Files and schema

- `questions.jsonl` — One JSON object per line. The code reads the fields below. obkit also reads `kind` and `vendor` (the `split_axes` in `benchmark.yaml`), and on the held-out sets `crawler_open` (true when robots.txt opens the answer page to the nine crawler names; false rows run once, apart) and `question_group` (`first selection` or `extension`, the weighted groups). The other fields of a row (`n_changes`, `implemented`, `outfile`, `docs`, `required`, `query_forbidden`, `why_opaque`, `stripped`, `leak_excerpt_in_task_files`) are records from building the question, and nothing reads them.

| Field | Type | Description |
|---|---|---|
| `task_id` | string | Unique identifier for the ticket. |
| `set` | string | Dataset split (`public`). |
| `vendor` | string | Target enterprise product (for example, `genesys`, `successfactors`, `workday`). |
| `ticket` | string | The product ticket; the LLM verifier reads it. |
| `prompt` | string | The full task prompt; the question the agent gets takes its ticket sentences from it. |
| `starter_code` | string | The existing code; the question shows the request it sends today, not the code. |
| `gold_code` | string | The reference fix: the code with the request the ticket needs. |
| `retrieval_key` | object | Ground truth. `accepted` lists the right calls (method, address, Workday SOAP operation, and the `fields` the key checks), which `judge/checks.py` grades against; `docs` lists the pages that show them, which `key_check` reads. |
| `source_url` | string | The main documentation page; `key_check` reads it when the key lists no `docs`. |
