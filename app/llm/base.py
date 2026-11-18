from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class BufferedGeneration:
    """One completed native generation; metadata is never added to wire messages.

    Missing usage stays unknown. A completion marker does not imply a natural
    stop: callers can distinguish token-limit termination through ``done_reason``.
    The message is request-owned, not recursively frozen by this dataclass.
    """

    message: dict
    done_reason: str | None
    prompt_tokens: int | None
    generated_tokens: int | None
    inference_id: str


def response_text(response: object, *, context: str = "Model") -> str:
    """Extract usable text from the message returned by a blocking chat call."""
    content = response.get("content") if isinstance(response, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"{context} returned no usable text content")
    return content.strip()


class InferenceFailure(RuntimeError):
    """Typed model inference failure safe for orchestrator recovery decisions."""

    def __init__(self, category: str, message: str, *, cancelled: bool = False):
        super().__init__(message)
        self.category = category
        self.cancelled = cancelled


class LLMClient(ABC):
    """Inference contract required by the assistant's generation paths."""

    def generation_options(self, think_override=None, options_override=None) -> dict:
        """Resolve the exact streaming/buffered options without model inference."""
        raise NotImplementedError("This backend does not expose generation options for budgeting.")

    @abstractmethod
    def resolve_think_value(self, think_override=None):
        """Resolve an override against the backend's configured reasoning mode."""
        raise NotImplementedError

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        think_override=None,
        options_override: dict | None = None,
        timeout_override: float | None = None,
        max_retries_override: int | None = None,
        tools: list[dict] | None = None,
        format_override: dict | str | None = None,
        telemetry_session_id: str | None = None,
    ) -> dict:
        """
        Blocking, non-streaming call.
        Must return the assistant message dictionary, with text in `content`.
        `options_override` allows a caller to supply per-request generation
        options without mutating the client's default configuration.
        `format_override` optionally supplies a native structured-output format
        for blocking callers without affecting normal or streaming requests.
        Used for summarizers, classifiers, and other structured workflows.
        """
        raise NotImplementedError

    @abstractmethod
    def chat_buffered(
        self,
        messages: list[dict],
        think_override=None,
        options_override: dict | None = None,
        timeout_override: float | None = None,
        generation_deadline_s: float | None = 120.0,
        tools: list[dict] | None = None,
        generation_phase: str | None = None,
        react_iteration: int | None = None,
        telemetry_session_id: str | None = None,
    ) -> dict:
        """Return a complete generation atomically; never expose partial tool calls.

        Unlike visible streaming, incomplete or failed generation must raise
        InferenceFailure rather than return a partial assistant message.
        Native tool-call messages retain their complete string content and
        optional string thinking fields for turn-local continuation replay.
        """
        raise NotImplementedError

    def chat_buffered_result(
        self,
        messages: list[dict],
        think_override=None,
        options_override: dict | None = None,
        timeout_override: float | None = None,
        generation_deadline_s: float | None = 120.0,
        tools: list[dict] | None = None,
        generation_phase: str | None = None,
        react_iteration: int | None = None,
        telemetry_session_id: str | None = None,
    ) -> BufferedGeneration:
        """Expose atomic generation metadata without a second inference.

        Backends implement this explicitly; no signature probing or retry of a
        message-only call is permitted. Existing message-only clients remain valid.
        """
        raise NotImplementedError("This backend does not expose buffered generation metadata.")

    @abstractmethod
    def stream_chat(
        self,
        messages: list[dict],
        think_override=None,
        tools: list[dict] | None = None,
        options_override: dict | None = None,
        generation_deadline_s: float | None = None,
        timeout_override: float | None = None,
        telemetry_session_id: str | None = None,
    ) -> Iterator[str | dict]:
        """
        Streaming call.
        Yields text chunks (including thinking markers) or native tool-call
        dictionaries. Per-call controls must not mutate backend defaults.
        """
        raise NotImplementedError
