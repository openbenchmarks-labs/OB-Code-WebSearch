"""obkit entry points: answer one question with one setup, what the model sees, what each setup sends, and the offline
selftest (`python -B -m harness.obkit data/questions.jsonl`)."""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import yaml
from judge import checks as facts

from . import agent
from . import search as vendors

HERE = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((HERE / "config.yaml").read_text())
AZURE_KEY_ENV = "AZURE_OPENAI_NEXTGEN_DEPLOYMENT_KEY"
AZURE_URL_ENV = "AZURE_OPENAI_NEXTGEN_DEPLOYMENT_URL"
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


def _arms() -> dict[str, tuple[str, str]]:
    """Each arm obkit runs, as (search backend, mode): a setup with a list of modes is one arm per mode."""
    out = {"no_search": ("no_search", "no_search")}
    for path in sorted((HERE.parent / "tools").glob("*.yaml")):
        for name, cfg in (yaml.safe_load(path.read_text()).get("setups") or {}).items():
            modes = cfg["mode"] if isinstance(cfg["mode"], list) else [cfg["mode"]]
            if "search_and_fetch" in modes and "fetch_usd" not in (cfg.get("pricing") or {}):
                raise ValueError(f"tools/{path.name} setups.{name}: a fetch setup needs pricing.fetch_usd")
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
    run = agent.AgentRun()
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
        )
    for left in (1, 0):
        agent._run_fetch(
            fetch_call,
            run=run,
            conversation=conversation,
            backend=None,
            max_fetches=left,
            fetch_fn=lambda u, o: dict(PROBE_PAGE),
        )
    return conversation


def contract(questions: list[dict]) -> dict:
    """What the model sees: the first request of every question in every mode, and the tool-output shaping."""

    def first_turns(row: dict) -> dict:
        text = facts.question(row)
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
    """The request this arm sends for a fixed query (and a fixed fetch), recorded offline, plus its tool schema, a
    digest of the answer-path code and the versions of the packages that code imports."""
    files = sorted(_imported(HERE / "obkit.py"))
    answer_path = {"code": _code_digest(files), "packages": _packages(files)}
    if ARMS[setup][1] == "no_search":
        return {"setup": setup, **answer_path}
    sent: list = []

    def record(method, url, **kwargs):
        sent.append({"method": method, "url": url, "headers": sorted(kwargs.pop("headers", None) or {}), **kwargs})
        raise _Sent

    source = (HERE / "search.py").read_text()
    names = set(re.findall(r'os\.environ\.get\("([A-Z0-9_]+)"', source))
    for args in re.findall(r"_first_env\(([^)]*)\)", source):
        names |= set(re.findall(r'"([A-Z0-9_]+)"', args))
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
    return {"tool": agent._search_tool(backend), "sent": json.loads(json.dumps(sent, default=str)), **answer_path}


def _code_digest(files: list[Path]) -> str:
    """sha256 of `files`, each as normalized source (docstrings dropped, `ast.unparse`), so comments, docstrings and
    layout do not count, and every Python version gives the same digest. `search.py` holds every backend, so a change
    there moves every arm."""
    digest = hashlib.sha256()
    for path in files:
        digest.update(f"{path.relative_to(HERE).as_posix()}\n{_source(path)}\n".encode())
    return digest.hexdigest()


def _imported(start: Path) -> set[Path]:
    """`start` and the harness modules it imports, directly or through other harness modules."""
    seen: set[Path] = set()
    todo = [start]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        package = path.relative_to(HERE).parent.parts
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, ast.ImportFrom) and node.level:
                base = [*package[:len(package) - node.level + 1], *(node.module.split(".") if node.module else [])]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "harness":
                base = node.module.split(".")[1:]
            elif isinstance(node, ast.Import):
                todo += _module_files([[*n.name.split(".")[1:]] for n in node.names if n.name.split(".")[0] == "harness"])
                continue
            else:
                continue
            todo += _module_files([base, *[[*base, alias.name] for alias in node.names]])
    return seen


def _module_files(names: list[list[str]]) -> list[Path]:
    files = []
    for parts in names:
        for path in (HERE.joinpath(*parts, "__init__.py"), HERE.joinpath(*parts[:-1], f"{parts[-1]}.py") if parts else None):
            if path is not None and path.is_file():
                files.append(path.resolve())
    return files


