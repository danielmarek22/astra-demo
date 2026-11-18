import json
import math
from copy import deepcopy
from dataclasses import dataclass

from app.core.task_workspace import TaskWorkspaceMessage
from app.llm.base import InferenceFailure


class PromptBudgetError(InferenceFailure):
    def __init__(self, message, *, metrics=None):
        super().__init__("context_budget", message)
        self.metrics = dict(metrics or {})


class ContextSystemMessage(dict):
    def __init__(self, mandatory_content, optional_sections=()):
        self.mandatory_content = mandatory_content
        self.optional_sections = tuple(optional_sections)
        super().__init__(role="system", content="\n\n---\n\n".join(
            part for part in (mandatory_content, *optional_sections) if part
        ))


def _web_payload(content):
    if not isinstance(content, str) or not content.startswith("[success] "):
        return None
    try:
        payload = json.loads(content[len("[success] "):])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") not in {"search_results", "page_excerpt", "page_find"}:
        return None
    return payload


def compact_web_observation(content, fraction):
    payload = _web_payload(content)
    if payload is None:
        return None
    payload["truncated"] = True
    payload["context_budget_truncated"] = True

    def shorten(record, field):
        value = record.get(field)
        if isinstance(value, str):
            record[field] = value[:int(len(value) * fraction)]

    if payload["kind"] == "search_results":
        for record in payload.get("results", []):
            if isinstance(record, dict):
                shorten(record, "snippet")
                record["truncated"] = True
    elif payload["kind"] == "page_find":
        for match in payload.get("matches", []):
            if isinstance(match, dict):
                shorten(match, "excerpt")
                shorten(match, "context")
                match["line_truncated"] = True
    else:
        original_excerpt = payload.get("excerpt", "")
        shorten(payload, "excerpt")
        excerpt = payload.get("excerpt", "")
        start_line = payload.get("start_line", 0)
        if isinstance(excerpt, str) and type(start_line) is int:
            partial = len(excerpt) < len(original_excerpt) and bool(excerpt) and not excerpt.endswith("\n")
            payload["end_line"] = start_line + len(excerpt.splitlines())
            payload["line_truncated"] = bool(payload.get("line_truncated")) or partial
            payload["next_line"] = max(start_line, payload["end_line"] - int(partial))
        if isinstance(payload.get("links"), list):
            payload["links"] = payload["links"][:int(len(payload["links"]) * fraction)]
            payload["links_truncated"] = True
    if isinstance(payload.get("warnings"), list):
        payload["warnings"] = payload["warnings"][:int(len(payload["warnings"]) * fraction)]
    return "[success] " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def bounded_web_observation(content, max_chars):
    minimum = compact_web_observation(content, 0)
    if minimum is None or len(minimum) > max_chars:
        return None
    lower, upper = 0, 999
    while lower < upper:
        middle = (lower + upper + 1) // 2
        candidate = compact_web_observation(content, middle / 1000)
        if len(candidate) <= max_chars:
            lower = middle
        else:
            upper = middle - 1
    return compact_web_observation(content, lower / 1000)


