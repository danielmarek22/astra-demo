"""The single native execution FSM; presentation and persistence remain outside."""

import json
import logging
from collections.abc import Callable, Generator
from typing import cast

from app.core.conversation import InputSource, SenderAttribution, SenderType
from app.core.events import TurnEvent
from app.core.execution_state import ExecutionOutcome, ExecutionPhase, ExecutionState, InferencePhase
from app.core.generation_step import AcceptedToolCall, FinalText, GenerationStepResult, IncompleteGeneration, InvalidToolCall
from app.core.prompt_budget import PromptBudgetError
from app.core.task_workspace import (
    CONTINUE_TOOL, WORKSPACE_POLICY, TaskWorkspace, TaskWorkspaceMessage, WorkspaceUpdateError,
)
from app.integrations import CapabilityId, IntegrationEvent, NotificationRequest, ToolResult
from app.llm.base import InferenceFailure
from app.logging import trace_event
from app.tools.web.answer_sources import RetrievedSources

logger = logging.getLogger("orchestrator")
_BELIEF_CAPABILITY = CapabilityId("beliefs", "update")
InferStep = Callable[..., Generator[TurnEvent, None, GenerationStepResult]]
ExecuteTool = Callable[..., Generator[TurnEvent, None, ToolResult]]
RecoverResponse = Callable[..., Generator[TurnEvent, None, FinalText]]


def inject_hidden_system_message(messages: list[dict], content: str) -> None:
    payload = {
        "role": "system",
        "content": content,
    }
    if messages and messages[0].get("role") == "system":
        messages.insert(1, payload)
    else:
        messages.insert(0, payload)


