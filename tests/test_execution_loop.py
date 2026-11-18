"""Execution transitions preserve native routing, authority and bounded recovery."""

import unittest
from copy import deepcopy
from unittest.mock import Mock

from app.core.events import AssistantThinkingEvent
from app.core.execution_loop import ExecutionRunner
from app.core.execution_state import (
    ExecutionBudget, ExecutionLimits, ExecutionPhase, ExecutionProfile,
    ExecutionProfileOverrides, ExecutionSettings, ExecutionState, InferencePhase,
)
from app.core.generation_step import AssistantContinuation, FinalText, parse_tool_call
from app.core.prompt_budget import PromptBudgetError, tool_groups
from app.integrations import CapabilityId, EventId, IntegrationEvent, ToolResult
from app.llm.base import InferenceFailure
from tests.support import consume_generator


def native_call(name="demo__run", arguments=None):
    return parse_tool_call({"function": {"name": name, "arguments": {} if arguments is None else arguments}},
                           continuation=AssistantContinuation("Checking.", "Native thought"))


class ExecutionLoopTests(unittest.TestCase):
    def fixture(self, steps, results=(), *, limit=5, reasoning=False, allowed=None):
        state = ExecutionState(
            ExecutionSettings(limits=ExecutionLimits(max_inference_steps=limit), reasoning_enabled=reasoning),
            normal_think=True, allowed_capabilities=allowed,
        )
        steps, results = iter(steps), iter(results)
        calls = []

        def infer(**kwargs):
            calls.append(("infer", state.phase, kwargs, deepcopy(kwargs["messages"])))
            yield AssistantThinkingEvent(text="infer")
            step = next(steps)
            if isinstance(step, Exception):
                raise step
            return step

        def execute(**kwargs):
            calls.append(("tool", state.phase, kwargs, None))
            yield AssistantThinkingEvent(text="tool")
            return next(results)

        def recover(**kwargs):
            calls.append(("recover", state.phase, kwargs, deepcopy(kwargs["messages"])))
            yield AssistantThinkingEvent(text="recover")
            return FinalText("Recovered.")

        writer = Mock()
        def render(**kwargs):
            yield from ()
            return kwargs["response"]

        runner = ExecutionRunner(infer, execute, recover, writer, render)
        messages = [{"role": "user", "content": "Question"}]
        return state, runner, calls, messages, writer

    def run_fixture(self, fixture, **kwargs):
        state, runner, _, messages, _ = fixture
        return consume_generator(runner.run(state=state, session_id="s", messages=messages,
                                            user_text="Question", **kwargs))

    def test_direct_final_uses_one_inference_and_no_recovery(self):
        fixture = self.fixture([FinalText("Answer")])
        events, result = self.run_fixture(fixture)
        state, _, calls, _, writer = fixture
        self.assertEqual(result.response, "Answer")
        self.assertEqual(result.stop_reason, "final_text")
        self.assertEqual(state.stop_reason, result.stop_reason)
        self.assertFalse(result.recovered)
        self.assertEqual(state.phase, ExecutionPhase.COMPLETED)
        self.assertEqual([call[0] for call in calls], ["infer"])
        self.assertEqual([event.text for event in events], ["infer"])
        self.assertEqual(state.budget.inference_attempts, 1)
        self.assertEqual(state.budget.recovery_attempts, 0)
        writer.assert_not_called()

    def test_tool_continuation_preserves_fields_authority_callbacks_and_order(self):
        allowed = {CapabilityId("demo", "run")}
        fixture = self.fixture([native_call(), FinalText("Done")], [ToolResult.success("Observed")], allowed=allowed)
        allowed.clear()
        authority, prepared, approval = object(), object(), Mock()
        events, _ = self.run_fixture(fixture, authoritative_turn=authority,
                                     prepared_belief_turn=prepared, tool_approval_callback=approval)
        state, _, calls, _, writer = fixture
        self.assertEqual([entry[:2] for entry in calls], [
            ("infer", ExecutionPhase.INFER), ("tool", ExecutionPhase.EXECUTE_TOOL),
            ("infer", ExecutionPhase.INFER),
        ])
        self.assertEqual([event.text for event in events], ["infer", "tool", "infer"])
        for call in calls:
            self.assertEqual(call[2]["allowed_capabilities"], frozenset({CapabilityId("demo", "run")}))
            self.assertIs(call[2]["authoritative_turn"], authority)
            self.assertIs(call[2]["prepared_belief_turn"], prepared)
        self.assertIs(calls[1][2]["tool_approval_callback"], approval)
        replay = calls[2][3][-2:]
        self.assertEqual(replay[0]["content"], "Checking.")
        self.assertEqual(replay[0]["thinking"], "Native thought")
        self.assertEqual(replay[1], {"role": "tool", "tool_name": "demo__run", "content": "[success] Observed"})
        self.assertEqual(len(tool_groups(replay)), 1)
        self.assertEqual(calls[2][2]["inference_phase"], InferencePhase.CONTINUATION)
        self.assertEqual(state.budget.tool_dispatches, 1)
        writer.assert_called_once()

    def test_invalid_native_arguments_are_observed_without_dispatch(self):
        fixture = self.fixture([native_call(arguments=[]), FinalText("Clarify")])
        self.run_fixture(fixture)
        state, _, calls, messages, _ = fixture
        self.assertEqual([call[0] for call in calls], ["infer", "infer"])
        self.assertIn("Tool arguments must be an object", messages[-1]["content"])
        self.assertEqual(state.budget.tool_dispatches, 0)
        self.assertEqual(state.budget.tool_interactions, 1)

    def test_belief_rejections_keep_one_correction_and_one_recovery(self):
        fixture = self.fixture([native_call("beliefs__update"), native_call("beliefs__update")],
                               [ToolResult.error("first"), ToolResult.error("second")])
        _, outcome = self.run_fixture(fixture)
        state, _, calls, messages, _ = fixture
        self.assertEqual([call[0] for call in calls], ["infer", "tool", "infer", "tool", "recover"])
        self.assertEqual(calls[2][2]["inference_phase"], InferencePhase.CORRECTION)
        self.assertEqual(calls[-1][2]["reason"], "belief_attempt_limit_reached")
        self.assertEqual(calls[-1][2]["react_iteration"], 3)
        self.assertEqual(outcome.stop_reason, "belief_attempt_limit_reached")
        self.assertEqual(state.belief_attempts, 2)
        self.assertTrue(state.belief_disabled)
        self.assertEqual(state.budget.recovery_attempts, 1)
        self.assertEqual(len(tool_groups(messages)), 2)

    def test_suppressed_capability_cannot_be_reenabled_by_the_model(self):
        fixture = self.fixture([native_call(), native_call(), FinalText("Unavailable")],
                               [ToolResult.error("offline", diagnostics={"suppress_for_turn": True})])
        self.run_fixture(fixture)
        state, _, calls, messages, _ = fixture
        self.assertEqual([call[0] for call in calls].count("tool"), 1)
        self.assertIn(CapabilityId("demo", "run"), calls[2][2]["excluded_capabilities"])
        self.assertIn("temporarily unavailable", messages[-1]["content"])
        self.assertEqual(state.budget.tool_interactions, 2)

    def test_exhaustion_has_exactly_existing_work_and_one_recovery_for_both_reasoning_settings(self):
        for enabled in (False, True):
            with self.subTest(reasoning=enabled):
                fixture = self.fixture([native_call(), native_call()],
                                       [ToolResult.success("one"), ToolResult.success("two")],
                                       limit=2, reasoning=enabled)
                _, outcome = self.run_fixture(fixture)
                state, _, calls, messages, _ = fixture
                self.assertEqual([call[0] for call in calls], ["infer", "tool", "infer", "tool", "recover"])
                self.assertTrue(all(call[2]["normal_think"] is True for call in calls if call[0] == "infer"))
                self.assertEqual(outcome.stop_reason, "late_routing_step_limit")
                self.assertEqual(calls[-1][2]["react_iteration"], 3)
                self.assertTrue(any("MAX TOOL STEPS REACHED" in message.get("content", "") for message in messages))
                self.assertEqual(state.budget.inference_attempts, 2)
                self.assertEqual(state.budget.recovery_attempts, 1)

    def test_failure_and_empty_output_preserve_recovery_reasons(self):
        for step, reason in ((InferenceFailure("read_timeout", "timeout"), "initial_failure"),
                             (FinalText(""), "initial_empty_no_tool_generation")):
            with self.subTest(reason=reason):
                fixture = self.fixture([step])
                _, outcome = self.run_fixture(fixture)
                self.assertEqual(outcome.stop_reason, reason)
                self.assertTrue(outcome.recovered)
                self.assertEqual([call[0] for call in fixture[2]], ["infer", "recover"])

    def test_required_context_failure_does_not_spend_recovery(self):
        fixture = self.fixture([PromptBudgetError("mandatory input too large")])
        with self.assertRaises(PromptBudgetError):
            self.run_fixture(fixture)
        self.assertEqual(fixture[0].phase, ExecutionPhase.FAILED)
        self.assertEqual([call[0] for call in fixture[2]], ["infer"])

    def test_generator_close_marks_cancelled_without_recovery_or_further_yields(self):
        state, runner, calls, messages, _ = self.fixture([FinalText("Answer")])
        iterator = runner.run(state=state, session_id="s", messages=messages, user_text="Question")
        next(iterator)
        iterator.close()
        self.assertEqual(state.phase, ExecutionPhase.CANCELLED)
        self.assertEqual([call[0] for call in calls], ["infer"])

    def test_generator_close_during_tool_or_recovery_does_not_resume_work(self):
        for step, results, expected in ((native_call(), [ToolResult.success("result")], ExecutionPhase.EXECUTE_TOOL),
                                        (FinalText(""), [], ExecutionPhase.RECOVER)):
            with self.subTest(phase=expected):
                state, runner, calls, messages, _ = self.fixture([step], results)
                iterator = runner.run(state=state, session_id="s", messages=messages, user_text="Question")
                next(iterator)
                next(iterator)
                self.assertEqual(state.phase, expected)
                iterator.close()
                self.assertEqual(state.phase, ExecutionPhase.CANCELLED)
                self.assertEqual(len(calls), 2)
                self.assertEqual(state.budget.inference_attempts, 1)

    def test_recovery_exception_terminates_without_another_inference(self):
        fixture = self.fixture([FinalText("")])
        state, runner, calls, _, _ = fixture

        def failed_recovery(**kwargs):
            yield from ()
            raise PromptBudgetError("recovery required context too large")

        runner.recover_response = failed_recovery
        with self.assertRaises(PromptBudgetError):
            self.run_fixture(fixture)
        self.assertEqual(state.phase, ExecutionPhase.FAILED)
        self.assertEqual(state.budget.recovery_attempts, 1)
        self.assertEqual([call[0] for call in calls], ["infer"])

    def test_event_missing_authority_is_closed_and_traces_stay_internal(self):
        fixture = self.fixture([native_call(), FinalText("Done")], [ToolResult.denied("no authority")])
        event = IntegrationEvent(EventId("demo", "wake"), {}, session_id="s")
        self.run_fixture(fixture, event=event, persist_tool_traces=False)
        for call in fixture[2]:
            self.assertEqual(call[2]["allowed_capabilities"], frozenset())
        fixture[4].assert_not_called()

    def test_terminal_state_cannot_be_reused_or_transition_back_to_work(self):
        fixture = self.fixture([FinalText("Done")])
        self.run_fixture(fixture)
        with self.assertRaisesRegex(RuntimeError, "fresh execution state"):
            self.run_fixture(fixture)
        with self.assertRaisesRegex(RuntimeError, "Invalid execution transition"):
            fixture[0].transition(ExecutionPhase.INFER)

    def test_budget_rejects_recursive_recovery_and_invalid_limits(self):
        budget = ExecutionBudget(ExecutionLimits(max_inference_steps=1))
        self.assertTrue(budget.admit_inference())
        self.assertFalse(budget.admit_inference())
        budget.admit_recovery()
        with self.assertRaises(RuntimeError):
            budget.admit_recovery()
        for settings in ({"max_inference_steps": 0}, {"max_inference_steps": None},
                         {"num_predict": True}, {"recovery_deadline_s": float("inf")},
                         {"max_continuations": -1}, {"max_continuations": True},
                         {"max_continuation_attempts": 0}, {"max_tool_dispatches": 0}, {"workspace": {}}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                ExecutionLimits(**settings)

    def test_profile_resolution_inherits_without_changing_the_base(self):
        base = ExecutionLimits(max_inference_steps=8)
        override = ExecutionProfileOverrides(max_inference_steps=12, context_window_tokens=16384)
        deep = ExecutionSettings(ExecutionProfile.DEEP, override.resolve(base))
        self.assertEqual(deep.limits.max_inference_steps, 12)
        self.assertEqual(deep.limits.context_window_tokens, 16384)
        self.assertEqual(deep.limits.recovery_num_predict, base.recovery_num_predict)
        self.assertEqual(base.max_inference_steps, 8)


if __name__ == "__main__":
    unittest.main()