def compact_mcp_observation(content, fraction):
    """Shorten normalized MCP evidence only; keep status, provenance and links."""
    if not isinstance(content, str):
        return None
    prefix = next((value for value in ("[success] ", "[error] ") if content.startswith(value)), None)
    if prefix is None:
        return None
    try:
        payload = json.loads(content[len(prefix):])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "mcp_tool_result":
        return None
    blocks = payload.get("content")
    if not isinstance(blocks, list) or not all(
        isinstance(block, dict) and (block.get("type") == "resource_link"
                                   or (block.get("type") == "text" and isinstance(block.get("text"), str)))
        for block in blocks
    ):
        return None
    structured = payload.get("structured_content")
    if structured is not None:
        # Most servers duplicate structured output as text. Structured-only output
        # still needs a readable evidence excerpt when its complete object cannot fit.
        if not any(block.get("type") == "text" and block["text"] for block in blocks):
            blocks.append({"type": "text", "text": json.dumps(structured, ensure_ascii=False)})
            payload["structured_content_excerpt"] = True
        payload.update(structured_content=None, structured_content_omitted=True)
    for block in blocks:
        if block.get("type") == "text":
            block["text"] = block["text"][:int(len(block["text"]) * fraction)]
    payload.update(truncated=True, context_budget_truncated=True)
    return prefix + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def tool_groups(messages):
    groups = []
    index = 0
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls")
        if calls:
            if message.get("role") != "assistant" or not isinstance(calls, list):
                raise PromptBudgetError("Invalid tool-call history; no model request was sent.")
            end = index + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            expected = [call["function"].get("name") for call in calls
                        if isinstance(call, dict) and isinstance(call.get("function"), dict)]
            actual = [item.get("tool_name") for item in messages[index + 1:end]]
            if len(expected) != len(calls) or expected != actual or not all(isinstance(name, str) and name for name in expected):
                raise PromptBudgetError("Incomplete or mismatched tool-call/result history; no model request was sent.")
            groups.append(tuple(range(index, end)))
            index = end
        elif message.get("role") == "tool":
            raise PromptBudgetError("An orphan tool result cannot be sent to the model.")
        else:
            index += 1
    return groups


def has_native_continuation(message):
    return (message.get("role") == "assistant" and bool(message.get("tool_calls"))
            and bool(message.get("thinking") or message.get("content")))


@dataclass(frozen=True)
class BudgetedPrompt:
    messages: list[dict]
    options_override: dict
    metrics: dict


