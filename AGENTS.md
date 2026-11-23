# AGENTS.md

Commands, from the repo root.

```sh
uv venv && uv pip install -r requirements.txt
.venv/bin/python -B -m harness.run selftest --tasks data/questions.jsonl   # offline, no API keys
```

The OpenBenchmarks team runs this repo through obkit, at the version `benchmark.yaml` pins:

```sh
obkit check                                         # the selftest above, plus the repo layout
obkit go --set public --tools <setups> --dry       # canned replies, no spend; judges and writes the board
obkit add-set validation|private FILE              # store a held-out set by its sha256 and name it here
```

`requirements.txt` pins every dependency, compiled from `harness/requirements.in` with `uv pip compile --universal --no-header harness/requirements.in -o requirements.txt`. obkit refuses a venv that differs from it, since package versions are part of each setup's version.
