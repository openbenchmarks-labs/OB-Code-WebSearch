# harness

Runs the benchmark and calls the search APIs. No grading code lives here.

- `config.yaml` — Model, reasoning effort, turn, search, fetch and result limits, and model prices.
- `obkit.py` — The obkit hooks (`answer`, `contract`, `probe`) and the offline selftest.
- `agent.py` — Runs the agent loop and collects its final API call.
- `search.py` — API adapters for every search tool.
- `requirements.in` — The packages `requirements.txt` is compiled from.
