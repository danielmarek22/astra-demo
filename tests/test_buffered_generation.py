"""Message and metadata APIs share one atomic native inference implementation."""

import json
import unittest
from unittest.mock import Mock, patch

from app.llm.base import BufferedGeneration, InferenceFailure
from app.llm.ollama_stream import OllamaClient
from tests.support import make_response


class BufferedGenerationTests(unittest.TestCase):
    def client(self, chunks):
        client = OllamaClient("fixture", "http://localhost:11434", thinking_enabled=True)
        self.addCleanup(client.close)
        client.session.post = Mock(return_value=make_response(lines=[json.dumps(chunk).encode() for chunk in chunks]))
        return client

    def test_metadata_is_available_without_telemetry_or_thinking_logging(self):
        call = {"function": {"name": "demo__run", "arguments": {"value": "żółć"}}}
        client = self.client([
            {"message": {"thinking": "plan", "content": "Checking "}, "done": False},
            {"message": {"content": "now", "tool_calls": [call]}, "done": True,
             "done_reason": "stop", "prompt_eval_count": 17, "eval_count": 9},
        ])
        with patch("app.logging._TRACE_INCLUDE_THINKING", False):
            result = client.chat_buffered_result(
                [{"role": "user", "content": "Question"}], think_override=True,
                options_override={"num_ctx": 16384, "num_predict": 1024},
                timeout_override=45, generation_deadline_s=50, tools=[],
                generation_phase="continuation", react_iteration=2, telemetry_session_id="s",
            )
        self.assertIsInstance(result, BufferedGeneration)
        self.assertEqual(result.message, {"thinking": "plan", "content": "Checking now", "tool_calls": [call]})
        self.assertEqual((result.done_reason, result.prompt_tokens, result.generated_tokens), ("stop", 17, 9))
        self.assertTrue(result.inference_id)
        client.session.post.assert_called_once()
        request = client.session.post.call_args.kwargs
        self.assertEqual(request["timeout"], 45)
        self.assertEqual(request["json"]["options"]["num_ctx"], 16384)
        self.assertEqual(request["json"]["options"]["num_predict"], 1024)
        self.assertTrue(request["json"]["think"])

    def test_message_wrapper_preserves_payload_shape_and_single_request(self):
        chunks = [{"message": {"content": "Answer", "thinking": "plan"}, "done": True,
                   "done_reason": "stop", "prompt_eval_count": 8, "eval_count": 4}]
        plain, rich = self.client(chunks), self.client(chunks)
        message = plain.chat_buffered([], think_override=False, tools=[])
        result = rich.chat_buffered_result([], think_override=False, tools=[])
        self.assertEqual(type(message), dict)
        self.assertEqual(message, result.message)
        self.assertEqual(plain.session.post.call_args.kwargs, rich.session.post.call_args.kwargs)
        plain.session.post.assert_called_once()
        rich.session.post.assert_called_once()
        self.assertEqual(set(message), {"content", "thinking"})

    def test_missing_or_invalid_usage_is_unknown_and_zero_is_preserved(self):
        for value in (None, True, -1, 1.5, "3", 0):
            with self.subTest(value=value):
                result = self.client([{"done": True, "prompt_eval_count": value, "eval_count": value}]).chat_buffered_result([])
                expected = 0 if type(value) is int and value == 0 else None
                self.assertEqual(result.prompt_tokens, expected)
                self.assertEqual(result.generated_tokens, expected)
                self.assertIsNone(result.done_reason)

    def test_length_finish_is_exposed_without_changing_message_only_behavior(self):
        chunks = [{"message": {"content": "truncated"}, "done": True, "done_reason": "length"}]
        result = self.client(chunks).chat_buffered_result([])
        self.assertEqual(result.done_reason, "length")
        self.assertEqual(self.client(chunks).chat_buffered([]), result.message)

    def test_failed_stream_has_no_partial_result_and_is_not_retried(self):
        for method in ("chat_buffered", "chat_buffered_result"):
            with self.subTest(method=method):
                client = self.client([{"message": {"thinking": "partial", "tool_calls": [
                    {"function": {"name": "demo__run", "arguments": {}}}]}, "done": False}])
                with self.assertRaisesRegex(InferenceFailure, "completed generation marker"):
                    getattr(client, method)([])
                client.session.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
