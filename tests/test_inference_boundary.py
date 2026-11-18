"""Atomic native classification and terminal-only output, including recovery."""
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from app.core.events import AssistantSpeechEvent, AssistantThinkingEvent
from app.core.execution_loop import ExecutionRunner
from app.core.execution_state import ExecutionPhase, ExecutionSettings, ExecutionState
from app.core.generation_step import FinalText, IncompleteGeneration
from app.core.response_generator import ResponseGenerator, SAFE_EMPTY_RESPONSE_FALLBACK
from app.llm.ollama_stream import OllamaClient
from tests.support import buffered_generation, consume_generator, make_response


class InferenceBoundaryTests(unittest.TestCase):
    def fixture(self, messages):
        client = OllamaClient("fixture", "http://localhost:11434")
        self.addCleanup(client.close)
        responses = iter(make_response(lines=[json.dumps(item).encode()]) for item in messages)
        payloads = []

        def post(*args, **kwargs):
            payloads.append(deepcopy(kwargs["json"]))
            return next(responses)

        client.session.post = Mock(side_effect=post)
        executor = SimpleNamespace(get_native_tools=lambda **kwargs: [], execute=Mock())
        writer = Mock()
        return ResponseGenerator(client, executor, writer), payloads

    def test_classification_emits_no_terminal_output_and_exposes_metadata(self):
        generator, payloads = self.fixture([{
            "message": {"content": "Answer"}, "done": True, "done_reason": "stop",
            "prompt_eval_count": 12, "eval_count": 3,
        }])
        events, result = consume_generator(generator._stream_late_routing_step("s", [], "Question"))
        self.assertIsInstance(result, FinalText)
        self.assertFalse(any(isinstance(e, AssistantSpeechEvent) for e in events))
        self.assertEqual((result.generation.done_reason, result.generation.prompt_tokens,
                          result.generation.generated_tokens), ("stop", 12, 3))
        self.assertEqual(len(payloads), 1)

    def test_truncated_tool_or_text_is_never_executed_replayed_or_rendered(self):
        for tool in (False, True):
            with self.subTest(tool=tool):
                message = {"content": "Unsafe partial", "thinking": "Partial thought"}
                if tool:
                    message["tool_calls"] = [{"function": {"name": "shell__execute", "arguments": {"command": "pwd"}}}]
                generator, payloads = self.fixture([
                    {"message": message, "done": True, "done_reason": "length"},
                    {"message": {"content": "Recovered."}, "done": True, "done_reason": "stop"},
                ])
                events, response = consume_generator(generator.stream_late_routed_response("s", [], "Question"))
                self.assertEqual(response, "Recovered.")
                self.assertEqual(len(payloads), 2)
                self.assertNotIn("Unsafe partial", str(events) + str(payloads[1]))
                self.assertFalse(any(isinstance(e, AssistantThinkingEvent) for e in events))
                generator.tool_executor.execute.assert_not_called()
                generator.record_tool_trace.assert_not_called()

    def test_truncated_recovery_has_no_partial_output_and_cannot_recurse(self):
        generator, payloads = self.fixture([
            {"message": {"content": ""}, "done": True},
            {"message": {"content": "Unsafe recovery"}, "done": True, "done_reason": "length"},
        ])
        events, response = consume_generator(generator.stream_late_routed_response("s", [], "Question"))
        self.assertEqual(response, SAFE_EMPTY_RESPONSE_FALLBACK)
        self.assertEqual(len(payloads), 2)
        self.assertFalse(any(isinstance(e, AssistantSpeechEvent) for e in events))

    def test_fsm_accepts_terminal_before_render_and_retains_usage_on_truncation(self):
        partial = buffered_generation({"content": "partial"}, reason="length", generated_tokens=20)
        state = ExecutionState(ExecutionSettings(), normal_think=False)
        calls = []

        def infer(**kwargs):
            yield from ()
            return IncompleteGeneration(partial)

        def render(**kwargs):
            raise AssertionError("Truncated inference must not render")
            yield

        def recover(**kwargs):
            calls.append(state.phase)
            yield from ()
            return FinalText("Recovered")

        _, outcome = consume_generator(ExecutionRunner(infer, Mock(), recover, Mock(), render).run(
            state=state, session_id="s", messages=[], user_text="Question"))
        self.assertEqual(outcome.response, "Recovered")
        self.assertEqual(state.generations, [partial])
        self.assertEqual(calls, [ExecutionPhase.RECOVER])
        state = ExecutionState(ExecutionSettings(), normal_think=False)

        def infer_final(**kwargs):
            yield from ()
            return FinalText("Answer")

        def render_final(**kwargs):
            self.assertEqual(state.phase, ExecutionPhase.FINALIZE)
            yield from ()
            return kwargs["response"]

        consume_generator(ExecutionRunner(infer_final, Mock(), recover, Mock(), render_final).run(
            state=state, session_id="s", messages=[], user_text="Question"))