def _source(path: Path) -> str:
    tree = ast.parse(path.read_bytes())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _packages(files: list[Path]) -> dict[str, str]:
    """The installed version of every third-party package `files` import, so a different version of one is a
    different setup version. Raises when the venv differs from a pin in requirements.txt or an import has no installed
    package, so a stale venv is refused instead of re-versioning arms. The repo's own packages (`judge`) are skipped."""
    _check_pins()
    names = {
        name.split(".")[0]
        for path in files for node in ast.walk(ast.parse(path.read_bytes()))
        for name in ([n.name for n in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and not node.level and node.module else [])
    } - set(sys.stdlib_module_names) - {"harness"}
    names = {n for n in names if not (HERE.parent / n).is_dir() and not (HERE.parent / f"{n}.py").is_file()}
    owners = importlib.metadata.packages_distributions()
    if missing := sorted(n for n in names if not owners.get(n)):
        raise RuntimeError(f"no installed package provides {', '.join(missing)}: run uv pip install -r requirements.txt")
    return {dist: importlib.metadata.version(dist) for name in sorted(names) for dist in owners[name]}


def _check_pins() -> None:
    """Pins with an environment marker (`; sys_platform == ...`) are skipped: they may not apply to this machine."""
    installed = {_canon(d.metadata["Name"]): d.version for d in importlib.metadata.distributions()}
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;]+)\s*$", (HERE.parent / "requirements.txt").read_text(), re.M)
    if wrong := [f"{name} {installed[_canon(name)]} (pinned {pin})" for name, pin in pins
                 if _canon(name) in installed and installed[_canon(name)] != pin]:
        raise RuntimeError(f"installed versions differ from requirements.txt: {', '.join(wrong)}: "
                           "run uv pip install -r requirements.txt")


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


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


def load_environment() -> None:
    path = HERE.parent / ".env"
    if path.is_file():
        from dotenv import load_dotenv

        load_dotenv(path)


def make_llm_client(*, timeout: float, max_retries: int):
    """Azure Foundry OpenAI-v1 deployment when NEXTGEN env is set, else native OpenAI."""
    from openai import OpenAI

    azure_url = (os.environ.get(AZURE_URL_ENV) or "").rstrip("/")
    azure_key = os.environ.get(AZURE_KEY_ENV) or ""
    if azure_url and azure_key:
        return OpenAI(base_url=azure_url, api_key=azure_key, timeout=timeout, max_retries=max_retries)
    key = os.environ.get("OPENAI_API_KEY") or ""
    if not key:
        raise SystemExit(f"set {AZURE_URL_ENV} + {AZURE_KEY_ENV} (preferred) or OPENAI_API_KEY")
    return OpenAI(api_key=key, timeout=timeout, max_retries=max_retries)


def answer(question: dict, setup: str, *, dry: bool, tracer=None) -> dict:
    if dry:
        return _canned(question, setup)
    load_environment()
    search, fetch, system = _mode(setup)
    run = _run(
        facts.question(question),
        ARMS[setup][0],
        search,
        fetch,
        system,
        make_llm_client(timeout=180, max_retries=1),
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
    fetched = sum(1 for call in run.fetches if call.request_attempted and not call.error)
    per_mtok = CONFIG["usd_per_mtok"]
    vendor_cost = billed * float(price.get("list_usd") or 0) + fetched * float(price.get("fetch_usd") or 0)
    llm_cost = (run.llm_prompt_tokens * per_mtok["input"] + run.llm_completion_tokens * per_mtok["output"]) / 1e6
    cost_facts = {
        "llm_cost_usd": round(llm_cost, 6),
        "vendor_cost_usd": round(vendor_cost, 6),
        "llm_ms": run.llm_ms,
        "search_ms": sum(call.latency_ms for call in run.searches),
        "fetch_ms": sum(call.latency_ms for call in run.fetches),
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


def _sent_call(request: dict) -> dict:
    """A request the stand-in recorded, written as an agent's answer."""
    query = "&".join(f"{name}={value}" for name, value in (request.get("query") or {}).items())
    return {"method": request["method"].upper(), "path": request["path"] + (f"?{query}" if query else ""),
            "operation": request.get("soap") or "", "fields": request.get("body_keys") or []}


def selftest(path: Path) -> int:
    """Offline: every question's reference fix sends a request the answer grader accepts, its starter one the grader
    refuses (unless the row names a known issue), and every setup's backend builds."""
    rows = [json.loads(line) for line in path.read_text().split("\n") if line.strip()]
    failed = 0
    for row in rows:
        accepted = row["retrieval_key"]["accepted"]
        for which, passes in (("gold", True), ("starter", row.get("known_issue") is not None)):
            sent = facts.recorded(row, which)["requests"]
            if not sent or facts.grade(accepted, _sent_call(sent[0])) != passes:
                failed += 1
                print(f"FAIL {row['task_id']} {which}: {'accepted' if not passes else 'refused'} or sent nothing")
    backends = sorted({backend for backend, _ in ARMS.values() if backend != "no_search"})
    for backend in backends:
        vendors.get_backend(backend)
    print(f"questions {len(rows)}: reference fixes and starters graded as expected, {failed} failed")
    print(f"setups {len(ARMS)}: {len(backends)} search backends build")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(selftest(Path(sys.argv[1])))