class PromptBudget:
    def __init__(self, *, context_window_tokens=32768, completion_reserve_tokens=2048,
                 safety_margin_tokens=1024, media_reserve_tokens=8192, text_bytes_per_token=1.0):
        if any(type(value) is not int or value <= 0 for value in (
            context_window_tokens, completion_reserve_tokens, safety_margin_tokens, media_reserve_tokens,
        )):
            raise ValueError("Prompt budget limits must be positive integers.")
        if (type(text_bytes_per_token) not in (int, float) or not math.isfinite(text_bytes_per_token)
                or not 1 <= text_bytes_per_token <= 4):
            raise ValueError("text_bytes_per_token must be finite and between 1 and 4.")
        self.context_window_tokens = context_window_tokens
        self.completion_reserve_tokens = completion_reserve_tokens
        self.safety_margin_tokens = safety_margin_tokens
        self.media_reserve_tokens = media_reserve_tokens
        self.text_bytes_per_token = text_bytes_per_token

    def estimate(self, messages, tools):
        total = 256
        for message in messages:
            text = {key: value for key, value in message.items() if key != "images"}
            text_bytes = len(json.dumps(text, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            total += math.ceil(text_bytes / self.text_bytes_per_token) + 64
            images = message.get("images", [])
            if not isinstance(images, list):
                raise PromptBudgetError("Unsupported media payload for context budgeting.")
            total += len(images) * self.media_reserve_tokens
        if tools:
            schema_bytes = len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            total += math.ceil(schema_bytes / self.text_bytes_per_token) + 256
        return total

    def prepare(self, messages, tools, generation_options, options_override=None, *, continuation_only=False):
        """Bound replay even without the full guard; that mode only omits older native fields."""
        configured_window = generation_options.get("num_ctx", self.context_window_tokens)
        prediction = generation_options.get("num_predict", self.completion_reserve_tokens)
        if type(configured_window) is not int or configured_window <= 0 or type(prediction) is not int or prediction <= 0:
            raise PromptBudgetError("Context budgeting requires finite positive context and completion limits.")
        window = min(configured_window, self.context_window_tokens)
        reserve = max(prediction, self.completion_reserve_tokens)
        limit = window - reserve - self.safety_margin_tokens
        if limit <= 0:
            raise PromptBudgetError("Completion reserve and safety margin leave no room for the prompt.")
        working = deepcopy(list(messages))
        groups = tool_groups(working)
        protected_group = groups[-1] if groups and (has_native_continuation(working[groups[-1][0]])
            or any(isinstance(message, TaskWorkspaceMessage) for message in working)) else ()
        removed = set()
        counters = {"history_messages_removed": 0, "background_sections_removed": 0,
                    "web_observations_reduced": 0, "mcp_observations_reduced": 0, "tool_groups_removed": 0,
                    "thinking_fields_removed": 0, "assistant_preambles_removed": 0}

        def render():
            rendered = [dict(message) for index, message in enumerate(working) if index not in removed]
            if any(counters.values()):
                notice = (
                    "Context budget notice: optional history/background, assistant continuation or tool evidence was reduced. "
                    "Truncation flags identify shortened web/MCP evidence; do not invent omitted details "
                    "or infer that an omitted tool operation succeeded. Retained source URLs remain intact."
                )
                position = 1 if rendered and rendered[0].get("role") == "system" else 0
                rendered.insert(position, {"role": "system", "content": notice})
            return rendered

        def fits():
            return self.estimate(render(), tools) <= limit

        current_user = max((index for index, message in enumerate(working) if message.get("role") == "user"), default=len(working))
        history_blocks = []
        block = []
        group_members = {index for group in groups for index in group}
        for index, message in enumerate(working[:current_user]):
            if message.get("role") == "system" or isinstance(message, TaskWorkspaceMessage) or index in group_members:
                continue
            if message.get("role") == "user" and block:
                history_blocks.append(block)
                block = []
            block.append(index)
        if block:
            history_blocks.append(block)
        for block in history_blocks if not continuation_only else ():
            if fits():
                break
            removed.update(block)
            counters["history_messages_removed"] += len(block)
        for message in working if not continuation_only else ():
            if not isinstance(message, ContextSystemMessage):
                continue
            sections = list(message.optional_sections)
            while sections and not fits():
                sections.pop()
                message["content"] = "\n\n---\n\n".join(part for part in (message.mandatory_content, *sections) if part)
                counters["background_sections_removed"] += 1
        # Keep complete fields when they fit. Under pressure, omit older fields
        # whole before reducing source evidence; the latest selected step is protected.
        # Sacrifice older preambles first to retain as much thinking as possible.
        for field, counter in (("content", "assistant_preambles_removed"),
                               ("thinking", "thinking_fields_removed")):
            for group in groups[:-1]:
                if fits():
                    break
                message = working[group[0]]
                if not message.get(field):
                    continue
                if field == "thinking":
                    message.pop(field)
                else:
                    message[field] = ""
                counters[counter] += 1
        web_observations = []
        latest_pages = {}
        priorities = {}
        reduced_web_indices = set()
        for index, message in enumerate(working) if not continuation_only else ():
            if message.get("role") != "tool" or message.get("tool_name") not in {"web__search", "web__open", "web__find"}:
                continue
            payload = _web_payload(message.get("content"))
            if payload is None:
                continue
            web_observations.append((index, payload))
            priorities[index] = 2 if payload["kind"] == "page_find" else 0
            if payload["kind"] == "page_excerpt":
                source = next(((field, payload[field]) for field in ("snapshot_id", "source_id", "final_url")
                               if isinstance(payload.get(field), str) and payload[field]), ("message", index))
                latest_pages[source] = index
        for index in latest_pages.values():
            priorities[index] = 1
        for index, payload in web_observations:
            if index < current_user:
                priorities[index] = 0
            if fits():
                continue
            if payload["kind"] != "page_excerpt" or not isinstance(payload.get("links"), list) or not payload["links"]:
                continue
            payload.update(links=[], links_truncated=True, truncated=True, context_budget_truncated=True)
            working[index]["content"] = "[success] " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            reduced_web_indices.add(index)
            counters["web_observations_reduced"] = len(reduced_web_indices)
        previous_priority = 0
        for index, _payload in sorted(web_observations, key=lambda observation: (priorities[observation[0]], observation[0])):
            if fits():
                break
            priority = priorities[index]
            if priority > previous_priority:
                for group in groups:
                    if fits():
                        break
                    if group == protected_group or any(member in removed for member in group):
                        continue
                    if all(member in priorities and priorities[member] < priority for member in group[1:]):
                        removed.update(group)
                        counters["tool_groups_removed"] += 1
                previous_priority = priority
            if fits():
                break
            message = working[index]
            original = message.get("content")
            minimum = compact_web_observation(original, 0)
            if minimum is None:
                continue
            message["content"] = minimum
            reduced_web_indices.add(index)
            counters["web_observations_reduced"] = len(reduced_web_indices)
            if fits():
                lower, upper = 0, 999
                while lower < upper:
                    middle = (lower + upper + 1) // 2
                    message["content"] = compact_web_observation(original, middle / 1000)
                    if fits():
                        lower = middle
                    else:
                        upper = middle - 1
                message["content"] = compact_web_observation(original, lower / 1000)
        # A protected native pair must keep its call and complete assistant fields,
        # but its external evidence can be explicitly shortened just like web data.
        for message in working if not continuation_only else ():
            if fits():
                break
            if message.get("role") != "tool" or not str(message.get("tool_name", "")).startswith("mcp__"):
                continue
            original = message.get("content")
            candidate = compact_mcp_observation(original, 1)
            if candidate is None:
                continue
            message["content"] = candidate
            counters["mcp_observations_reduced"] += 1
            if fits():
                continue
            message["content"] = compact_mcp_observation(original, 0)
            if fits():
                lower, upper = 0, 999
                while lower < upper:
                    middle = (lower + upper + 1) // 2
                    message["content"] = compact_mcp_observation(original, middle / 1000)
                    if fits():
                        lower = middle
                    else:
                        upper = middle - 1
                message["content"] = compact_mcp_observation(original, lower / 1000)
        for group in groups if not continuation_only else ():
            if fits():
                break
            if group == protected_group or any(member in removed for member in group):
                continue
            removed.update(group)
            counters["tool_groups_removed"] += 1
        result = render()
        estimate = self.estimate(result, tools)
        overrides = dict(options_override or {})
        overrides.update(num_ctx=window, num_predict=prediction)
        metrics = {**counters, "estimated_input_tokens": estimate,
                   "thinking_fields_retained": sum(bool(message.get("thinking")) for message in result
                                                   if message.get("role") == "assistant" and message.get("tool_calls")),
                   "assistant_preambles_retained": sum(bool(message.get("content")) for message in result
                                                      if message.get("role") == "assistant" and message.get("tool_calls")),
                   "continuation_only": continuation_only,
                   "prompt_limit_tokens": limit, "estimated_overflow_tokens": max(0, estimate - limit),
                   "context_window_tokens": window, "completion_reserve_tokens": reserve,
                   "safety_margin_tokens": self.safety_margin_tokens,
                   "text_bytes_per_token": self.text_bytes_per_token,
                   "estimation_method": ("utf8_bytes_plus_framing_and_media_reserve" if self.text_bytes_per_token == 1
                                         else "scaled_utf8_bytes_plus_framing_and_media_reserve"),
                   "exact_token_count": False}
        if estimate > limit:
            raise PromptBudgetError(
                "Required context, including any protected latest assistant continuation and its tool result, "
                f"exceeds the context budget (estimated input {estimate}; available {limit}; "
                f"context window {window}; generation reserve {reserve}; safety margin {self.safety_margin_tokens}). "
                "Shorten the input or configure an appropriate context window; no model request was sent.",
                metrics=metrics,
            )
        return BudgetedPrompt(result, overrides, metrics)
