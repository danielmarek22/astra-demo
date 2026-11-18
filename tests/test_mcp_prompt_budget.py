import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from app.core.execution_state import ExecutionSettings
from app.core.prompt_budget import (
    PromptBudget,
    PromptBudgetError,
    compact_mcp_observation,
    tool_groups,
)
from app.core.response_generator import ResponseGenerator
from app.core.task_workspace import TaskWorkspaceMessage

NAME = "mcp__gmail_message_search_fixture"
PROVENANCE = {"server_id": "gmail", "tool_name": "message-search", "capability": NAME,
              "schema_fingerprint": "fixture-fingerprint"}


def observation(text, *, status="success", structured=None, **metadata):
    return f"[{status}] " + json.dumps({
        "kind": "mcp_tool_result", **PROVENANCE,
        "content": [{"type": "text", "text": text}],
        "structured_content": structured, "truncated": False, **metadata,
    })


def messages(content):
    return [
        {"role": "system", "content": "Mandatory policy"},
        TaskWorkspaceMessage(role="assistant", content="Keep workspace exact"),
        {"role": "user", "content": "Search the fixture inbox"},
        {"role": "assistant", "content": "Selected preamble", "thinking": 'Keep this complete plan: "żółć"\n',
         "tool_calls": [{"function": {"name": NAME, "arguments": {"query": "fixture", "limit": 5}}}]},
        {"role": "tool", "tool_name": NAME, "content": content},
    ]


