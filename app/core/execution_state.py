"""Turn-local execution contracts, separate from presentation and persistence."""

import math
from dataclasses import dataclass, field, replace
from enum import Enum

from app.core.generation_step import AcceptedToolCall, InvalidToolCall
from app.integrations import CapabilityId
from app.llm.base import BufferedGeneration
from app.core.task_workspace import TaskWorkspace, WorkspaceLimits


class ExecutionProfile(str, Enum):
    STANDARD = "standard"
    DEEP = "deep"


class ExecutionPhase(str, Enum):
    INFER = "infer"
    EXECUTE_TOOL = "execute_tool"
    FINALIZE = "finalize"
    CONTINUE = "continue"
    RECOVER = "recover"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InferencePhase(str, Enum):
    INITIAL = "initial"
    CONTINUATION = "continuation"
    CORRECTION = "correction"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class ExecutionLimits:
    # Preserve the existing meaning: normal inference attempts, then at most
    # one tool-free recovery. This is not an independent external-tool cap.
    max_inference_steps: int = 5
    generation_deadline_s: float = 600.0
    recovery_deadline_s: float = 180.0
    recovery_num_predict: int = 192
    recovery_observation_chars: int = 1200
    correction_num_predict: int = 512
    context_window_tokens: int | None = None
    num_predict: int | None = None
    max_continuations: int = 2
    max_continuation_attempts: int = 3
    max_tool_dispatches: int | None = None
    workspace: WorkspaceLimits = field(default_factory=WorkspaceLimits)

    def __post_init__(self) -> None:
        for name in ("max_inference_steps", "recovery_num_predict", "recovery_observation_chars", "correction_num_predict",
                     "context_window_tokens", "num_predict", "max_continuation_attempts", "max_tool_dispatches"):
            value = getattr(self, name)
            if value is None and name in {"context_window_tokens", "num_predict", "max_tool_dispatches"}:
                continue
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.max_continuations) is not int or self.max_continuations < 0:
            raise ValueError("max_continuations must be a nonnegative integer")
        if not isinstance(self.workspace, WorkspaceLimits):
            raise ValueError("workspace must contain typed workspace limits")
        for name in ("generation_deadline_s", "recovery_deadline_s"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class ExecutionProfileOverrides:
    """Omitted settings inherit existing application configuration."""

    max_inference_steps: int | None = None
    generation_deadline_s: float | None = None
    recovery_deadline_s: float | None = None
    recovery_num_predict: int | None = None
    recovery_observation_chars: int | None = None
    correction_num_predict: int | None = None
    context_window_tokens: int | None = None
    num_predict: int | None = None

    max_continuations: int | None = None
    max_continuation_attempts: int | None = None
    max_tool_dispatches: int | None = None
    workspace: WorkspaceLimits | None = None

    def resolve(self, base: ExecutionLimits) -> ExecutionLimits:
        return replace(base, **{name: value for name, value in vars(self).items() if value is not None})


@dataclass(frozen=True)
class ExecutionSettings:
    profile: ExecutionProfile = ExecutionProfile.STANDARD
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    # Internal continuations are opt-in, independent of native model thinking.
    reasoning_enabled: bool = False


@dataclass
class ExecutionBudget:
    limits: ExecutionLimits
    inference_attempts: int = 0
    tool_interactions: int = 0
    tool_dispatches: int = 0
    recovery_attempts: int = 0
    continuation_attempts: int = 0
    accepted_continuations: int = 0

    def admit_inference(self) -> bool:
        if self.inference_attempts >= self.limits.max_inference_steps:
            return False
        self.inference_attempts += 1
        return True

    def admit_recovery(self) -> None:
        if self.recovery_attempts:
            raise RuntimeError("Execution recovery cannot recurse")
        self.recovery_attempts += 1


@dataclass(frozen=True)
class ExecutionOutcome:
    response: str
    stop_reason: str
    recovered: bool = False


_TRANSITIONS = {
    ExecutionPhase.INFER: frozenset({ExecutionPhase.EXECUTE_TOOL, ExecutionPhase.CONTINUE, ExecutionPhase.RECOVER,
                                    ExecutionPhase.FINALIZE, ExecutionPhase.COMPLETED, ExecutionPhase.FAILED, ExecutionPhase.CANCELLED}),
    ExecutionPhase.EXECUTE_TOOL: frozenset({ExecutionPhase.INFER, ExecutionPhase.RECOVER,
                                           ExecutionPhase.FAILED, ExecutionPhase.CANCELLED}),
    ExecutionPhase.CONTINUE: frozenset({ExecutionPhase.INFER, ExecutionPhase.RECOVER,
                                       ExecutionPhase.FAILED, ExecutionPhase.CANCELLED}),
    ExecutionPhase.FINALIZE: frozenset({ExecutionPhase.COMPLETED, ExecutionPhase.RECOVER,
                                       ExecutionPhase.FAILED, ExecutionPhase.CANCELLED}),
    ExecutionPhase.RECOVER: frozenset({ExecutionPhase.COMPLETED, ExecutionPhase.FAILED, ExecutionPhase.CANCELLED}),
    ExecutionPhase.COMPLETED: frozenset(),
    ExecutionPhase.FAILED: frozenset(),
    ExecutionPhase.CANCELLED: frozenset(),
}


@dataclass
class ExecutionState:
    settings: ExecutionSettings
    normal_think: object
    allowed_capabilities: frozenset[CapabilityId] | None = None
    phase: ExecutionPhase = ExecutionPhase.INFER
    inference_phase: InferencePhase = InferencePhase.INITIAL
    belief_attempts: int = 0
    belief_disabled: bool = False
    suppressed_capabilities: set[CapabilityId] = field(default_factory=set)
    pending_call: AcceptedToolCall | InvalidToolCall | None = None
    stop_reason: str | None = None
    recovery_iteration: int | None = None
    workspace: TaskWorkspace | None = None
    generations: list[BufferedGeneration] = field(default_factory=list)
    outcome: ExecutionOutcome | None = None
    budget: ExecutionBudget = field(init=False)

    def __post_init__(self) -> None:
        self.budget = ExecutionBudget(self.settings.limits)
        if self.allowed_capabilities is not None:
            self.allowed_capabilities = frozenset(self.allowed_capabilities)

    def transition(self, phase: ExecutionPhase) -> None:
        if phase not in _TRANSITIONS[self.phase]:
            raise RuntimeError(f"Invalid execution transition: {self.phase.value} -> {phase.value}")
        self.phase = phase

    def recover(self, reason: str, iteration: int | None = None) -> None:
        self.stop_reason = reason
        self.recovery_iteration = iteration
        self.transition(ExecutionPhase.RECOVER)
