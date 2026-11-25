# data

This folder holds the 30-question public split used for offline verification (`selftest`) and local reproduction.

## Public vs. held-out evaluation sets

- **Public split (`questions.jsonl`):** 30 enterprise API tickets covering Workday, SAP SuccessFactors, UKG, Veeva, Genesys Cloud, and ServiceNow. Also published on Hugging Face at [`openbenchmarks/OB-Code-Websearch`](https://huggingface.co/datasets/openbenchmarks/OB-Code-Websearch).
- **Held-out split (private):** The evaluation set used for the published leaderboard stays private so search tools and models cannot train on it.

## Files and schema

- `questions.jsonl` — One JSON object per line.

| Field | Type | Description |
|---|---|---|
| `task_id` | string | Unique identifier for the ticket. |
| `set` | string | Dataset split (`public`). |
| `vendor` | string | Target enterprise product (for example, `genesys`, `successfactors`, `workday`). |
| `ticket` | string | Engineering prompt describing the required API call. |
| `starter_code` | string | The existing code; the question shows the request it sends today, not the code. |
| `reference` / ground truth fields | object | Accepted API call details used by `judge/`. |
