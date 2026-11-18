"""Internal outcomes of one native generation step, before tool execution."""

from dataclasses import dataclass, field
from typing import Mapping, TypeAlias

from app.integrations import CapabilityId, ToolCall
from app.llm.base import BufferedGeneration, InferenceFailure


@dataclass(frozen=True)
class AssistantContinuation:
    """Validated native fields for this turn's selected call, never durable memory."""

    content: str = ""
    thinking: str = ""

    @classmethod
    def from_message(cls, message: dict) -> "AssistantContinuation":
        content = message.get("content", "")
        thinking = message.get("thinking", "")
        if not isinstance(content, str) or not isinstance(thinking, str):
            raise InferenceFailure("malformed_response", "Native assistant content and thinking must be text.")
        return cls(content=content, thinking=thinking)

    def message_fields(self) -> dict:
        fields = {"content": self.content}
        if self.thinking:
            fields["thinking"] = self.thinking
        return fields


@dataclass(frozen=True)
class FinalText:
    response: str
    generation: BufferedGeneration | None = field(default=None, compare=False)


@dataclass(frozen=True)
class AcceptedToolCall:
    call: ToolCall
    continuation: AssistantContinuation = field(default_factory=AssistantContinuation)
    generation: BufferedGeneration | None = field(default=None, compare=False)

    @property
    def tool_name(self) -> str:
        return str(self.call.capability)

    @property
    def tool_arguments(self) -> Mapping[str, object]:
        return self.call.arguments


@dataclass(frozen=True)
class InvalidToolCall:
    # Preserve malformed values for the model's correction observation.
    tool_name: object
    tool_arguments: object
    error: str
    continuation: AssistantContinuation = field(default_factory=AssistantContinuation)
    generation: BufferedGeneration | None = field(default=None, compare=False)


@dataclass(frozen=True)
class IncompleteGeneration:
    generation: BufferedGeneration


GenerationStepResult: TypeAlias = FinalText | AcceptedToolCall | InvalidToolCall | IncompleteGeneration


def parse_tool_call(raw: object, *, continuation: AssistantContinuation | None = None) -> AcceptedToolCall | InvalidToolCall:
    """Validate call structure; capability authorization belongs to the executor."""
    tool_name: object = ""
    tool_arguments: object = {}
    try:
        if not isinstance(raw, dict):
            raise ValueError("Tool call must be an object")
        function = raw.get("function", {})
        if not isinstance(function, dict):
            raise ValueError("Tool function must be an object")
        tool_name = function.get("name", "")
        tool_arguments = function.get("arguments", {})
        if not isinstance(tool_name, str):
            raise ValueError(f"Invalid capability ID: {tool_name!r}")
        capability = CapabilityId.parse(tool_name)
        if not isinstance(tool_arguments, dict):
            raise ValueError("Tool arguments must be an object")
        return AcceptedToolCall(ToolCall(capability=capability, arguments=tool_arguments),
                                continuation=continuation or AssistantContinuation())
    except (TypeError, ValueError) as exc:
        return InvalidToolCall(
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            error=f"Invalid tool call {tool_name!r}: {exc}",
            continuation=continuation or AssistantContinuation(),
        )
