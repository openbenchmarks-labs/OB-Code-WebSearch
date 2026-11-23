"""Agent loop with web search and fetch tools."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .search import (
    QUERY_PARAMETER,
    UNSCORED_FAILURES,
    SearchBackend,
    classify_failure,
    fit_search_budget,
    get_backend,
)

WEB_SEARCH_TOOL = {
    "type": "function",
    "name": "web_search",
    "description": (
        "Search the web. Required before you return an answer or file. "
        "Call it to find current docs. You may call it more than once "
        "with a new query. If you are unsure, search again rather than "
        "returning a wrong file."
    ),
    "parameters": {
        "type": "object",
        "properties": {"query": QUERY_PARAMETER},
        "required": ["query"],
        "additionalProperties": False,
    },
    "strict": True,
}

WEB_FETCH_TOOL = {
    "type": "function",
    "name": "web_fetch",
    "description": (
        "Fetch a specific URL through this vendor's native extract/scrape API. "
        "Use after search when snippets are not enough."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "https URL to fetch."},
            "objective": {
                "type": "string",
                "description": "What you need from the page. Empty string for the full page.",
            },
        },
        "required": ["url", "objective"],
        "additionalProperties": False,
    },
    "strict": True,
}


@dataclass
class SearchCall:
    query: str
    error: str | None
    latency_ms: int
    n_hits: int
    http_status: int | None
    hit_urls: list[str]
    # Queries the request carried: each one is one of the max_searches when the request returns a result.
    n_queries: int
    failure_class: str | None
    request_attempted: bool
    notices: list[dict[str, Any]]


@dataclass
class FetchCall:
    url: str
    error: str | None
    latency_ms: int
    http_status: int | None
    failure_class: str | None
    request_attempted: bool


@dataclass
class LlmTurn:
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    tool_names: list[str] = field(default_factory=list)


@dataclass
class AgentRun:
    answer: str = ""
    searches: list[SearchCall] = field(default_factory=list)
    fetches: list[FetchCall] = field(default_factory=list)
    llm_turns: list[LlmTurn] = field(default_factory=list)
    llm_ms: int = 0
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    error: str | None = None


def run_agent(
    question: str,
    *,
    client: Any,
    backend_name: str,
    model: str,
    reasoning_effort: str,
    max_turns: int,
    max_searches: int,
    max_fetches: int,
    max_results: int,
    system: str,
    allow_search: bool,
    allow_fetch: bool,
    text_format: dict[str, Any],
    search_fn: Callable[[str], list[dict[str, str]]] | None = None,
    fetch_fn: Callable[[str, str], dict[str, str]] | None = None,
) -> AgentRun:
    backend = get_backend(backend_name) if allow_search and not search_fn else None
    provider = backend.name if backend is not None else backend_name
    run = AgentRun()
    conversation: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]
    tools = []
    if allow_search:
        tools.append(_search_tool(backend))
    if allow_fetch:
        tools.append(WEB_FETCH_TOOL)
    # Every lane: each search call takes a turn, including separate calls in one model response, so empty or failed
    # searches, which do not use one of the max_searches, are still capped by max_turns.
    extra_search_turns = 0
    for model_turn in range(max_turns):
        turn = model_turn + extra_search_turns
        if turn >= max_turns:
            break
        llm_started = time.perf_counter()
        request = {
            "model": model, "reasoning": {"effort": reasoning_effort},
            "tools": tools, "input": conversation, "max_output_tokens": 4000, "store": False,
            "text": {"format": text_format},
        }
        try:
            response = client.responses.create(**request)
        except Exception as exc:  # noqa: BLE001
            llm_ms = int((time.perf_counter() - llm_started) * 1000)
            run.llm_ms += llm_ms
            run.error = f"{type(exc).__name__}: {exc}"
            run.llm_turns.append(LlmTurn(latency_ms=llm_ms, prompt_tokens=0, completion_tokens=0))
            break
        llm_ms = int((time.perf_counter() - llm_started) * 1000)
        prompt_tokens, completion_tokens = _usage(response)
        run.llm_ms += llm_ms
        run.llm_prompt_tokens += prompt_tokens
        run.llm_completion_tokens += completion_tokens
        calls = _function_calls(response)
        run.llm_turns.append(
            LlmTurn(
                latency_ms=llm_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                tool_names=[_call_name(call) for call in calls],
            )
        )
        if not calls:
            run.answer = (getattr(response, "output_text", None) or "").strip()
            return run
        _append_model_output(conversation, response)
        search_calls_this_response = 0
        for call in calls:
            name = _call_name(call)
            if name == "web_search":
                if not allow_search:
                    conversation.append(_tool_output(call, {"error": "web_search is not available"}))
                    continue
                if search_calls_this_response:
                    if turn + 1 >= max_turns:
                        conversation.append(_tool_output(call, {"error": "turn budget exhausted; answer now"}))
                        continue
                    turn += 1
                    extra_search_turns += 1
                search_calls_this_response += 1
                _run_search(
                    call,
                    run=run,
                    conversation=conversation,
                    backend=backend,
                    provider=provider,
                    max_searches=max_searches,
                    max_results=max_results,
                    search_fn=search_fn,
                    client_model=model,
                )
            elif name == "web_fetch":
                if not allow_fetch:
                    conversation.append(_tool_output(call, {"error": "web_fetch is not available; search again"}))
                    continue
                _run_fetch(
                    call,
                    run=run,
                    conversation=conversation,
                    backend=backend,
                    max_fetches=max_fetches,
                    fetch_fn=fetch_fn,
                )
            else:
                conversation.append(_tool_output(call, {"error": f"unknown tool {name}"}))
            if run.error:
                break
        if run.error:
            break
    return run


def _vendor_meta(backend: SearchBackend | None) -> dict[str, Any]:
    return dict(getattr(backend, "last_meta", None) or {})


def _failed_call_meta(backend: SearchBackend | None, before: Any) -> dict[str, Any]:
    """Meta of the vendor HTTP call made by the tool call that just failed.

    vendor_call replaces backend.last_meta on every HTTP call, so an unchanged
    object means the failure came before any request: record nothing rather than
    an earlier call's endpoint and status.
    """
    after = getattr(backend, "last_meta", None)
    return dict(after) if after is not None and after is not before else {}


MULTI_QUERY_NOTE = "Each query in the list counts as one of your searches."


def _search_tool(backend: SearchBackend | None) -> dict[str, Any]:
    """web_search as every lane sees it; a lane with its vendor's documented parameters (`tool_parameters`) gets those
    parameters under the same name and description, and a lane whose call carries several queries (`queries_field`) is
    told that each query uses one search."""
    parameters = getattr(backend, "tool_parameters", None)
    if not parameters:
        return WEB_SEARCH_TOOL
    note = f" {MULTI_QUERY_NOTE}" if getattr(backend, "queries_field", None) else ""
    return {**WEB_SEARCH_TOOL, "description": WEB_SEARCH_TOOL["description"] + note, "parameters": parameters}


def _run_search(
    call: Any,
    *,
    run: AgentRun,
    conversation: list[dict[str, Any]],
    backend: SearchBackend | None,
    provider: str,
    max_searches: int,
    max_results: int,
    search_fn: Callable[[str], list[dict[str, str]]] | None,
    client_model: str = "",
) -> None:
    # One rule for every lane: each query is one search, so a request carrying N queries uses N of the max_searches
    # (the lane's tool text says so); a search with no results, or one that failed, does not use any; the turn it took
    # still counts. Billing is separate: one billed API request is one price, whatever it carries.
    args = _call_args(call)
    # A lane with its own tool parameters sends the model's fields; `query` records them as JSON.
    tool_lane = bool(getattr(backend, "tool_parameters", None)) and not search_fn
    request = backend.tool_request(args) if tool_lane else None
    if tool_lane:
        query = json.dumps(request, ensure_ascii=False) if request else ""
    else:
        query = str(args.get("query") or "").strip()
    if not query:
        fields = backend.tool_parameters["required"] if tool_lane else ["query"]
        conversation.append(_tool_output(call, {"error": f"{' and '.join(fields)} required"}))
        return
    queries_field = getattr(backend, "queries_field", None) if tool_lane else None
    left = max_searches - sum(done.n_queries for done in run.searches if done.n_hits > 0)
    if left <= 0:
        conversation.append(_tool_output(call, {"error": "search budget exhausted; answer now"}))
        return
    budget_note = ""
    if queries_field and len(request[queries_field]) > left:
        request[queries_field] = request[queries_field][:left]
        query = json.dumps(request, ensure_ascii=False)
        budget_note = f"only {left} of your searches were left, so only the first {left} queries were run"
    n_queries = len(request[queries_field]) if queries_field else 1
    search_started = time.perf_counter()
    meta: dict[str, Any] = {}
    before = getattr(backend, "last_meta", None)
    try:
        if search_fn:
            hits = search_fn(query)
        elif tool_lane:
            hits = backend.search_tool(request, max_results=max_results, client_model=client_model)
        else:
            hits = backend.search(query, max_results=max_results)
        if not search_fn:
            meta = _vendor_meta(backend)
        error = None
    except Exception as exc:  # noqa: BLE001
        hits = []
        error = f"{type(exc).__name__}: {exc}"
        meta = _failed_call_meta(backend, before)
    # Vendor latency is the HTTP call alone (`http_ms`), not our rate-pacing wait before it.
    latency_ms = int(meta["http_ms"] if meta.get("http_ms") is not None else (time.perf_counter() - search_started) * 1000)
    hits = fit_search_budget(hits)
    hop = SearchCall(
        query=query,
        error=error,
        latency_ms=latency_ms,
        n_hits=len(hits),
        http_status=meta.get("http_status"),
        hit_urls=[str(hit.get("url") or "") for hit in hits if hit.get("url")],
        n_queries=n_queries,
        failure_class=classify_failure(meta, error),
        request_attempted=bool(meta.get("request")),
        notices=meta.get("notices", []),
    )
    run.searches.append(hop)
    # Exa's own tool output leads with the notices, then the results: exa_py/tools.py:32-37.
    tool_result: dict[str, Any] = {**(request if tool_lane else {"query": query}),
                                   **({"notices": hop.notices} if hop.notices else {}),
                                   **({"budget": _budget_text(n_queries, hop.n_hits > 0, left, budget_note)}
                                      if queries_field else {}),
                                   "hits": hits}
    if error:
        # The vendor's own error text, the same for every vendor, so the model can act on it.
        vendor_text = str(meta.get("error_body") or "")[:500]
        tool_result["error"] = f"search failed ({hop.failure_class}, HTTP {hop.http_status})" + (
            f": {vendor_text}" if vendor_text else "")
        if hop.failure_class in UNSCORED_FAILURES:
            run.error = error
        if hop.failure_class == "account_billing":
            # Out of credit fails every later search of this vendor: stop the round, so no attempt is scored on it.
            raise BudgetStop(f"{provider} account out of credit (HTTP {hop.http_status})")
    conversation.append(_tool_output(call, tool_result))


def _budget_text(n_queries: int, counted: bool, left: int, truncated: str) -> str:
    """What a multi-query call used of the search budget, in the tool result."""
    used = n_queries if counted else 0
    text = f"{n_queries} queries: used {used} of your searches, {left - used} left" if n_queries > 1 else (
        f"used {used} of your searches, {left - used} left")
    return f"{truncated}; {text}" if truncated else text


def _run_fetch(
    call: Any,
    *,
    run: AgentRun,
    conversation: list[dict[str, Any]],
    backend: SearchBackend | None,
    max_fetches: int,
    fetch_fn: Callable[[str, str], dict[str, str]] | None,
) -> None:
    if len(run.fetches) >= max_fetches:
        conversation.append(_tool_output(call, {"error": "fetch budget exhausted; answer now"}))
        return
    args = _call_args(call)
    url = str(args.get("url") or "").strip()
    objective = str(args.get("objective") or "").strip()
    if not url.startswith(("http://", "https://")):
        conversation.append(_tool_output(call, {"error": "url must be http(s)"}))
        return
    fetch_started = time.perf_counter()
    before = getattr(backend, "last_meta", None)
    try:
        page = fetch_fn(url, objective) if fetch_fn else backend.fetch(url, objective=objective)
        meta = {} if fetch_fn else _vendor_meta(backend)
        error = None
    except Exception as exc:  # noqa: BLE001
        page = {"url": url, "title": "", "content": ""}
        error = f"{type(exc).__name__}: {exc}"
        meta = _failed_call_meta(backend, before)
    hop = FetchCall(
        url=str(page.get("url") or url),
        error=error,
        latency_ms=int((time.perf_counter() - fetch_started) * 1000),
        http_status=meta.get("http_status"),
        failure_class=classify_failure(meta, error),
        request_attempted=bool(meta.get("request")),
    )
    run.fetches.append(hop)
    fetch_result: dict[str, Any] = {
        "url": page.get("url") or url,
        "title": page.get("title") or "",
        "content": page.get("content") or "",
    }
    if error:
        fetch_result["error"] = f"fetch failed ({hop.failure_class}, HTTP {hop.http_status})"
        if hop.failure_class in UNSCORED_FAILURES:
            run.error = error
    conversation.append(_tool_output(call, fetch_result))


def _function_calls(response: Any) -> list[Any]:
    return [item for item in (getattr(response, "output", None) or []) if _item_type(item) == "function_call"]


def _item_type(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("type") or "")
    return str(getattr(item, "type", "") or "")


def _call_name(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("name") or "")
    return str(getattr(call, "name", "") or "")


def _call_id(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("call_id") or call.get("id") or "")
    return str(getattr(call, "call_id", None) or getattr(call, "id", "") or "")


def _call_args(call: Any) -> dict[str, Any]:
    raw = call.get("arguments") if isinstance(call, dict) else getattr(call, "arguments", "")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _append_model_output(conversation: list[dict[str, Any]], response: Any) -> None:
    for item in getattr(response, "output", None) or []:
        if _item_type(item) in {"reasoning", "message"}:
            continue
        conversation.append(_item_as_input(item))


def _item_as_input(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    payload = {
        "type": getattr(item, "type", None),
        "id": getattr(item, "id", None),
        "call_id": getattr(item, "call_id", None),
        "name": getattr(item, "name", None),
        "arguments": getattr(item, "arguments", None),
        "status": getattr(item, "status", None),
    }
    return {key: value for key, value in payload.items() if value is not None}


class BudgetStop(RuntimeError):
    """The vendor account is out of credit; the round stops here."""


def _tool_output(call: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": _call_id(call), "output": json.dumps(payload, ensure_ascii=False)}


def _usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    return int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)