class MCPPromptBudgetTests(unittest.TestCase):
    def budget(self, window=4000):
        return PromptBudget(context_window_tokens=window, completion_reserve_tokens=256,
                            safety_margin_tokens=128, text_bytes_per_token=2.0)

    def assert_fits(self, policy, prepared, tools):
        self.assertLessEqual(policy.estimate(prepared.messages, tools), prepared.metrics["prompt_limit_tokens"])
        self.assertEqual(prepared.metrics["estimated_overflow_tokens"], 0)
        tool_groups(prepared.messages)

    def test_small_mcp_evidence_is_exact_and_not_relabelled(self):
        source = messages(observation("Complete evidence", structured={"count": 1}))
        original = deepcopy(source)
        prepared = self.budget().prepare(source, [], {})
        self.assertEqual(prepared.messages, original)
        self.assertEqual(prepared.metrics["mcp_observations_reduced"], 0)
        self.assertEqual(source, original)

    def test_recorded_gmail_request_shape_now_fits_with_exact_latest_continuation(self):
        catalog = json.loads((Path(__file__).parent / "fixtures/mcp_gmail_2_3_3/catalog.json").read_text())
        search = next(tool for tool in catalog["tools"] if tool["name"] == "message-search")
        tools = [{"type": "function", "function": {"name": NAME, "parameters": search["input_schema"]}}]
        tools.extend({"type": "function", "function": {"name": f"fixture__tool_{index}", "description": "",
                                                     "parameters": {"type": "object"}}} for index in range(22))
        size = lambda: len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")).encode())
        tools[-1]["function"]["description"] = "x" * (24034 - size())
        self.assertEqual(size(), 24034)  # Recorded live schema footprint; no mail data.
        source = messages(observation("Synthetic inbox item.\n" * 600, truncated=True))
        source[0]["content"] = "Mandatory policy. " * 760
        source[1]["content"] = "Workspace evidence. " * 60
        original, original_tools = deepcopy(source), deepcopy(tools)
        policy = PromptBudget(context_window_tokens=32768, completion_reserve_tokens=2048,
                              safety_margin_tokens=1024, text_bytes_per_token=2.0)
        options = {"num_ctx": 32768, "num_predict": 8192}
        with patch("app.core.prompt_budget.compact_mcp_observation", return_value=None), \
                self.assertRaises(PromptBudgetError):
            policy.prepare(source, tools, options)
        prepared = policy.prepare(source, tools, options)
        self.assertEqual(prepared.metrics["prompt_limit_tokens"], 23552)
        self.assertEqual(prepared.metrics["mcp_observations_reduced"], 1)
        self.assertEqual(prepared.messages[-2], original[-2])
        self.assertIn(dict(original[1]), prepared.messages)
        payload = json.loads(prepared.messages[-1]["content"][len("[success] "):])
        self.assertTrue(payload["context_budget_truncated"])
        self.assertTrue(payload["truncated"])
        self.assertTrue(any(block.get("text") for block in payload["content"]))
        self.assertLess(len(payload["content"][0]["text"]), len(json.loads(original[-1]["content"][10:])["content"][0]["text"]))
        self.assertEqual({key: payload[key] for key in PROVENANCE}, PROVENANCE)
        self.assertEqual(source, original)
        self.assertEqual(tools, original_tools)
        self.assertEqual(prepared.options_override, options)
        self.assert_fits(policy, prepared, tools)

    def test_duplicate_structured_output_is_omitted_before_text_is_shortened(self):
        source = messages(observation("Keep all this text", structured={"items": "x" * 9000}))
        original = deepcopy(source)
        prepared = self.budget().prepare(source, [], {})
        payload = json.loads(prepared.messages[-1]["content"][10:])
        self.assertEqual(payload["content"][0]["text"], "Keep all this text")
        self.assertIsNone(payload["structured_content"])
        self.assertTrue(payload["structured_content_omitted"])
        self.assertEqual(source, original)
        self.assert_fits(self.budget(), prepared, [])

    def test_structured_only_output_retains_a_marked_unicode_excerpt(self):
        content = observation("", structured={"items": ['Quoted "żółć"\n\\' * 1000]})
        source = messages(content)
        prepared = self.budget().prepare(source, [], {})
        payload = json.loads(prepared.messages[-1]["content"][10:])
        self.assertTrue(payload["structured_content_excerpt"])
        self.assertTrue(payload["structured_content_omitted"])
        self.assertTrue(payload["context_budget_truncated"])
        self.assertTrue(any(block.get("text") for block in payload["content"]))
        self.assertEqual(prepared.messages[-2], source[-2])
        self.assert_fits(self.budget(), prepared, [])

    def test_error_status_provenance_unsupported_markers_and_links_survive(self):
        content = observation("External error detail. " * 2000, status="error", completion="uncertain",
                              suppress_for_turn=True, unsupported_content=["image"])
        payload = json.loads(content[len("[error] "):])
        link = {"type": "resource_link", "uri": "https://example.invalid/evidence", "name": "Source"}
        payload["content"].append(link)
        source = messages("[error] " + json.dumps(payload))
        original = deepcopy(source)
        prepared = self.budget().prepare(source, [], {})
        content = prepared.messages[-1]["content"]
        self.assertTrue(content.startswith("[error] "))
        reduced = json.loads(content[len("[error] "):])
        for key in (*PROVENANCE, "completion", "suppress_for_turn", "unsupported_content"):
            self.assertEqual(reduced[key], payload[key])
        self.assertEqual(reduced["content"][-1], link)
        self.assertEqual(prepared.messages[-2], original[-2])
        self.assertEqual(source, original)
        self.assert_fits(self.budget(), prepared, [])

    def test_only_normalized_mcp_evidence_can_be_shortened(self):
        for content in ("[success] not JSON", '[success] {"kind":"other"}',
                        '[success] {"kind":"mcp_tool_result","content":"wrong"}',
                        '[success] {"kind":"mcp_tool_result","content":[{"type":"text","text":3}]}',
                        '[denied] {"kind":"mcp_tool_result","content":[]}'):
            with self.subTest(content=content):
                self.assertIsNone(compact_mcp_observation(content, 0))
        source = messages(observation("Evidence. " * 3000))
        for mode in ("continuation_only", "other_namespace"):
            with self.subTest(mode=mode):
                candidate = deepcopy(source)
                if mode == "other_namespace":
                    candidate[-1]["tool_name"] = "demo__run"
                    candidate[-2]["tool_calls"][0]["function"]["name"] = "demo__run"
                original = deepcopy(candidate)
                with self.assertRaises(PromptBudgetError):
                    self.budget().prepare(candidate, [], {}, continuation_only=mode == "continuation_only")
                self.assertEqual(candidate, original)

    def test_oversized_protected_thinking_and_links_still_fail_with_safe_metrics(self):
        source = messages(observation("Evidence"))
        source[-2]["thinking"] = "PRIVATE required plan " * 1000
        original = deepcopy(source)
        with self.assertRaises(PromptBudgetError) as failure:
            self.budget().prepare(source, [], {})
        metrics = failure.exception.metrics
        self.assertGreater(metrics["estimated_input_tokens"], metrics["prompt_limit_tokens"])
        self.assertGreater(metrics["estimated_overflow_tokens"], 0)
        self.assertNotIn("PRIVATE", str(failure.exception) + str(metrics))
        self.assertEqual(source, original)
        content = observation("text")
        payload = json.loads(content[10:])
        payload["content"] = [{"type": "resource_link", "uri": "https://example.invalid/" + "x" * 20000}]
        with self.assertRaises(PromptBudgetError):
            self.budget().prepare(messages("[success] " + json.dumps(payload)), [], {})

    def test_failure_trace_reports_budget_numbers_without_contents_or_inference(self):
        generator = ResponseGenerator.__new__(ResponseGenerator)
        generator.execution_settings = ExecutionSettings()
        generator.prompt_budget = self.budget()
        generator.llm = Mock()
        generator.llm.generation_options.return_value = {"num_ctx": 4000, "num_predict": 256}
        with patch("app.core.response_generator.trace_event") as trace, self.assertRaises(PromptBudgetError):
            generator._budget_request("fixture", [{"role": "user", "content": "PRIVATE " * 3000}])
        event = trace.call_args.kwargs["payload"]
        self.assertGreater(event["estimated_overflow_tokens"], 0)
        self.assertEqual(event["prompt_limit_tokens"], 3616)
        self.assertNotIn("PRIVATE", str(event))
        generator.llm.chat_buffered_result.assert_not_called()
