"""Run native multi-request reasoning through the real Ollama wire adapter."""
import json
import unittest
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from app.core.events import AssistantSpeechEvent
from app.core.execution_loop import ExecutionRunner
from app.core.execution_state import ExecutionLimits, ExecutionSettings, ExecutionState
from app.core.prompt_budget import PromptBudget, PromptBudgetError, tool_groups
from app.core.response_generator import ResponseGenerator
from app.core.task_workspace import CONTINUE_TOOL, TaskWorkspace, TaskWorkspaceMessage, WorkspaceLimits
from app.integrations import ToolResult
from app.llm.ollama_stream import OllamaClient
from tests.support import BufferedResultMock, consume_generator, make_response


def call(name, arguments, **fields):
    return {**fields, "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def continuation(revision=0, **update):
    return call(CONTINUE_TOOL, {"base_revision": revision, "update": update})


def workspace(payload):
    return next(json.loads(message["content"])["workspace"] for message in payload["messages"]
                if message.get("content", "").startswith('{"kind":"task_workspace"'))


class ReasoningContinuationTests(unittest.TestCase):
    def run_steps(self, steps, *, limits=None, enabled=True, results=(), budget=None, before_inference=None):
        client = OllamaClient("fixture", "http://localhost:11434", thinking_enabled=False,
                              options={"num_ctx": 32768, "num_predict": 1024})
        self.addCleanup(client.close)
        steps = iter(steps)
        payloads, dispatched, events = [], [], []

        def post(*args, **kwargs):
            payload = deepcopy(kwargs["json"])
            if before_inference:
                before_inference(payload, events)
            payloads.append(payload)
            step = next(steps)
            if isinstance(step, Exception):
                raise step
            chunk = step if "message" in step else {"message": step, "done": True, "done_reason": "stop"}
            return make_response(lines=[json.dumps(chunk).encode()])

        result_iter = iter(results)

        def execute(tool_call, **kwargs):
            dispatched.append((tool_call, kwargs))
            yield from ()
            return next(result_iter)

        client.session.post = Mock(side_effect=post)
        writer = Mock()
        generator = ResponseGenerator(client, SimpleNamespace(get_native_tools=lambda **kw: [], execute=execute), writer,
                                      execution_settings=ExecutionSettings(limits=limits or ExecutionLimits(),
                                                                           reasoning_enabled=enabled), prompt_budget=budget)
        state = ExecutionState(generator.execution_settings, normal_think=False)
        messages = [{"role": "user", "content": "Solve the task"}]
        generator._inject_late_routing_system_message(messages)
        runner = ExecutionRunner(generator._stream_late_routing_step, generator._execute_late_tool_call,
                                 generator._force_tool_free_response, writer, generator._render_terminal_response)
        pending = runner.run(state=state, session_id="s", messages=messages, user_text="Solve the task")
        while True:
            try:
                events.append(next(pending))
            except StopIteration as completed:
                return state, payloads, dispatched, writer, events, completed.value, messages

    def test_two_native_continuations_without_thinking_or_external_tools(self):
        first = continuation(next_focus="Check constraints", plan="Compare two cases")
        first.update(content="Hidden preamble")
        first["tool_calls"].append({"function": {"name": "shell__execute", "arguments": {}}})

        def inspect(payload, events):
            self.assertFalse(any(isinstance(event, AssistantSpeechEvent) for event in events))

        state, payloads, dispatched, writer, events, outcome, messages = self.run_steps([
            first, continuation(1, findings=[{"text": "Both cases agree", "evidence_refs": []}],
                                questions=[], next_focus="Compose answer"), {"content": "The answer."},
        ], before_inference=inspect)
        self.assertEqual(outcome.response, "The answer.")
        self.assertEqual(len(payloads), 3)
        self.assertEqual((state.budget.inference_attempts, state.budget.accepted_continuations,
                          state.budget.continuation_attempts, state.budget.tool_dispatches), (3, 2, 2, 0))
        self.assertEqual(dispatched, [])
        writer.assert_not_called()
        self.assertEqual([workspace(payload)["revision"] for payload in payloads], [0, 1, 2])
        self.assertEqual(workspace(payloads[2])["plan"], "Compare two cases")
        for index in (1, 2):
            groups = tool_groups(payloads[index]["messages"])
            selected = payloads[index]["messages"][groups[0][0]]
            self.assertEqual(len(selected["tool_calls"]), 1)
            ack = json.loads(payloads[index]["messages"][groups[-1][-1]]["content"])
            self.assertEqual(ack["status"], "success")
            self.assertEqual(ack["revision"], index)
            self.assertEqual(payloads[index]["messages"][-1]["role"], "tool")
            self.assertFalse(any(message.get("thinking") for message in payloads[index]["messages"]))
        self.assertEqual(sum(isinstance(message, TaskWorkspaceMessage) for message in messages), 1)
        self.assertNotIn(CONTINUE_TOOL, [schema["function"]["name"] for schema in payloads[2].get("tools", [])])

    def test_internal_replay_preserves_native_thinking_and_selected_preamble(self):
        first = continuation(next_focus="Check")
        first.update(content="Hidden preamble", thinking="Native thinking")
        _, payloads, _, _, events, _, _ = self.run_steps([first, {"content": "Final"}])
        selected = next(message for message in payloads[1]["messages"] if message.get("tool_calls"))
        self.assertEqual(selected["content"], "Hidden preamble")
        self.assertEqual(selected["thinking"], "Native thinking")
        self.assertEqual(selected["tool_calls"], first["tool_calls"])
        speech = "".join(event.text for event in events if isinstance(event, AssistantSpeechEvent))
        self.assertEqual(speech, "Final")

    def test_recovery_keeps_workspace_external_evidence_and_latest_internal_ack(self):
        for guarded in (False, True):
            with self.subTest(guarded=guarded):
                internal = continuation(1, plan="Compare source claims", next_focus="Check the contrast", findings=[
                    {"text": "Actual finding", "evidence_refs": ["obs_1"]},
                ])
                internal.update(content="Hidden preamble", thinking="Private continuation")

                def inspect(payload, events):
                    self.assertFalse(any(isinstance(event, AssistantSpeechEvent) for event in events))

                state, payloads, dispatched, writer, _, outcome, _ = self.run_steps([
                    call("web__open", {}), internal, {"content": ""}, {"content": "Recovered"},
                ], results=[ToolResult.success("Actual source evidence")],
                    budget=PromptBudget() if guarded else None, before_inference=inspect)
                self.assertTrue(outcome.recovered)
                recovery = payloads[-1]
                snapshot = workspace(recovery)
                self.assertEqual(snapshot["revision"], 2)
                self.assertEqual(snapshot["plan"], "Compare source claims")
                self.assertEqual(snapshot["next_focus"], "Check the contrast")
                self.assertEqual(snapshot["findings"][0]["evidence_refs"], ["obs_1"])
                notes = next(message for message in recovery["messages"]
                             if message.get("content", "").startswith('{"kind":"task_workspace"'))
                self.assertEqual(notes["role"], "assistant")
                self.assertEqual(json.loads(notes["content"])["trust"], "untrusted_working_data")
                self.assertFalse(any("Compare source claims" in message.get("content", "")
                                     for message in recovery["messages"] if message["role"] == "system"))
                self.assertEqual(len(tool_groups(recovery["messages"])), 2)
                self.assertEqual(recovery["messages"][-1]["tool_name"], CONTINUE_TOOL)
                self.assertIn("Actual source evidence", str(recovery))
                # Compact recovery strips native preamble/thinking but keeps the current notes.
                self.assertNotIn("Hidden preamble", str(recovery))
                self.assertNotIn("Private continuation", str(recovery))
                self.assertNotIn("tools", recovery)
                self.assertEqual(len(payloads), 4)
                self.assertEqual(len(dispatched), 1)
                writer.assert_called_once()
                self.assertEqual(state.budget.recovery_attempts, 1)

    def test_compact_recovery_cannot_silently_drop_a_workspace_that_does_not_fit(self):
        for guarded in (False, True):
            with self.subTest(guarded=guarded):
                client = OllamaClient("fixture", "http://localhost:11434",
                                      options={"num_ctx": 8192, "num_predict": 192})
                self.addCleanup(client.close)
                client.session.post = Mock()
                executor = SimpleNamespace(execute=Mock())
                writer = Mock()
                generator = ResponseGenerator(client, executor, writer,
                                              execution_settings=ExecutionSettings(reasoning_enabled=True),
                                              prompt_budget=PromptBudget(context_window_tokens=8192) if guarded else None)
                notes = TaskWorkspace("Compare sources", WorkspaceLimits())
                notes.apply({"base_revision": 0, "update": {
                    "plan": "p" * 512, "next_focus": "n" * 512,
                    "findings": [{"text": str(index) * 512, "evidence_refs": []} for index in range(3)],
                    "questions": [str(index) * 512 for index in range(4)],
                }})
                current_input = {"role": "user", "content": "Current question " * 60}
                messages = [notes.message(remaining_continuations=1, remaining_inferences=0), current_input]
                # The input/recovery instructions fit; the protected workspace makes it overflow.
                without_notes = generator._build_instant_recovery_messages(messages=[current_input], user_text="Task")
                self.assertLess(PromptBudget().estimate(without_notes, []), 8192 - 2048 - 1024)
                pending = generator._force_tool_free_response(
                    session_id="s", messages=messages, user_text="Task", reason="continuation_failure")
                events = []
                with self.assertRaises(PromptBudgetError):
                    events.extend(pending)
                self.assertFalse(any(isinstance(event, AssistantSpeechEvent) for event in events))
                client.session.post.assert_not_called()
                executor.execute.assert_not_called()
                writer.assert_not_called()

    def test_invalid_updates_are_acknowledged_atomically_without_external_side_effects(self):
        invalid = [[], {"base_revision": 3, "update": {"next_focus": "Next"}},
                   {"base_revision": 0, "update": {"plan": "x" * 513}},
                   {"base_revision": 0, "update": {"next_focus": "x" * 10000}},
                   {"base_revision": 0, "update": {"next_focus": ""}},
                   {"base_revision": 0, "update": {"permissions": ["shell__execute"]}},
                   {"base_revision": 0, "update": {"findings": [{"text": "Fake", "evidence_refs": ["obs_1"]}]}}]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                state, payloads, dispatched, writer, _, outcome, _ = self.run_steps([
                    call(CONTINUE_TOOL, arguments, content="Never speak this"), {"content": "Final"},
                ])
                self.assertEqual(state.workspace.snapshot.revision, 0)
                self.assertEqual(state.budget.accepted_continuations, 0)
                self.assertEqual(state.budget.continuation_attempts, 1)
                self.assertEqual(outcome.response, "Final")
                ack = next(message for message in payloads[1]["messages"] if message["role"] == "tool")
                self.assertEqual(json.loads(ack["content"])["status"], "error")
                self.assertEqual(dispatched, [])
                writer.assert_not_called()

    def test_repeated_stale_and_noop_updates_do_not_count_as_progress(self):
        state, payloads, _, _, _, _, _ = self.run_steps([
            continuation(next_focus="Check A"), continuation(next_focus="Check A"),
            continuation(1, next_focus=" Check A "), {"content": "Final"},
        ], limits=replace(ExecutionLimits(), max_continuation_attempts=4))
        self.assertEqual(state.workspace.snapshot.revision, 1)
        self.assertEqual(state.budget.accepted_continuations, 1)
        self.assertEqual(state.budget.continuation_attempts, 3)
        self.assertEqual(len(payloads), 4)

    def test_invalid_attempt_cap_reserves_exactly_one_terminal_recovery_with_ack(self):
        state, payloads, dispatched, writer, _, outcome, _ = self.run_steps([
            continuation(next_focus=""), continuation(next_focus=""), {"content": "Recovered"},
        ], limits=replace(ExecutionLimits(), max_continuation_attempts=2))
        self.assertEqual(outcome.stop_reason, "continuation_budget_exhausted")
        self.assertEqual((state.budget.inference_attempts, state.budget.recovery_attempts), (2, 1))
        self.assertEqual(len(payloads), 3)
        self.assertNotIn("tools", payloads[-1])
        ack = payloads[-1]["messages"][tool_groups(payloads[-1]["messages"])[-1][-1]]
        self.assertEqual(json.loads(ack["content"])["status"], "error")
        self.assertEqual(dispatched, [])
        writer.assert_not_called()

    def test_accepted_continuation_cap_cannot_be_overridden_by_hallucinated_call(self):
        state, payloads, _, _, _, outcome, _ = self.run_steps([
            continuation(next_focus="Check"), continuation(1, next_focus="Keep looping"), {"content": "Recovered"},
        ], limits=replace(ExecutionLimits(), max_continuations=1))
        self.assertEqual(state.budget.accepted_continuations, 1)
        self.assertEqual(state.workspace.snapshot.next_focus, "Check")
        self.assertEqual(outcome.stop_reason, "continuation_budget_exhausted")
        self.assertNotIn(CONTINUE_TOOL, [tool["function"]["name"] for tool in payloads[1].get("tools", [])])
        self.assertEqual(len(payloads), 3)

    def test_continuation_schema_requires_a_subsequent_normal_inference(self):
        for limit in (1, 3):
            with self.subTest(limit=limit):
                steps = [continuation(index, next_focus=f"Check {index}") for index in range(limit - 1)]
                state, payloads, dispatched, writer, _, outcome, _ = self.run_steps([
                    *steps, {"content": "Final"},
                ], limits=replace(ExecutionLimits(), max_inference_steps=limit,
                                  max_continuations=10, max_continuation_attempts=10))
                exposed = [CONTINUE_TOOL in [tool["function"]["name"] for tool in payload.get("tools", [])]
                           for payload in payloads]
                self.assertEqual(exposed, [True] * (limit - 1) + [False])
                self.assertEqual(len(payloads), limit)
                self.assertEqual(state.budget.inference_attempts, limit)
                self.assertEqual(state.budget.recovery_attempts, 0)
                self.assertFalse(outcome.recovered)
                self.assertEqual(dispatched, [])
                writer.assert_not_called()

    def test_hallucinated_continuation_on_last_inference_still_has_bounded_recovery(self):
        state, payloads, dispatched, writer, events, outcome, _ = self.run_steps([
            continuation(next_focus="Check"), {"content": "Recovered"},
        ], limits=replace(ExecutionLimits(), max_inference_steps=1, max_continuations=10))
        self.assertNotIn(CONTINUE_TOOL, [tool["function"]["name"] for tool in payloads[0].get("tools", [])])
        self.assertEqual(outcome.stop_reason, "late_routing_step_limit")
        self.assertEqual((state.budget.inference_attempts, state.budget.recovery_attempts), (1, 1))
        self.assertEqual(len(payloads), 2)
        self.assertNotIn("tools", payloads[1])
        group, = tool_groups(payloads[1]["messages"])
        self.assertEqual(payloads[1]["messages"][group[0]]["tool_calls"], continuation(next_focus="Check")["tool_calls"])
        self.assertEqual(payloads[1]["messages"][group[-1]]["tool_name"], CONTINUE_TOOL)
        self.assertEqual(json.loads(payloads[1]["messages"][group[-1]]["content"])["status"], "success")
        self.assertEqual("".join(event.text for event in events if isinstance(event, AssistantSpeechEvent)), "Recovered")
        self.assertEqual(dispatched, [])
        writer.assert_not_called()

    def test_length_stop_cannot_commit_a_proposed_workspace_update(self):
        state, payloads, dispatched, _, events, _, _ = self.run_steps([
            {"message": continuation(next_focus="Unfinished", plan="Partial plan"), "done": True, "done_reason": "length"},
            {"content": "Recovered"},
        ])
        self.assertEqual(state.workspace.snapshot.revision, 0)
        self.assertEqual(state.budget.continuation_attempts, 0)
        self.assertNotIn("Partial plan", str(payloads[1]) + str(events))
        self.assertEqual(dispatched, [])
        self.assertEqual(state.generations[0].done_reason, "length")

    def test_disabled_reasoning_exposes_no_workspace_schema_or_extra_inference(self):
        state, payloads, dispatched, writer, _, outcome, _ = self.run_steps([{"content": "Final"}], enabled=False)
        self.assertIsNone(state.workspace)
        self.assertEqual(state.budget.inference_attempts, 1)
        self.assertEqual(len(payloads), 1)
        self.assertNotIn("task_workspace", str(payloads))
        self.assertNotIn(CONTINUE_TOOL, str(payloads))
        self.assertEqual(outcome.response, "Final")
        self.assertEqual(dispatched, [])
        writer.assert_not_called()
        state, payloads, dispatched, writer, _, _, _ = self.run_steps([
            continuation(next_focus="Unauthorized"), {"content": "Final"},
        ], enabled=False)
        self.assertEqual(state.budget.accepted_continuations, 0)
        self.assertIsNone(state.workspace)
        self.assertEqual(dispatched, [])
        self.assertIn("disabled", str(payloads[1]))
        writer.assert_not_called()

    def test_workspace_references_actual_external_observations_and_independent_dispatch_cap(self):
        state, payloads, dispatched, writer, _, outcome, _ = self.run_steps([
            call("web__open", {"target": "https://example.org"}),
            continuation(1, findings=[{"text": "Actual finding", "evidence_refs": ["obs_1"]}], next_focus="Compare"),
            call("web__open", {"target": "https://example.org/other"}), {"content": "Recovered"},
        ], limits=replace(ExecutionLimits(), max_tool_dispatches=1), results=[ToolResult.success("Actual source")])
        self.assertEqual(outcome.stop_reason, "external_tool_budget_exhausted")
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(state.budget.tool_dispatches, 1)
        self.assertEqual(state.budget.accepted_continuations, 1)
        self.assertEqual(state.workspace.snapshot.observations[0].excerpt, "Actual source")
        self.assertEqual(state.workspace.snapshot.revision, 2)
        self.assertEqual(writer.call_count, 2)  # external observations only, including budget rejection
        self.assertNotIn(CONTINUE_TOOL, str(writer.call_args_list))
        self.assertEqual(len(payloads), 4)

    def test_pending_belief_correction_cannot_be_reset_by_internal_continuation(self):
        state, payloads, dispatched, _, _, _, _ = self.run_steps([
            call("beliefs__update", {}), continuation(1, next_focus="Avoid correction"),
            call("beliefs__update", {}), {"content": "Recovered"},
        ], results=[ToolResult.error("First rejection"), ToolResult.error("Second rejection")])
        self.assertEqual(state.belief_attempts, 2)
        self.assertTrue(state.belief_disabled)
        self.assertEqual(state.budget.accepted_continuations, 0)
        self.assertEqual(len(dispatched), 2)
        for payload in payloads[1:3]:
            self.assertFalse(payload["think"])
            self.assertEqual(payload["options"]["num_predict"], 512)
            self.assertNotIn(CONTINUE_TOOL, [tool["function"]["name"] for tool in payload.get("tools", [])])

    def test_internal_continuation_creates_no_external_operation_or_integration_call(self):
        from app.autonomy.store import AutonomyStore
        from app.core.tool_executor import ToolExecutor
        from app.integrations import IntegrationRegistry
        store = AutonomyStore(":memory:")
        self.addCleanup(store.close)
        registry = IntegrationRegistry()
        registry.invoke = Mock(wraps=registry.invoke)
        executor = ToolExecutor(registry, operation_store=store)
        executor.execute = Mock(wraps=executor.execute)
        writer = Mock()
        llm = SimpleNamespace(
            resolve_think_value=lambda override: False,
            generation_options=lambda *_args: {"num_ctx": 32768, "num_predict": 1024},
            chat_buffered_result=BufferedResultMock(side_effect=[continuation(next_focus="Check"), {"content": "Final"},
                                                               {"content": "Second turn"}]),
        )
        generator = ResponseGenerator(llm, executor, writer, execution_settings=ExecutionSettings(reasoning_enabled=True))
        _, response = consume_generator(generator.stream_late_routed_response("s", [], "Task"))
        self.assertEqual(response, "Final")
        consume_generator(generator.stream_late_routed_response("s", [], "Other task"))
        payload = {"messages": llm.chat_buffered_result.call_args.kwargs["messages"]}
        self.assertEqual(workspace(payload)["revision"], 0)
        self.assertEqual(workspace(payload)["goal"], "Other task")
        self.assertIsNone(workspace(payload)["plan"])
        executor.execute.assert_not_called()
        registry.invoke.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(store._conn.execute("SELECT count(*) FROM integration_operations").fetchone()[0], 0)

    def test_no_tool_reply_finishes_immediately_even_when_reasoning_is_enabled(self):
        state, payloads, _, _, _, _, _ = self.run_steps([{"content": "Answer"}])
        self.assertEqual(len(payloads), 1)
        self.assertEqual(state.budget.accepted_continuations, 0)

    def test_workspace_and_web_evidence_compete_with_explicit_evidence_compaction(self):
        notes = TaskWorkspace("Compare sources", WorkspaceLimits())
        notes.observe("web__open", "success", "Actual evidence " * 1000)
        notes.apply({"base_revision": 1, "update": {"plan": "Retain this plan", "findings": [
            {"text": "Actual finding", "evidence_refs": ["obs_1"]},
        ]}})
        ws = notes.message(remaining_continuations=1, remaining_inferences=3)
        evidence = {"kind": "page_excerpt", "excerpt": "Actual evidence " * 1000,
                    "source_id": "source", "final_url": "https://example.org", "links": []}
        messages = [{"role": "user", "content": "Compare sources"},
                    {"role": "assistant", "content": "", "tool_calls": call("web__open", {})["tool_calls"]},
                    {"role": "tool", "tool_name": "web__open", "content": "[success] " + json.dumps(evidence)}, ws]
        budget = PromptBudget(context_window_tokens=4096, completion_reserve_tokens=128, safety_margin_tokens=128)
        prepared = budget.prepare(messages, [], {"num_ctx": 4096, "num_predict": 128})
        self.assertIn(ws, prepared.messages)
        self.assertEqual(len(tool_groups(prepared.messages)), 1)
        retained = next(message for message in prepared.messages if message["role"] == "tool")
        self.assertTrue(json.loads(retained["content"][len("[success] "):])["context_budget_truncated"])
        self.assertLessEqual(budget.estimate(prepared.messages, []), 4096 - 256)

    def test_workspace_and_latest_ack_are_mandatory_under_context_pressure(self):
        ws = TaskWorkspaceMessage(role="assistant", content='{"kind":"task_workspace","notes":"' + "x" * 1000 + '"}')
        pair = [{"role": "assistant", "content": "", "tool_calls": continuation(next_focus="Check")["tool_calls"]},
                {"role": "tool", "tool_name": CONTINUE_TOOL, "content": "ack"}]
        budget = PromptBudget(context_window_tokens=4096, completion_reserve_tokens=128, safety_margin_tokens=128)
        # No user-role input (event turns): workspace must not be mistaken for removable history.
        prepared = budget.prepare([{"role": "assistant", "content": "old" * 4000}, *pair, ws], [], {"num_ctx": 4096, "num_predict": 128})
        self.assertIn(ws, prepared.messages)
        self.assertEqual(len(tool_groups(prepared.messages)), 1)
        with self.assertRaises(PromptBudgetError):
            budget.prepare([*pair, TaskWorkspaceMessage(role="assistant", content="x" * 5000)], [],
                           {"num_ctx": 4096, "num_predict": 128})
