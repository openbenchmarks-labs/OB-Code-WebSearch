# AGENTS.md

Commands, from the repo root.

```sh
uv venv && uv pip install -r requirements.txt
.venv/bin/python -B -m harness.run selftest --tasks data/questions.jsonl   # offline, no API keys
```

The OpenBenchmarks team runs this repo through obkit, at the version `benchmark.yaml` pins:

```sh
obkit check                                    # the selftest above, plus the repo layout
obkit lock --reason "<why>"                    # a new version after a score input changed
obkit run --set public --tools <setups> --dry  # canned replies, no spend
obkit judge runs/<run id>
```
