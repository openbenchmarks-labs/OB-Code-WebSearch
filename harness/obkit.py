"""obkit entry points: answer one question with one setup, what the model sees, and what each setup sends."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import yaml
from judge import checks as facts

from . import agent
from . import search as vendors
from .run import CodingTask, load_environment, load_tasks, make_llm_client

HERE = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((HERE / "config.yaml").read_text())
CANNED_PASS_PERCENT = {"no_search": 30, "exa_fast": 75, "parallel_fast": 60}
CANNED_FACTS = {
    "llm_cost_usd": 0.0,
    "vendor_cost_usd": 0.0,
    "llm_ms": 0,
    "search_ms": 0,
    "fetch_ms": 0,
    "input_tokens": 0,
    "output_tokens": 0,
}
PROBE_QUERY = "obkit probe: Workday Get_Workers SOAP operation"
PROBE_URL = "https://example.com/obkit-probe"
PROBE_HITS = [
    {"url": f"https://example.com/{i}", "title": f"title {i}", "snippet": "snippet " * 400} for i in range(12)
]
PROBE_PAGE = {"requested_url": PROBE_URL, "final_url": PROBE_URL, "title": "probe", "text": "page " * 9000}


class _Sent(Exception):
    """Raised where a request would leave this machine."""


def _task(row: dict) -> CodingTask:
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()
        return load_tasks(Path(f.name))[0]


def _arms() -> dict[str, tuple[str, str]]:
    """Each arm obkit runs, as (search backend, mode): a setup with a list of modes is one arm per mode."""
    out = {"no_search": ("no_search", "no_search")}
    for path in sorted((HERE.parent / "tools").glob("*.yaml")):
        for name, cfg in (yaml.safe_load(path.read_text()).get("setups") or {}).items():
            modes = cfg["mode"] if isinstance(cfg["mode"], list) else [cfg["mode"]]
            out |= {name if len(modes) == 1 else f"{name}+{mode}": (name, mode) for mode in modes}
    return out


ARMS = _arms()


def _mode(arm: str) -> tuple[bool, bool, str]:
    mode = ARMS[arm][1]
    search, fetch = mode != "no_search", mode == "search_and_fetch"
    return search, fetch, facts.SYSTEM if fetch else facts.SYSTEM_SEARCH_ONLY if search else facts.SYSTEM_NO_SEARCH


def _run(text: str, setup: str | None, search: bool, fetch: bool, system: str, client, **fns) -> agent.AgentRun:
    limits = CONFIG["limits"]
    return agent.run_agent(
        text,
        client=client,
        backend_name=setup or "no_search",
        model=CONFIG["model"],
        reasoning_effort=CONFIG["effort"],
        max_turns=limits["max_turns"],
        max_searches=limits["max_searches"],
        max_fetches=limits["max_fetches"] if fetch else 0,
        max_results=limits["results_per_search"],
        system=system,
        allow_search=search,
        allow_fetch=fetch,
        text_format=facts.ANSWER_SCHEMA,
        **fns,
    )


class _FirstTurn:
    """A model client that records the first request and sends nothing."""

    def __init__(self):
        self.request = None
        self.responses = self

    def create(self, **request):
        self.request = request
        raise _Sent


def _shaping() -> list:
    """The tool outputs the model would read for fixed search and fetch results, and the budget messages."""
    run = agent.AgentRun(question="", answer="", model="", backend="probe", used_search=False)
    conversation: list = []
    search_call = {"call_id": "s", "name": "web_search", "arguments": json.dumps({"query": PROBE_QUERY})}
    fetch_call = {"call_id": "f", "name": "web_fetch", "arguments": json.dumps({"url": PROBE_URL, "objective": ""})}
    for left in (1, 0):
        agent._run_search(
            search_call,
            run=run,
            conversation=conversation,
            backend=None,
            provider="probe",
            max_searches=left,
            max_results=CONFIG["limits"]["results_per_search"],
            search_fn=lambda q: [dict(h) for h in PROBE_HITS],
            turn=0,
            leak_needles=[],
        )
    for left in (1, 0):
        agent._run_fetch(
            fetch_call,
            run=run,
            conversation=conversation,
            backend=None,
            provider="probe",
            max_fetches=left,
            fetch_fn=lambda u, o: dict(PROBE_PAGE),
            turn=0,
        )
    return conversation


def contract(questions: list[dict]) -> dict:
    """What the model sees: the first request of every question in every mode, and the tool-output shaping."""

    def first_turns(row: dict) -> dict:
        text = facts.question(_task(row))
        out = {}
        for mode in ("search_fetch", "search_only", "no_search"):
            client = _FirstTurn()
            search, fetch = mode != "no_search", mode == "search_fetch"
            system = facts.SYSTEM if fetch else facts.SYSTEM_SEARCH_ONLY if search else facts.SYSTEM_NO_SEARCH
            _run(text, None, search, fetch, system, client, search_fn=lambda q: [], fetch_fn=lambda u, o: {})
            out[mode] = client.request
        return {row["task_id"]: out}

    with ThreadPoolExecutor(max_workers=8) as pool:
        turns = {k: v for part in pool.map(first_turns, questions) for k, v in part.items()}
    return {"first_turns": turns, "shaping": _shaping()}


def probe(setup: str) -> dict:
    """The request this arm sends for a fixed query (and a fixed fetch), recorded offline, plus its tool schema."""
    if ARMS[setup][1] == "no_search":
        return {"setup": setup}
    sent: list = []

    def record(method, url, **kwargs):
        sent.append({"method": method, "url": url, "headers": sorted(kwargs.pop("headers", None) or {}), **kwargs})
        raise _Sent

    names = set(re.findall(r'os\.environ\.get\("([A-Z0-9_]+)"', (HERE / "search.py").read_text()))
    backend = vendors.get_backend(ARMS[setup][0])
    with (
        mock.patch.object(vendors, "vendor_request", record),
        mock.patch.dict(os.environ, dict.fromkeys(names, "obkit-probe")),
    ):
        for call in (
            lambda: backend.search(PROBE_QUERY, max_results=CONFIG["limits"]["results_per_search"]),
            lambda: backend.fetch(PROBE_URL, objective="") if _mode(setup)[1] else None,
        ):
            try:
                call()
            except _Sent:
                pass
            except Exception as exc:  # noqa: BLE001
                sent.append({"raises": f"{type(exc).__name__}: {exc}"})
    return {"tool": agent._search_tool(backend), "sent": json.loads(json.dumps(sent, default=str))}


def _canned(question: dict, setup: str) -> dict:
    """Selftest replies: the accepted call for some questions, a near miss for the rest. No network."""
    roll = int(hashlib.sha256(f"{setup} {question['task_id']}".encode()).hexdigest()[:8], 16) % 100
    if roll >= 97:
        return {
            "answer": "",
            "status": "infra_error",
            "error": "canned model timeout",
            "cost_usd": 0.0,
            "facts": CANNED_FACTS,
        }
    right = question["retrieval_key"]["accepted"][0]
    call = {"method": right["method"], "path": right["address"], "operation": right.get("soap") or "", "fields": []}
    if roll >= CANNED_PASS_PERCENT.get(setup, 70):
        call["path"] = right["address"].split("?")[0].rstrip("/") + "/settings"
        call["operation"] = "Get_Settings" if call["operation"] else ""
    status = "vendor_empty" if setup != "no_search" and roll % 23 == 0 else "ok"
    return {"answer": json.dumps(call), "status": status, "error": None, "cost_usd": 0.0, "facts": CANNED_FACTS}


def _price(setup: str) -> dict:
    for path in (HERE.parent / "tools").glob("*.yaml"):
        setups = yaml.safe_load(path.read_text()).get("setups") or {}
        if setup in setups:
            return setups[setup].get("pricing") or {}
    return {}


def answer(question: dict, setup: str, *, dry: bool, tracer=None) -> dict:
    if dry:
        return _canned(question, setup)
    load_environment()
    task = _task(question)
    search, fetch, system = _mode(setup)
    run = _run(
        facts.question(task),
        ARMS[setup][0],
        search,
        fetch,
        system,
        make_llm_client(timeout=180, max_retries=1),
        leak_needles=task.leak_needles(),
    )
    if tracer is not None:
        for turn in run.llm_turns:
            usage = {
                "prompt_tokens": turn.prompt_tokens,
                "completion_tokens": turn.completion_tokens,
                "latency_ms": turn.latency_ms,
            }
            tracer.span("model", output={"tool_calls": turn.tool_names}, metadata=usage)
        for call in run.searches:
            tracer.span(
                "web_search",
                input={"query": call.query},
                output={"urls": call.hit_urls},
                metadata={"error": call.error},
            )
        for call in run.fetches:
            tracer.span("web_fetch", input={"url": call.url}, metadata={"error": call.error})
    sent = [call for call in run.searches if call.request_attempted]
    price = _price(ARMS[setup][0])
    billed = sum(1 for call in sent if not call.error and (call.n_hits or price.get("bills_empty")))
    per_mtok = CONFIG["usd_per_mtok"]
    vendor_cost = billed * float(price.get("list_usd") or 0)
    llm_cost = (run.llm_prompt_tokens * per_mtok["input"] + run.llm_completion_tokens * per_mtok["output"]) / 1e6
    metrics = run.metrics()
    cost_facts = {
        "llm_cost_usd": round(llm_cost, 6),
        "vendor_cost_usd": round(vendor_cost, 6),
        "llm_ms": metrics["llm_ms"],
        "search_ms": metrics["search_ms"],
        "fetch_ms": metrics["fetch_ms"],
        "input_tokens": run.llm_prompt_tokens,
        "output_tokens": run.llm_completion_tokens,
    }
    # run.error is set only by a model failure or a search failure the harness leaves unscored: ours, not the vendor's.
    if run.error:
        status = "infra_error"
    elif sent and all(call.error for call in sent):
        status = "vendor_error"
    elif sent and all(not call.error and call.n_hits == 0 for call in sent):
        status = "vendor_empty"
    else:
        status = "ok"
    return {
        "answer": run.answer,
        "status": status,
        "error": run.error,
        "cost_usd": round(llm_cost + vendor_cost, 6),
        "facts": cost_facts,
    }
