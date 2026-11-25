"""Unit tests for context token guard and sliding-window compaction."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import unittest
from harness.agent import compact_conversation


class TestContextGuard(unittest.TestCase):

    def test_under_budget_not_modified(self):
        conversation = [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "Find the API endpoint."},
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": json.dumps({"url": "https://example.com", "content": "short content"}),
            },
        ]
        original_output = conversation[2]["output"]
        compact_conversation(conversation, max_chars=10_000)
        self.assertEqual(conversation[2]["output"], original_output)

    def test_protects_recent_tool_outputs(self):
        # 3 tool outputs: 2 recent should be untouched, 1 older should be compacted
        long_content = "A" * 1000
        conversation = [
            {"role": "system", "content": "System prompt."},
            {"role": "user", "content": "User question."},
            # Older output
            {
                "type": "function_call_output",
                "call_id": "call_old",
                "output": json.dumps({"url": "https://old.com", "content": long_content}),
            },
            # Recent output 1
            {
                "type": "function_call_output",
                "call_id": "call_recent_1",
                "output": json.dumps({"url": "https://recent1.com", "content": long_content}),
            },
            # Recent output 2
            {
                "type": "function_call_output",
                "call_id": "call_recent_2",
                "output": json.dumps({"url": "https://recent2.com", "content": long_content}),
            },
        ]

        compact_conversation(conversation, max_chars=500, keep_recent=2)

        # Recent 1 and 2 must stay untouched
        recent1 = json.loads(conversation[3]["output"])
        recent2 = json.loads(conversation[4]["output"])
        self.assertEqual(recent1["content"], long_content)
        self.assertEqual(recent2["content"], long_content)

        # Old output must be compacted
        old = json.loads(conversation[2]["output"])
        self.assertLess(len(old["content"]), len(long_content))
        self.assertIn("page content trimmed for context window", old["content"])
        self.assertEqual(old["url"], "https://old.com")

    def test_compacts_older_search_snippets(self):
        long_snippet = "Documentation details: " + ("B" * 500)
        conversation = [
            {"role": "system", "content": "System."},
            {"role": "user", "content": "Question."},
            # Older search output
            {
                "type": "function_call_output",
                "call_id": "call_search",
                "output": json.dumps({
                    "query": "ServiceNow API",
                    "hits": [
                        {"url": "https://docs.servicenow.com/table", "title": "Table API", "snippet": long_snippet},
                        {"url": "https://docs.servicenow.com/auth", "title": "Auth", "snippet": long_snippet},
                    ],
                }),
            },
            # Recent dummy output
            {
                "type": "function_call_output",
                "call_id": "call_recent",
                "output": json.dumps({"url": "https://final.com", "title": "Final", "content": "small"}),
            },
        ]

        compact_conversation(conversation, max_chars=100, keep_recent=1)

        old_search = json.loads(conversation[2]["output"])
        for hit in old_search["hits"]:
            self.assertLess(len(hit["snippet"]), 150)
            self.assertTrue(hit["snippet"].endswith("..."))
            self.assertTrue(hit["url"].startswith("https://docs.servicenow.com"))

    def test_preserves_valid_json_structure(self):
        conversation = [
            {"role": "system", "content": "Sys"},
            {"role": "user", "content": "Q"},
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": json.dumps({"content": "C" * 800}),
            },
            {
                "type": "function_call_output",
                "call_id": "call_2",
                "output": json.dumps({"content": "D" * 800}),
            },
        ]

        compact_conversation(conversation, max_chars=50, keep_recent=1)

        for item in conversation:
            if item.get("type") == "function_call_output":
                # Must parse cleanly without error
                parsed = json.loads(item["output"])
                self.assertIsInstance(parsed, dict)


if __name__ == "__main__":
    unittest.main()
