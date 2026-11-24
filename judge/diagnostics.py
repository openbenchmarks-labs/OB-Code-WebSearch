"""judge/diagnostics.py: Root-cause diagnostics for obkit benchmark run rows.

Classifies agent misses into actionable root causes:
- search_exhaustion: search tool returned 0 hits (status: vendor_empty)
- vendor_error: search API returned error / rate limit (status: vendor_error)
- infra_error: model timeout or harness exception (status: infra_error)
- model_error: agent retrieved docs but produced an incorrect call:
    - wrong_method_right_path: right endpoint, but incorrect HTTP method (e.g. GET instead of POST)
    - missing_query_parameters: right endpoint and method, but missing required $expand
    - wrong_key_labels: entity keys or parameters misnamed
    - wrong_path: called wrong endpoint or unrelated path
    - unreadable_answer / no_call: invalid JSON or missing call structure
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .checks import LABEL_ALIAS, _answer_keys, _expands, _op, _same, _segments, _service


def _diagnose_rest_miss(call: dict[str, Any], right: dict[str, Any]) -> str:
    """Classify why a REST call missed this accepted answer."""
    method_match = (call.get("method") or "").upper() == right["method"].upper()
    got_segs = _segments(call.get("path") or "")
    want_segs = _segments(right["address"])
    path_match = _same(got_segs, want_segs)

    if path_match:
        if not method_match:
            return "wrong_method_right_path"
        query = str(call.get("path") or "") + "&" + "&".join(map(str, call.get("fields") or []))
        need = ([right["require_query"]] if right.get("require_query") else []) + _expands(right["address"])
        expand_match = all(
            re.search(r"(?:^|[?&\s])\$?expand=[^&\s]*\b" + re.escape(n) + r"\b", query, re.I)
            for n in need
        )
        if not expand_match:
            return "missing_query_parameters"
        return "passed"

    bare = lambda segs: [re.sub(r"\(.*\)$", "", s) for s in segs]
    if _same(bare(got_segs), bare(want_segs)):
        return "wrong_key_labels"

    return "wrong_path_right_method" if method_match else "wrong_path"


def _diagnose_soap_miss(call: dict[str, Any], right: dict[str, Any]) -> str:
    """Classify why a SOAP call missed this accepted answer."""
    sent_to = [x.lower() for x in re.split(r"[/?]", call.get("path") or "")]
    service_match = _service(right["address"]) in sent_to
    op_match = bool(call.get("operation") and _op(call["operation"]) == _op(right["soap"]))

    if op_match and service_match:
        return "passed"
    if op_match and not service_match:
        return "wrong_soap_service"
    return "wrong_soap_operation"


def diagnose_row(row: dict[str, Any], question: dict[str, Any] | None = None) -> dict[str, Any]:
    """Diagnose a single obkit run row.

    Args:
        row: An obkit run result dict containing:
             task_id, setup, answer, status, error, cost_usd, facts, passed
        question: Optional question dict containing retrieval_key.accepted

    Returns:
        Structured diagnosis with category, tag, and explanation.
    """
    task_id = row.get("task_id") or row.get("id", "")
    setup = row.get("setup") or row.get("backend", "")
    status = row.get("status", "ok")
    error = row.get("error")
    passed = bool(row.get("passed", False))

    if passed:
        return {
            "task_id": task_id,
            "setup": setup,
            "passed": True,
            "category": "success",
            "tag": "passed",
            "detail": "Answer matches accepted ground truth.",
        }

    # 1. Search tool returned 0 hits
    if status == "vendor_empty":
        return {
            "task_id": task_id,
            "setup": setup,
            "passed": False,
            "category": "search_exhaustion",
            "tag": "empty_results",
            "detail": "Search tool returned 0 hits across all query turns.",
        }

    # 2. Search tool API error or rate-limit
    if status == "vendor_error":
        return {
            "task_id": task_id,
            "setup": setup,
            "passed": False,
            "category": "vendor_error",
            "tag": "search_api_error",
            "detail": str(error or "Search backend failed with an HTTP or rate-limit error."),
        }

    # 3. Model failure or harness timeout
    if status == "infra_error" or (error and status != "ok"):
        return {
            "task_id": task_id,
            "setup": setup,
            "passed": False,
            "category": "infra_error",
            "tag": "model_or_system_error",
            "detail": str(error or "Model call or harness infrastructure timed out."),
        }

    # 4. Agent produced an answer, but the call was rejected by the grader
    raw_answer = row.get("answer", "")
    try:
        call = json.loads(raw_answer) if isinstance(raw_answer, str) else (raw_answer or {})
    except Exception:
        return {
            "task_id": task_id,
            "setup": setup,
            "passed": False,
            "category": "model_error",
            "tag": "unreadable_answer",
            "detail": "Agent output could not be parsed as valid JSON.",
        }

    if not isinstance(call, dict) or not (call.get("path") or call.get("operation")):
        return {
            "task_id": task_id,
            "setup": setup,
            "passed": False,
            "category": "model_error",
            "tag": "no_call",
            "detail": "Agent answer did not contain a path or operation.",
        }

    accepted = (question.get("retrieval_key", {}).get("accepted") if question else None) or []
    if not accepted:
        return {
            "task_id": task_id,
            "setup": setup,
            "passed": False,
            "category": "model_error",
            "tag": "rejected_call",
            "detail": "Call failed grading (no accepted answers provided for granular diff).",
        }

    misses: list[str] = []
    for right in accepted:
        if right.get("soap"):
            tag = _diagnose_soap_miss(call, right)
        else:
            tag = _diagnose_rest_miss(call, right)
        if tag == "passed":
            # Grader says false, but matches here; edge case
            return {
                "task_id": task_id,
                "setup": setup,
                "passed": True,
                "category": "success",
                "tag": "passed",
                "detail": "Answer matches accepted ground truth.",
            }
        misses.append(tag)

    # Order of nearness to right answer
    nearness = (
        "wrong_path",
        "wrong_soap_operation",
        "wrong_soap_service",
        "wrong_path_right_method",
        "wrong_key_labels",
        "wrong_method_right_path",
        "missing_query_parameters",
    )
    best_tag = max(misses, key=lambda t: nearness.index(t) if t in nearness else -1)

    details_map = {
        "wrong_method_right_path": "Agent identified the correct endpoint path, but used the wrong HTTP method.",
        "missing_query_parameters": "Endpoint and method matched, but required $expand or query parameter was omitted.",
        "wrong_key_labels": "Entity key labels in parentheses differed from accepted aliases.",
        "wrong_path_right_method": "HTTP method was correct, but path segments did not match the target endpoint.",
        "wrong_path": "Neither path nor method matched the target endpoint.",
        "wrong_soap_service": "SOAP operation was correct, but was sent to the wrong Workday service path.",
        "wrong_soap_operation": "SOAP operation did not match ground truth.",
    }

    return {
        "task_id": task_id,
        "setup": setup,
        "passed": False,
        "category": "model_error",
        "tag": best_tag,
        "detail": details_map.get(best_tag, best_tag),
    }


def analyze_obkit_run(rows: list[dict[str, Any]], questions_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compile summary statistics and failure attributions across all obkit run rows."""
    total = len(rows)
    passed_count = sum(1 for r in rows if r.get("passed"))
    failed_count = total - passed_count

    tag_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    setup_stats: dict[str, dict[str, Any]] = {}
    diagnoses: list[dict[str, Any]] = []

    total_cost = 0.0
    total_search_ms = 0
    total_llm_ms = 0

    for r in rows:
        task_id = r.get("task_id") or r.get("id", "")
        setup = r.get("setup") or r.get("backend", "unknown")
        q = questions_by_id.get(task_id)
        diag = diagnose_row(r, q)
        diagnoses.append(diag)

        cat = diag["category"]
        tag = diag["tag"]
        category_counts[cat] = category_counts.get(cat, 0) + 1
        tag_counts[tag] = tag_counts.get(tag, 0) + 1

        if setup not in setup_stats:
            setup_stats[setup] = {"total": 0, "passed": 0, "failed": 0, "failures": {}}
        setup_stats[setup]["total"] += 1
        if diag["passed"]:
            setup_stats[setup]["passed"] += 1
        else:
            setup_stats[setup]["failed"] += 1
            setup_stats[setup]["failures"][tag] = setup_stats[setup]["failures"].get(tag, 0) + 1

        total_cost += float(r.get("cost_usd") or 0.0)
        facts = r.get("facts") or {}
        total_search_ms += int(facts.get("search_ms") or 0)
        total_llm_ms += int(facts.get("llm_ms") or 0)

    for s_name, s_data in setup_stats.items():
        n = s_data["total"]
        s_data["pass_rate_pct"] = round((s_data["passed"] / n * 100) if n else 0.0, 1)

    summary = {
        "total_runs": total,
        "passed": passed_count,
        "failed": failed_count,
        "pass_rate_pct": round((passed_count / total * 100) if total else 0.0, 1),
        "category_breakdown": category_counts,
        "failure_tag_breakdown": tag_counts,
        "setups": setup_stats,
        "totals": {
            "cost_usd": round(total_cost, 4),
            "search_ms": total_search_ms,
            "llm_ms": total_llm_ms,
        },
    }

    return {"summary": summary, "diagnoses": diagnoses}


