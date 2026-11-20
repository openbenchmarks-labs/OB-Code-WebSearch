# harness

Runs the benchmark and calls the search APIs. No grading code lives here.

- `config.yaml` — Model name, turn limit, search limit, and repeat count.
- `run.py` — Command-line runner (`selftest` and live benchmark runs).
- `agent.py` — Runs the coding agent loop and collects its final API call.
- `search.py` — API adapters for every search tool.
- `logger.py` — Saves run logs under `runs/` with API keys stripped out.
