"""Unit tests for judge.diagnostics module with obkit run rows."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import unittest
from judge.diagnostics import diagnose_row, analyze_obkit_run


class TestObkitDiagnostics(unittest.TestCase):

    def setUp(self):
        self.question_rest = {
            "task_id": "hard_rest",
            "retrieval_key": {
                "accepted": [
                    {"method": "POST", "address": "/api/v2/documentation/search", "soap": None}
                ]
            },
        }
        self.question_expand = {
            "task_id": "hard_expand",
            "retrieval_key": {
                "accepted": [
                    {"method": "GET", "address": "/odata/v2/BenefitCoverage?$expand=benefit", "soap": None}
                ]
            },
        }
        self.question_soap = {
            "task_id": "hard_soap",
            "retrieval_key": {
                "accepted": [
                    {"method": "POST", "address": "/ccx/service/tenant/worker/v42.0", "soap": "Get_Workers_Request"}
                ]
            },
        }

    def test_vendor_empty_diagnosis(self):
        row = {
            "task_id": "hard_rest",
            "setup": "tavily_advanced",
            "answer": "",
            "status": "vendor_empty",
            "passed": False,
        }
        diag = diagnose_row(row, self.question_rest)
        self.assertFalse(diag["passed"])
        self.assertEqual(diag["category"], "search_exhaustion")
        self.assertEqual(diag["tag"], "empty_results")

    def test_vendor_error_diagnosis(self):
        row = {
            "task_id": "hard_rest",
            "setup": "linkup_fast",
            "answer": "",
            "status": "vendor_error",
            "error": "HTTP 429 Rate Limited",
            "passed": False,
        }
        diag = diagnose_row(row, self.question_rest)
        self.assertFalse(diag["passed"])
        self.assertEqual(diag["category"], "vendor_error")
        self.assertEqual(diag["tag"], "search_api_error")

    def test_infra_error_diagnosis(self):
        row = {
            "task_id": "hard_rest",
            "setup": "exa_auto",
            "answer": "",
            "status": "infra_error",
            "error": "Connection timed out",
            "passed": False,
        }
        diag = diagnose_row(row, self.question_rest)
        self.assertFalse(diag["passed"])
        self.assertEqual(diag["category"], "infra_error")
        self.assertEqual(diag["tag"], "model_or_system_error")

    def test_wrong_method_right_path(self):
        row = {
            "task_id": "hard_rest",
            "setup": "brave",
            "answer": '{"method": "GET", "path": "/api/v2/documentation/search", "operation": "", "fields": []}',
            "status": "ok",
            "passed": False,
        }
        diag = diagnose_row(row, self.question_rest)
        self.assertFalse(diag["passed"])
        self.assertEqual(diag["category"], "model_error")
        self.assertEqual(diag["tag"], "wrong_method_right_path")

    def test_missing_query_parameters(self):
        row = {
            "task_id": "hard_expand",
            "setup": "perplexity_web_tool",
            "answer": '{"method": "GET", "path": "/odata/v2/BenefitCoverage", "operation": "", "fields": []}',
            "status": "ok",
            "passed": False,
        }
        diag = diagnose_row(row, self.question_expand)
        self.assertFalse(diag["passed"])
        self.assertEqual(diag["category"], "model_error")
        self.assertEqual(diag["tag"], "missing_query_parameters")

    def test_wrong_path(self):
        row = {
            "task_id": "hard_rest",
            "setup": "serp",
            "answer": '{"method": "POST", "path": "/api/v1/other/path", "operation": "", "fields": []}',
            "status": "ok",
            "passed": False,
        }
        diag = diagnose_row(row, self.question_rest)
        self.assertFalse(diag["passed"])
        self.assertEqual(diag["category"], "model_error")
        self.assertEqual(diag["tag"], "wrong_path_right_method")

    def test_soap_service_mismatch(self):
        row = {
            "task_id": "hard_soap",
            "setup": "tako",
            "answer": '{"method": "POST", "path": "/ccx/service/tenant/financial/v42.0", "operation": "Get_Workers_Request", "fields": []}',
            "status": "ok",
            "passed": False,
        }
        diag = diagnose_row(row, self.question_soap)
        self.assertFalse(diag["passed"])
        self.assertEqual(diag["category"], "model_error")
        self.assertEqual(diag["tag"], "wrong_soap_service")

    def test_unreadable_and_no_call_answer(self):
        row1 = {"task_id": "hard_rest", "setup": "s1", "answer": "not json", "status": "ok", "passed": False}
        diag1 = diagnose_row(row1, self.question_rest)
        self.assertEqual(diag1["tag"], "unreadable_answer")

        row2 = {"task_id": "hard_rest", "setup": "s1", "answer": '{"foo": "bar"}', "status": "ok", "passed": False}
        diag2 = diagnose_row(row2, self.question_rest)
        self.assertEqual(diag2["tag"], "no_call")

    def test_passed_row(self):
        row = {
            "task_id": "hard_rest",
            "setup": "tavily_advanced",
            "answer": '{"method": "POST", "path": "/api/v2/documentation/search", "operation": "", "fields": []}',
            "status": "ok",
            "passed": True,
        }
        diag = diagnose_row(row, self.question_rest)
        self.assertTrue(diag["passed"])
        self.assertEqual(diag["category"], "success")
        self.assertEqual(diag["tag"], "passed")

    def test_analyze_obkit_run_summary(self):
        questions = {"hard_rest": self.question_rest}
        rows = [
            {"task_id": "hard_rest", "setup": "s1", "passed": True, "status": "ok", "cost_usd": 0.01, "facts": {"search_ms": 100, "llm_ms": 200}},
            {"task_id": "hard_rest", "setup": "s1", "passed": False, "status": "vendor_empty", "cost_usd": 0.0, "facts": {"search_ms": 50, "llm_ms": 0}},
        ]
        report = analyze_obkit_run(rows, questions)
        summary = report["summary"]
        self.assertEqual(summary["total_runs"], 2)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["pass_rate_pct"], 50.0)
        self.assertIn("s1", summary["setups"])
        self.assertEqual(summary["setups"]["s1"]["passed"], 1)
        self.assertEqual(summary["setups"]["s1"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
