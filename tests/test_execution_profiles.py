"""Configured profiles reach composition and the actual native request boundary."""

import json
import tempfile
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.config import Config
from app.core import orchestrator_factory
from app.core.execution_state import ExecutionLimits, ExecutionProfile, ExecutionSettings
from app.core.prompt_budget import PromptBudget, PromptBudgetError
from app.core.response_generator import ResponseGenerator
from app.integrations import ToolResult
from app.llm.ollama_stream import OllamaClient
from app.storage.database import Database
from tests.support import consume_generator, make_response
from tests.test_memory_store import FakeVectorStore


class ExecutionProfileTests(unittest.TestCase):
    def test_profile_options_reach_correction_and_streamed_recovery_without_extra_requests(self):
        client = OllamaClient("fixture", "http://localhost:11434", thinking_enabled=True,
                              options={"num_ctx": 32768, "num_predict": 2048})
        self.addCleanup(client.close)
        call = {"function": {"name": "beliefs__update", "arguments": {}}}
        responses = iter([
            make_response(lines=[json.dumps({"message": {"tool_calls": [call]}, "done": True}).encode()]),
            make_response(lines=[json.dumps({"message": {"tool_calls": [call]}, "done": True}).encode()]),
            make_response(lines=[b'{"message":{"content":"Recovered."},"done":true}']),
        ])
        requests = []

        def post(*args, **kwargs):
            requests.append(deepcopy(kwargs))
            return next(responses)

        def execute(*args, **kwargs):
            yield from ()
            return ToolResult.error("rejected")

        client.session.post = Mock(side_effect=post)
        settings = ExecutionSettings(ExecutionProfile.DEEP, ExecutionLimits(
            max_inference_steps=4, generation_deadline_s=45, recovery_deadline_s=12,
            num_predict=1024, context_window_tokens=16384, correction_num_predict=128, recovery_num_predict=64,
        ))
        generator = ResponseGenerator(
            client, SimpleNamespace(get_native_tools=lambda **kwargs: [], execute=execute), Mock(),
            execution_settings=settings, prompt_budget=PromptBudget(),
        )
        _, response = consume_generator(generator.stream_late_routed_response(
            "s", [{"role": "user", "content": "Question"}], "Question"))
        self.assertEqual(response, "Recovered.")
        self.assertEqual(len(requests), 3)
        self.assertEqual([request["json"]["options"]["num_predict"] for request in requests], [1024, 128, 64])
        self.assertEqual([request["json"]["options"]["num_ctx"] for request in requests], [16384] * 3)
        self.assertEqual([request["timeout"] for request in requests], [45, 45, 12])
        self.assertEqual([request["json"]["think"] for request in requests], [True, False, False])
        self.assertNotIn("tools", requests[-1]["json"])
        self.assertEqual(client.options, {"num_ctx": 32768, "num_predict": 2048})

    def test_profile_context_overflow_fails_before_inference_without_recovery(self):
        client = OllamaClient("fixture", "http://localhost:11434")
        self.addCleanup(client.close)
        client.session.post = Mock()
        settings = ExecutionSettings(limits=ExecutionLimits(context_window_tokens=4096, num_predict=64))
        generator = ResponseGenerator(client, SimpleNamespace(get_native_tools=lambda **kwargs: []), Mock(),
                                      execution_settings=settings, prompt_budget=PromptBudget())
        with self.assertRaises(PromptBudgetError):
            consume_generator(generator.stream_late_routed_response(
                "s", [{"role": "user", "content": "x" * 5000}], "x" * 5000))
        client.session.post.assert_not_called()

    def test_factory_wires_configured_profile_and_reasoning_policy(self):
        database = Database(":memory:")
        self.addCleanup(database.close)
        vectors = FakeVectorStore()
        vectors.close = Mock()
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as file:
            file.write("execution:\n  default_profile: deep\n  profiles:\n    deep:\n"
                       "      max_inference_steps: 11\n      context_window_tokens: 16384\n"
                       "      recovery_observation_chars: 8192\n"
                       "      max_continuations: 5\n      max_continuation_attempts: 7\n"
                       "      max_tool_dispatches: 4\n      workspace:\n        max_bytes: 4096\n"
                       "reasoning:\n  enabled: true\n"
                       "orchestrator:\n  recovery_observation_chars: 4096\n"
                       "context:\n  prompt_budget:\n    enabled: true\n    text_bytes_per_token: 2.0\n"
                       "integrations:\n  scratchpad:\n    enabled: false\n")
            file.flush()
            config = Config(file.name)
        with patch.object(orchestrator_factory, "Database", return_value=database), \
                patch.object(orchestrator_factory, "VectorStore", return_value=vectors), \
                patch.object(orchestrator_factory, "OllamaClient"):
            orchestrator = orchestrator_factory.build_orchestrator(config)
        self.addCleanup(orchestrator.close)
        settings = orchestrator._response_generator().execution_settings
        self.assertEqual(settings.profile, ExecutionProfile.DEEP)
        self.assertEqual(settings.limits.max_inference_steps, 11)
        self.assertEqual(settings.limits.context_window_tokens, 16384)
        self.assertEqual(settings.limits.recovery_observation_chars, 8192)
        self.assertEqual(orchestrator._response_generator("standard").execution_settings.limits.recovery_observation_chars, 4096)
        self.assertEqual(orchestrator.prompt_budget.text_bytes_per_token, 2.0)
        self.assertTrue(settings.reasoning_enabled)
        self.assertEqual(settings.limits.max_continuations, 5)
        self.assertEqual(settings.limits.max_continuation_attempts, 7)
        self.assertEqual(settings.limits.max_tool_dispatches, 4)
        self.assertEqual(settings.limits.workspace.max_bytes, 4096)
        self.assertEqual(settings.limits.workspace.goal_chars, 512)
        standard = orchestrator._response_generator("standard").execution_settings
        self.assertEqual(standard.limits.max_inference_steps, config.autonomy["max_tool_steps"])
        self.assertIsNone(standard.limits.context_window_tokens)


if __name__ == "__main__":
    unittest.main()
