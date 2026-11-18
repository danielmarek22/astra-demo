from dataclasses import asdict
import json

from app.tools.web.contracts import DocumentSnapshot, WebReaderError
from app.tools.web.formatting import source_url
from app.tools.web.sources import SourceReference


class WebReaderService:
    def __init__(self, sources, backend=None, *, window_lines=40, max_output_chars=8000,
                 find_max_matches=5, find_context_lines=3, find_text_max_chars=200,
                 url_max_chars=2048):
        self.sources = sources
        self.backend = backend
        self.window_lines = window_lines
        self.max_output_chars = max_output_chars
        self.find_max_matches = find_max_matches
        self.find_context_lines = find_context_lines
        self.find_text_max_chars = find_text_max_chars
        self.url_max_chars = url_max_chars

    @staticmethod
    def _json(payload):
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _snapshot(self, session_id, reference):
        if not isinstance(reference, str) or not reference.startswith(("search_", "page_")) or len(reference) > 128:
            raise WebReaderError("invalid_arguments", "Find requires a known source or snapshot ID.")
        value = self.sources.get(session_id, reference)
        if isinstance(value, SourceReference):
            if value.snapshot_id is None:
                raise WebReaderError("not_opened", "Open this source before searching its page content.")
            value = self.sources.get(session_id, value.snapshot_id)
        if not isinstance(value, DocumentSnapshot):
            raise WebReaderError("not_opened", "Open this source before searching its page content.")
        return value

    def open(self, session_id, target, start_line=0):
        if not isinstance(target, str) or not target or len(target) > self.url_max_chars:
            raise WebReaderError("invalid_arguments", "Open requires a source ID or an absolute HTTP(S) URL.")
        if type(start_line) is not int or start_line < 0:
            raise WebReaderError("invalid_arguments", "start_line must be a nonnegative integer.")
        source_id = None
        if target.startswith(("search_", "page_")):
            value = self.sources.get(session_id, target)
            if isinstance(value, DocumentSnapshot):
                return self._window(value, start_line)
            source_id = value.source_id
            if value.snapshot_id is not None:
                try:
                    return self._window(self._snapshot(session_id, source_id), start_line)
                except WebReaderError as error:
                    if error.category not in {"expired_reference", "unknown_reference"}:
                        raise
            url = value.url
        else:
            url = source_url(target, self.url_max_chars)
            if url is None:
                raise WebReaderError("blocked_url", "Open requires a valid HTTP(S) URL without credentials or a known source ID.")
        cached = self.sources.find_url(session_id, url)
        if cached is not None:
            if source_id is not None:
                self.sources.bind_snapshot(session_id, source_id, cached.snapshot_id)
            return self._window(cached, start_line)
        if self.backend is None:
            raise WebReaderError("reader_unavailable", "The optional local page reader is not enabled or configured.")
        document = self.backend.read(url)
        snapshot = self.sources.add_document(session_id, document, source_id)
        return self._window(snapshot, start_line)

    def _metadata(self, snapshot):
        return {
            "source_id": snapshot.source_id, "snapshot_id": snapshot.snapshot_id,
            "requested_url": snapshot.requested_url, "final_url": snapshot.final_url,
            "title": snapshot.title, "retrieved_at": snapshot.retrieved_at,
            "content_kind": "read_page", "total_lines": len(snapshot.lines),
            "document_truncated": snapshot.truncated,
        }

    def _window(self, snapshot, start_line):
        if start_line > len(snapshot.lines):
            raise WebReaderError("invalid_line", "start_line exceeds this snapshot's total line count.")
        payload = self._metadata(snapshot)
        payload.update(kind="page_excerpt", start_line=start_line, end_line=start_line,
                       excerpt="", next_line=None, truncated=snapshot.truncated,
                       line_truncated=False, links=[], links_truncated=False,
                       warnings=list(snapshot.warnings))
        while len(self._json(payload)) > self.max_output_chars and payload["warnings"]:
            payload["warnings"].pop()
            payload["truncated"] = True
        if len(self._json(payload)) > self.max_output_chars:
            raise WebReaderError("output_limit", "Page metadata exceeds the configured observation limit.")
        excerpts = []
        for line_number in range(start_line, min(len(snapshot.lines), start_line + self.window_lines)):
            original = snapshot.lines[line_number]
            candidate = f"{line_number}: {original}"
            payload["excerpt"] = "\n".join([*excerpts, candidate])
            payload["end_line"] = line_number + 1
            payload["next_line"] = line_number + 1 if line_number + 1 < len(snapshot.lines) else None
            payload["truncated"] = snapshot.truncated or payload["next_line"] is not None
            if len(self._json(payload)) > self.max_output_chars:
                if excerpts:
                    payload["end_line"] = line_number
                    payload["next_line"] = line_number
                    payload["truncated"] = True
                    break
                lower, upper = 0, len(original)
                payload["line_truncated"] = True
                payload["truncated"] = True
                while lower < upper:
                    middle = (lower + upper + 1) // 2
                    payload["excerpt"] = f"{line_number}: {original[:middle]}"
                    if len(self._json(payload)) <= self.max_output_chars:
                        lower = middle
                    else:
                        upper = middle - 1
                excerpts.append(f"{line_number}: {original[:lower]}")
                break
            excerpts.append(candidate)
        payload["excerpt"] = "\n".join(excerpts)
        for link in snapshot.links:
            payload["links"].append(asdict(link))
            if len(self._json(payload)) > self.max_output_chars:
                payload["links"].pop()
                break
        payload["links_truncated"] = len(payload["links"]) < len(snapshot.links)
        result = self._json(payload)
        if len(result) > self.max_output_chars:
            raise WebReaderError("output_limit", "The page excerpt exceeds the configured observation limit.")
        return result

    @staticmethod
    def _original_position(line, folded_position):
        offset = 0
        for index, character in enumerate(line):
            if offset >= folded_position:
                return index
            offset += len(character.casefold())
        return len(line)

    def find(self, session_id, source_id, text):
        if not isinstance(text, str) or not text.strip() or len(text) > self.find_text_max_chars or "\n" in text or "\r" in text:
            raise WebReaderError("invalid_arguments", "Find requires bounded, nonempty, single-line literal text.")
        snapshot = self._snapshot(session_id, source_id)
        payload = self._metadata(snapshot)
        payload.update(kind="page_find", text=text, case_sensitive=False, match_count=0,
                       matching_lines=0, matches=[], truncated=snapshot.truncated)
        needle = text.casefold()
        matching = []
        for line_number, line in enumerate(snapshot.lines):
            folded = line.casefold()
            count = folded.count(needle)
            if not count:
                continue
            payload["match_count"] += count
            payload["matching_lines"] += 1
            if len(matching) < self.find_max_matches:
                position = self._original_position(line, folded.find(needle))
                start = max(0, position - 160)
                end = min(len(line), position + len(text) * 2 + 160)
                context_start = max(0, line_number - self.find_context_lines)
                context_end = min(len(snapshot.lines), line_number + self.find_context_lines + 1)
                matching.append({
                    "line": line_number, "match_count": count, "excerpt": line[start:end],
                    "start_line": context_start, "end_line": context_end,
                    "context": "\n".join(f"{index}: {snapshot.lines[index][:300]}" for index in range(context_start, context_end)),
                    "line_truncated": start > 0 or end < len(line),
                })
        for match in matching:
            payload["matches"].append(match)
            if len(self._json(payload)) > self.max_output_chars:
                payload["matches"].pop()
                break
        payload["truncated"] = snapshot.truncated or len(payload["matches"]) < payload["matching_lines"]
        result = self._json(payload)
        if len(result) > self.max_output_chars:
            raise WebReaderError("output_limit", "Find metadata exceeds the configured observation limit.")
        return result

    def close(self):
        try:
            if self.backend is not None:
                self.backend.close()
        finally:
            self.sources.clear()