def sample_obkit_run() -> dict[str, Any]:
    """Generate demonstration obkit run rows to verify diagnostics offline."""
    mock_questions = {
        "hard_gen_search": {
            "task_id": "hard_gen_search",
            "retrieval_key": {
                "accepted": [
                    {"method": "POST", "address": "/api/v2/documentation/search", "soap": None}
                ]
            },
        },
        "hard_sf_coverage": {
            "task_id": "hard_sf_coverage",
            "retrieval_key": {
                "accepted": [
                    {"method": "GET", "address": "/odata/v2/BenefitCoverage?$expand=benefit", "soap": None}
                ]
            },
        },
        "hard_wd_worker": {
            "task_id": "hard_wd_worker",
            "retrieval_key": {
                "accepted": [
                    {"method": "POST", "address": "/ccx/service/worker/v42.0", "soap": "Get_Workers_Request"}
                ]
            },
        },
    }

    mock_rows = [
        # 1. Successful pass
        {
            "task_id": "hard_gen_search",
            "setup": "tavily_advanced",
            "answer": '{"method": "POST", "path": "/api/v2/documentation/search", "operation": "", "fields": []}',
            "status": "ok",
            "passed": True,
            "cost_usd": 0.016,
            "facts": {"search_ms": 320, "llm_ms": 900},
        },
        # 2. Search tool returned 0 hits
        {
            "task_id": "hard_gen_search",
            "setup": "mock_empty_vendor",
            "answer": "",
            "status": "vendor_empty",
            "passed": False,
            "cost_usd": 0.0,
            "facts": {"search_ms": 210, "llm_ms": 0},
        },
        # 3. Model used wrong method (GET instead of POST) on correct path
        {
            "task_id": "hard_gen_search",
            "setup": "tavily_advanced",
            "answer": '{"method": "GET", "path": "/api/v2/documentation/search", "operation": "", "fields": []}',
            "status": "ok",
            "passed": False,
            "cost_usd": 0.016,
            "facts": {"search_ms": 350, "llm_ms": 1100},
        },
        # 4. Model found endpoint but missed required $expand
        {
            "task_id": "hard_sf_coverage",
            "setup": "exa_auto",
            "answer": '{"method": "GET", "path": "/odata/v2/BenefitCoverage", "operation": "", "fields": []}',
            "status": "ok",
            "passed": False,
            "cost_usd": 0.007,
            "facts": {"search_ms": 400, "llm_ms": 850},
        },
        # 5. Search vendor API error
        {
            "task_id": "hard_wd_worker",
            "setup": "linkup_standard",
            "answer": "",
            "status": "vendor_error",
            "error": "HTTP 429 Too Many Requests",
            "passed": False,
            "cost_usd": 0.0,
            "facts": {"search_ms": 100, "llm_ms": 0},
        },
    ]

    return analyze_obkit_run(mock_rows, mock_questions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OB-Code-Websearch obkit failure diagnostics")
    parser.add_argument("--runs", "--results", dest="runs", type=Path, help="JSON or JSONL file containing obkit run rows")
    parser.add_argument("--tasks", type=Path, help="JSONL file of benchmark questions (e.g. data/questions.jsonl)")
    parser.add_argument("--out", type=Path, help="Output path for diagnostics JSON report")
    parser.add_argument("--sample", action="store_true", help="Run sample verification dataset offline")

    args = parser.parse_args(argv)

    if args.sample:
        report = sample_obkit_run()
        print(json.dumps(report, indent=2))
        return 0

    if not args.runs or not args.tasks:
        parser.error("--runs and --tasks are required unless --sample is specified.")

    questions_by_id: dict[str, dict[str, Any]] = {}
    for line in args.tasks.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            questions_by_id[row["task_id"]] = row

    raw_text = args.runs.read_text(encoding="utf-8").strip()
    if raw_text.startswith("["):
        rows = json.loads(raw_text)
    elif raw_text.startswith("{"):
        parsed = json.loads(raw_text)
        rows = parsed.get("runs") or parsed.get("results") or [parsed]
    else:
        rows = [json.loads(line) for line in raw_text.splitlines() if line.strip()]

    report = analyze_obkit_run(rows, questions_by_id)

    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Diagnostics written to {args.out}")
    else:
        print(json.dumps(report["summary"], indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
