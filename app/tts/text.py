"""Text preparation migrated from the base server."""

import re
import emoji
from app.llm.thinking_filter import strip_complete_thinking_blocks

_GENERATED_AUDIO_NAME_RE = re.compile(r"^[0-9a-f]{32}\.wav$")
_FENCED_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_MARKDOWN_REFERENCE_DEF_RE = re.compile(r"^\s*\[[^\]]+\]:\s+\S+.*$", re.MULTILINE)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MARKDOWN_REFERENCE_LINK_RE = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_MARKDOWN_AUTOLINK_RE = re.compile(r"<https?://[^>]+>")
_MARKDOWN_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MARKDOWN_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_MARKDOWN_UNORDERED_LIST_RE = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)
_MARKDOWN_ORDERED_LIST_RE = re.compile(r"^\s{0,3}\d+\.\s+", re.MULTILINE)
_MARKDOWN_HRULE_RE = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$", re.MULTILINE)
_MARKDOWN_BOLD_RE = re.compile(r"(\*\*|__)(.*?)\1")
_MARKDOWN_ITALIC_RE = re.compile(r"(?<!\w)(\*|_)(.+?)\1(?!\w)")
_MARKDOWN_STRIKE_RE = re.compile(r"~~(.+?)~~")
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>~|])")
_WHITESPACE_RE = re.compile(r"\s+")


def _prepare_tts_text(text: str) -> str:
    """
    Convert markdown-ish text into plain text suitable for TTS:
    - drop fenced code blocks
    - keep link/image labels, drop URLs
    - remove block markers (headings, lists, quotes, rulers)
    - unwrap inline emphasis/code markers
    """
    if not text:
        return ""

    escaped_markers: dict[str, str] = {}

    def _protect_escaped(match: re.Match[str]) -> str:
        token = f"TTSESCAPED{len(escaped_markers)}TOKEN"
        escaped_markers[token] = match.group(1)
        return token

    cleaned = _MARKDOWN_ESCAPE_RE.sub(_protect_escaped, text)
    cleaned = strip_complete_thinking_blocks(cleaned)
    cleaned = _FENCED_CODE_BLOCK_RE.sub(" ", cleaned)
    cleaned = _MARKDOWN_REFERENCE_DEF_RE.sub(" ", cleaned)
    cleaned = _MARKDOWN_IMAGE_RE.sub(r"\1", cleaned)
    cleaned = _MARKDOWN_LINK_RE.sub(r"\1", cleaned)
    cleaned = _MARKDOWN_REFERENCE_LINK_RE.sub(r"\1", cleaned)
    cleaned = _MARKDOWN_AUTOLINK_RE.sub(" ", cleaned)
    cleaned = _MARKDOWN_INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = _MARKDOWN_HEADING_RE.sub("", cleaned)
    cleaned = _MARKDOWN_BLOCKQUOTE_RE.sub("", cleaned)
    cleaned = _MARKDOWN_UNORDERED_LIST_RE.sub("", cleaned)
    cleaned = _MARKDOWN_ORDERED_LIST_RE.sub("", cleaned)
    cleaned = _MARKDOWN_HRULE_RE.sub(" ", cleaned)
    cleaned = emoji.replace_emoji(cleaned, replace=" ")

    # Run a few passes so nested emphasis is progressively unwrapped.
    for _ in range(3):
        cleaned = _MARKDOWN_BOLD_RE.sub(r"\2", cleaned)
        cleaned = _MARKDOWN_ITALIC_RE.sub(r"\2", cleaned)
        cleaned = _MARKDOWN_STRIKE_RE.sub(r"\1", cleaned)

    for token, marker in escaped_markers.items():
        cleaned = cleaned.replace(token, marker)

    return _WHITESPACE_RE.sub(" ", cleaned).strip()