class ExecutionRunner:
    """Control native inference, tool observations and one bounded recovery.

    Callbacks own inference/rendering, authorized dispatch, and recovery output.
    The runner has no history store, model transport, or application resource owner.
    """

    def __init__(self, infer_step: InferStep, execute_tool: ExecuteTool,
                 recover_response: RecoverResponse, record_tool_trace: Callable[..., object],
                 render_response: Callable[..., Generator[TurnEvent, None, str]]) -> None:
        self.infer_step = infer_step
        self.execute_tool = execute_tool
        self.recover_response = recover_response
        self.record_tool_trace = record_tool_trace
        self.render_response = render_response

    def run(
        self, *, state: ExecutionState, session_id: str, messages: list[dict], user_text: str,
        tool_approval_callback: Callable[[dict], bool] | None = None,
        event: IntegrationEvent | None = None,
        notification_callback: Callable[[NotificationRequest], bool] | None = None,
        persist_tool_traces: bool = True, authoritative_turn: object | None = None,
        prepared_belief_turn: object | None = None,
        retrieved_sources: RetrievedSources | None = None,
    ) -> Generator[TurnEvent, None, ExecutionOutcome]:
        initial_phase = state.phase
        if initial_phase is not ExecutionPhase.INFER or state.budget.inference_attempts:
            raise RuntimeError("ExecutionRunner requires a fresh execution state")
        # Authority is snapshotted by the caller before prompt preparation.
        execution_capabilities = (
            state.allowed_capabilities if state.allowed_capabilities is not None
            else frozenset() if event is not None else None
        )
        if state.settings.reasoning_enabled:
            state.workspace = TaskWorkspace(user_text, state.settings.limits.workspace)
            inject_hidden_system_message(messages, WORKSPACE_POLICY)
        try:
            while state.phase is not ExecutionPhase.COMPLETED:
                if state.phase is ExecutionPhase.INFER:
                    if not state.budget.admit_inference():
                        self._exhausted(session_id, messages)
                        state.recover("late_routing_step_limit", state.settings.limits.max_inference_steps + 1)
                        continue
                    self._sync_workspace(state, messages)
                    step = state.budget.inference_attempts
                    logger.info("[%s] Late routing step %d/%d", session_id, step,
                                state.settings.limits.max_inference_steps)
                    inference_phase = state.inference_phase
                    try:
                        result = yield from self.infer_step(
                            session_id=session_id, messages=messages, user_text=user_text,
                            allowed_capabilities=execution_capabilities,
                            authoritative_turn=authoritative_turn, prepared_belief_turn=prepared_belief_turn,
                            excluded_capabilities=(frozenset(state.suppressed_capabilities)
                                | (frozenset({_BELIEF_CAPABILITY}) if state.belief_disabled else frozenset())),
                            inference_phase=inference_phase, normal_think=state.normal_think,
                            react_iteration=step,
                            internal_tools=self._internal_tools(state),
                        )
                    except PromptBudgetError:
                        raise
                    except (InferenceFailure, TimeoutError, ValueError) as exc:
                        category = getattr(exc, "category", type(exc).__name__)
                        logger.warning("[%s] Late-routing inference failed phase=%s category=%s; recovering tool-free",
                                       session_id, inference_phase.value, category)
                        trace_event("orchestrator", "late_routing_inference_failure", session_id=session_id,
                                    payload={"phase": inference_phase.value, "category": category})
                        state.recover(f"{inference_phase.value}_failure", step)
                        continue
                    if result.generation is not None:
                        state.generations.append(result.generation)
                    if isinstance(result, IncompleteGeneration):
                        state.recover("truncated_generation", step)
                        continue
                    if isinstance(result, FinalText):
                        state.transition(ExecutionPhase.FINALIZE)
                        response = yield from self.render_response(
                            session_id=session_id, messages=messages, response=result.response,
                        )
                        if response and response.strip():
                            state.stop_reason = "final_text"
                            state.outcome = ExecutionOutcome(response, "final_text")
                            state.transition(ExecutionPhase.COMPLETED)
                        else:
                            state.recover("empty_after_tool_interaction" if state.budget.tool_interactions
                                          else "initial_empty_no_tool_generation")
                    else:
                        state.pending_call = result
                        state.transition(ExecutionPhase.CONTINUE if self._is_internal(result)
                                         else ExecutionPhase.EXECUTE_TOOL)
                elif state.phase is ExecutionPhase.CONTINUE:
                    if state.pending_call is None:
                        raise RuntimeError("Continuation requires a selected native call")
                    self._continue(state, messages, state.pending_call)
                    state.pending_call = None
                elif state.phase is ExecutionPhase.EXECUTE_TOOL:
                    pending_call = state.pending_call
                    if pending_call is None:
                        raise RuntimeError("Tool execution requires a selected native call")
                    yield from self._observe_tool(
                        state=state, result=pending_call, session_id=session_id, messages=messages, user_text=user_text,
                        tool_approval_callback=tool_approval_callback, execution_capabilities=execution_capabilities,
                        event=event, notification_callback=notification_callback,
                        authoritative_turn=authoritative_turn, prepared_belief_turn=prepared_belief_turn,
                        persist_tool_traces=persist_tool_traces, retrieved_sources=retrieved_sources,
                    )
                    state.pending_call = None
                elif state.phase is ExecutionPhase.RECOVER:
                    if state.stop_reason is None:
                        raise RuntimeError("Recovery requires a stop reason")
                    self._sync_workspace(state, messages)
                    state.budget.admit_recovery()
                    recovery = yield from self.recover_response(
                        session_id=session_id, messages=messages, user_text=user_text,
                        reason=state.stop_reason, react_iteration=state.recovery_iteration,
                    )
                    if recovery.generation is not None:
                        state.generations.append(recovery.generation)
                    state.outcome = ExecutionOutcome(recovery.response, state.stop_reason, recovered=True)
                    state.transition(ExecutionPhase.COMPLETED)
                else:
                    raise RuntimeError(f"Cannot run execution in phase {state.phase.value}")
        except GeneratorExit:
            state.transition(ExecutionPhase.CANCELLED)
            raise
        except Exception:
            state.transition(ExecutionPhase.FAILED)
            raise
        if state.outcome is None:
            raise RuntimeError("Completed execution has no outcome")
        trace_event("orchestrator", "execution_completed", session_id=session_id, payload={
            "profile": state.settings.profile.value, "reasoning_enabled": state.settings.reasoning_enabled,
            "stop_reason": state.outcome.stop_reason, "inference_attempts": state.budget.inference_attempts,
            "recovery_attempts": state.budget.recovery_attempts,
            "continuation_attempts": state.budget.continuation_attempts,
            "accepted_continuations": state.budget.accepted_continuations,
            "external_tool_dispatches": state.budget.tool_dispatches,
        })
        return state.outcome

    @staticmethod
    def _is_internal(result: AcceptedToolCall | InvalidToolCall) -> bool:
        return isinstance(result.tool_name, str) and result.tool_name.startswith("reasoning__")

    @staticmethod
    def _sync_workspace(state: ExecutionState, messages: list[dict]) -> None:
        if state.workspace is None:
            return
        messages[:] = [message for message in messages if not isinstance(message, TaskWorkspaceMessage)]
        # Keep native replay ending in its matching tool acknowledgment. Notes
        # are contextual assistant data before the current input, not a trailing
        # assistant message that a backend template could treat as a prefill.
        current_input = max((index for index, message in enumerate(messages) if message.get("role") == "user"),
                            default=next((index for index, message in enumerate(messages)
                                          if message.get("role") != "system"), len(messages)))
        messages.insert(current_input, state.workspace.message(
            remaining_continuations=max(0, state.settings.limits.max_continuations - state.budget.accepted_continuations),
            remaining_inferences=max(0, state.settings.limits.max_inference_steps - state.budget.inference_attempts),
        ))

    @staticmethod
    def _internal_tools(state: ExecutionState) -> list[dict]:
        limits, budget = state.settings.limits, state.budget
        # Admission already counts the current request. Continuation needs a
        # subsequent normal request, not just the reserved terminal recovery.
        if (state.workspace is None or state.inference_phase is InferencePhase.CORRECTION
                or budget.inference_attempts >= limits.max_inference_steps
                or budget.accepted_continuations >= limits.max_continuations
                or budget.continuation_attempts >= limits.max_continuation_attempts):
            return []
        return [state.workspace.native_tool()]

    @staticmethod
    def _continue(state: ExecutionState, messages: list[dict], result: AcceptedToolCall | InvalidToolCall) -> None:
        budget, limits = state.budget, state.settings.limits
        budget.continuation_attempts += 1
        exhausted = (budget.continuation_attempts >= limits.max_continuation_attempts
                     or budget.accepted_continuations >= limits.max_continuations)
        error = None
        try:
            if not state.settings.reasoning_enabled or state.workspace is None:
                raise WorkspaceUpdateError("Internal reasoning is disabled")
            if result.tool_name != CONTINUE_TOOL:
                raise WorkspaceUpdateError("Unknown internal reasoning capability")
            if state.inference_phase is InferencePhase.CORRECTION:
                raise WorkspaceUpdateError("Complete the pending belief correction or answer directly")
            if (budget.continuation_attempts > limits.max_continuation_attempts
                    or budget.accepted_continuations >= limits.max_continuations):
                raise WorkspaceUpdateError("Continuation budget exhausted; finish using current evidence")
            if isinstance(result, InvalidToolCall):
                raise WorkspaceUpdateError("Malformed native continuation arguments")
            state.workspace.apply(result.tool_arguments)
            budget.accepted_continuations += 1
        except WorkspaceUpdateError as exc:
            error = str(exc)
        # Replay only the selected call, paired even when its update was rejected.
        # Internal acks never enter the external executor, trace store or journal.
        messages.extend([
            {"role": "assistant", **result.continuation.message_fields(), "tool_calls": [{"function": {
                "name": result.tool_name, "arguments": result.tool_arguments,
            }}]},
            {"role": "tool", "tool_name": result.tool_name, "content": json.dumps({
                "status": "error" if error else "success", "error": error,
                "revision": state.workspace.snapshot.revision if state.workspace else None,
                "accepted_continuations": budget.accepted_continuations,
            }, separators=(",", ":"))},
        ])
        if error and exhausted:
            state.recover("continuation_budget_exhausted", budget.inference_attempts + 1)
        else:
            if state.inference_phase is not InferencePhase.CORRECTION:
                state.inference_phase = InferencePhase.CONTINUATION
            state.transition(ExecutionPhase.INFER)

    def _observe_tool(
        self, *, state: ExecutionState, result: AcceptedToolCall | InvalidToolCall,
        session_id: str, messages: list[dict], user_text: str,
        tool_approval_callback: Callable[[dict], bool] | None,
        execution_capabilities: frozenset[CapabilityId] | None, event: IntegrationEvent | None,
        notification_callback: Callable[[NotificationRequest], bool] | None,
        authoritative_turn: object | None, prepared_belief_turn: object | None,
        persist_tool_traces: bool, retrieved_sources: RetrievedSources | None,
    ) -> Generator[TurnEvent, None, None]:
        tool_name = result.tool_name
        tool_arguments = result.tool_arguments
        is_belief_call = tool_name == str(_BELIEF_CAPABILITY)
        if is_belief_call:
            state.belief_attempts += 1

        messages.append({
            "role": "assistant",
            **result.continuation.message_fields(),
            "tool_calls": [{
                "function": {
                    "name": tool_name,
                    "arguments": tool_arguments,
                }
            }]
        })

        dispatch_exhausted = False
        if is_belief_call and state.belief_disabled:
            tool_result = ToolResult.error(
                "Belief update was not applied. Do not call beliefs__update again for "
                "this participant message. Continue with a normal response.",
                diagnostics={
                    "category": "attempt_limit",
                    "error_code": "BELIEF_ATTEMPT_LIMIT",
                    "repository_accessed": False,
                },
            )
        elif isinstance(result, AcceptedToolCall) and result.call.capability in state.suppressed_capabilities:
            tool_result = ToolResult.error(
                f"{tool_name} is temporarily unavailable for the rest of this turn. Do not retry it; "
                "explain the missing evidence or use another authorized source.",
                diagnostics={"category": "temporarily_suppressed"},
            )
        elif isinstance(result, InvalidToolCall):
            tool_result = ToolResult.error(result.error)
        elif (state.settings.limits.max_tool_dispatches is not None
              and state.budget.tool_dispatches >= state.settings.limits.max_tool_dispatches):
            dispatch_exhausted = True
            tool_result = ToolResult.error("External tool dispatch budget exhausted; answer using retained evidence")
        else:
            state.budget.tool_dispatches += 1
            tool_result = yield from self.execute_tool(
                session_id=session_id,
                call=result.call,
                user_text=user_text,
                tool_approval_callback=tool_approval_callback,
                allowed_capabilities=execution_capabilities,
                event=event,
                notification_callback=notification_callback,
                authoritative_turn=authoritative_turn,
                prepared_belief_turn=prepared_belief_turn,
            )
            if state.workspace is not None:
                state.workspace.observe(str(tool_name), tool_result.status.value, tool_result.content)
        state.budget.tool_interactions += 1
        if retrieved_sources is not None:
            retrieved_sources.observe(tool_name, tool_result.content,  # type: ignore[no-untyped-call]
                                       succeeded=tool_result.status.value == "success")
        if (isinstance(result, AcceptedToolCall)
                and (tool_result.diagnostics or {}).get("suppress_for_turn") is True):
            state.suppressed_capabilities.add(result.call.capability)
            inject_hidden_system_message(messages,
                f"{tool_name} is unavailable for the remainder of this turn after backend/provider failures. "
                "Do not call it again. Report missing evidence honestly or use other authorized capabilities. "
                "An empty search result is not evidence that any page was read; do not invent source URLs.")
        observation = f"[{tool_result.status.value}] {tool_result.content}"

        if is_belief_call and tool_result.status.value == "error":
            diagnostics = dict(tool_result.diagnostics or {})
            trace_event(
                "orchestrator",
                "belief_update_rejected",
                session_id=session_id,
                payload={
                    "capability": tool_name,
                    "error_code": diagnostics.get("error_code", "UNCLASSIFIED_REJECTION"),
                    "category": diagnostics.get("category", "unclassified_rejection"),
                    "reason": tool_result.content[:700],
                    "argument_summary": self._bounded_belief_argument_summary(tool_arguments),
                    "repository_accessed": diagnostics.get("repository_accessed"),
                    "react_attempt": state.belief_attempts,
                },
            )
            logger.warning(
                "[%s] beliefs__update rejected attempt=%d code=%s category=%s "
                "repository_accessed=%s reason=%s arguments=%s",
                session_id,
                state.belief_attempts,
                diagnostics.get("error_code", "UNCLASSIFIED_REJECTION"),
                diagnostics.get("category", "unclassified_rejection"),
                diagnostics.get("repository_accessed"),
                tool_result.content[:700],
                json.dumps(
                    self._bounded_belief_argument_summary(tool_arguments),
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            )
            if state.belief_attempts >= 2:
                state.belief_disabled = True
                observation += (
                    "\n\nBelief update was not applied. Do not call beliefs__update "
                    "again for this participant message. Continue with a normal response."
                )
            else:
                state.inference_phase = InferencePhase.CORRECTION
        elif is_belief_call:
            state.belief_disabled = True
            state.inference_phase = InferencePhase.CONTINUATION
        else:
            state.inference_phase = InferencePhase.CONTINUATION

        safe_observation = observation[:1024] + ("..." if len(observation) > 1024 else "")
        if persist_tool_traces:
            self.record_tool_trace(
                session_id,
                "system",
                f"[Tool Execution Trace: {tool_name}]\n{safe_observation}",
                sender=SenderAttribution(
                    sender_id=f"tool:{tool_name}",
                    # Keep existing attribution validation for malformed raw names.
                    sender_display_name=cast(str, tool_name),
                    sender_type=SenderType.TOOL,
                    input_source=InputSource.TOOL_RUNTIME,
                ),
            )

        messages.append({
            "role": "tool",
            "tool_name": tool_name,
            "content": observation,
        })

        if (is_belief_call and tool_result.status.value == "error" and state.belief_attempts >= 2):
            state.recover("belief_attempt_limit_reached", state.budget.inference_attempts + 1)
        elif dispatch_exhausted:
            state.recover("external_tool_budget_exhausted", state.budget.inference_attempts + 1)
        else:
            state.transition(ExecutionPhase.INFER)

    @staticmethod
    def _exhausted(session_id: str, messages: list[dict]) -> None:
        # --- LOOP EXHAUSTION FALLBACK ---
        logger.warning("[%s] Late routing hit max steps; forcing final answer", session_id)
        messages.append({
            "role": "system",
            "content": (
                "[SYSTEM INTERRUPT: MAX TOOL STEPS REACHED]\n"
                "You have reached the maximum allowed tool calls for this turn. "
                "Stop calling tools. Answer the original user request based ONLY on the information you have gathered so far. "
                "If you were unable to complete the task, explicitly explain what you tried, why it failed, and what you need from the user to proceed."
            ),
        })

    @staticmethod
    def _bounded_belief_argument_summary(arguments: object) -> dict:
        if not isinstance(arguments, dict):
            return {"argument_type": type(arguments).__name__}
        assertions = arguments.get("assertions")
        invalidations = arguments.get("invalidations")
        summary: dict[str, object] = {
            "top_level_fields": sorted(str(key)[:64] for key in arguments)[:12],
            "assertion_count": len(assertions) if isinstance(assertions, list) else None,
            "invalidation_count": len(invalidations) if isinstance(invalidations, list) else None,
        }
        if isinstance(assertions, list):
            summary["assertions"] = [
                {
                    "fields": sorted(str(key)[:64] for key in item)[:12],
                    "subject_reference": str(item.get("subject_reference", ""))[:128],
                    "predicate": str(item.get("predicate", ""))[:64],
                    "visibility": str(item.get("visibility", ""))[:32],
                    "expiry_policy": str(item.get("expiry_policy", ""))[:32],
                    "evidence_chars": len(str(item.get("evidence_excerpt", ""))),
                }
                for item in assertions[:4]
                if isinstance(item, dict)
            ]
        return summary
