"""Small test fixtures shared by related suites."""

import json
from unittest.mock import Mock

import requests


def consume_generator(generator):
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as completed:
            return events, completed.value


def make_response(*, data=None, lines=None, status_code=200, http_error=None):
    """Build a readable, closable response; tests may replace iter_lines to fail midstream."""
    response = requests.Response()
    response.status_code = status_code
    response._content = (
        b"\n".join(lines) if lines is not None
        else json.dumps(data if data is not None else {}).encode("utf-8")
    )
    response._content_consumed = True
    if http_error is not None:
        response.raise_for_status = Mock(side_effect=http_error)
    return response


def buffered_generation(message, *, reason="stop", prompt_tokens=None, generated_tokens=None):
    from app.llm.base import BufferedGeneration
    return BufferedGeneration(message, reason, prompt_tokens, generated_tokens, "test-inference")


class BufferedResultMock(Mock):
    """Script native messages while exercising the rich inference API."""

    def _execute_mock_call(self, *args, **kwargs):
        from app.llm.base import BufferedGeneration
        value = super()._execute_mock_call(*args, **kwargs)
        return value if isinstance(value, BufferedGeneration) else buffered_generation(value)


def rendered_step(generator, session_id, messages, user_text):
    """Explicit classification then terminal rendering for renderer contract tests."""
    from app.core.generation_step import FinalText
    result = yield from generator._stream_late_routing_step(session_id, messages, user_text)
    if isinstance(result, FinalText):
        response = yield from generator._render_terminal_response(
            session_id=session_id, messages=messages, response=result.response,
        )
        return FinalText(response)
    return result
