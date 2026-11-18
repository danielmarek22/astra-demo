"""Bounded provenance footer from this turn's actual web results, without inference."""

from dataclasses import dataclass
import html
import json
import re
from urllib.parse import quote, urlsplit

from app.tools.web.formatting import plain_text, source_url


_MAX_SOURCES = 8
_MAX_FOOTER_CHARS = 6000
_WEB_TOOLS = {"web__search", "web__open", "web__find"}


@dataclass(frozen=True)
class _Source:
    title: str
    url: str
    opened: bool

    def markdown(self):
        title = re.sub(r"([\\`*_\[\]()|])", r"\\\1", html.escape(self.title, quote=False))
        url = quote(self.url, safe=":/?#[]@!$&'*+,;=%~")
        return f"- [{title}](<{url}>) — {'Page opened' if self.opened else 'Search result'}"


class RetrievedSources:
    """Collect retrieval provenance, not model-selected claim citations or cache contents."""

    def __init__(self):
        self.attempted = False
        self._sources: dict[str, _Source] = {}
        self._omitted = False

    def _add(self, url, title, *, opened, requested_url=None):
        url = source_url(url, 2048)
        if url is None:
            return
        title, _ = plain_text(title, 120)
        source = _Source(title or urlsplit(url).hostname or "Source", url, opened)
        requested = source_url(requested_url, 2048)
        if requested and requested != url and requested in self._sources:
            # A redirect replaces the discovery link with the actual opened page.
            self._sources.pop(requested)
        previous = self._sources.get(url)
        if previous is not None:
            if opened and not previous.opened:
                self._sources[url] = source
            return
        if len(self._sources) >= _MAX_SOURCES:
            replaceable = next((key for key, item in self._sources.items() if not item.opened), None)
            self._omitted = True
            if not opened or replaceable is None:
                return
            self._sources.pop(replaceable)
        self._sources[url] = source

    def observe(self, tool_name, content, *, succeeded):
        if not isinstance(tool_name, str) or tool_name not in _WEB_TOOLS:
            return
        self.attempted = True
        if not succeeded or not isinstance(content, str):
            return
        try:
            payload = json.loads(content)
        except (ValueError, RecursionError):
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("kind")
        if tool_name == "web__search" and kind == "search_results":
            results = payload.get("results", [])
            if isinstance(results, list):
                for result in results:
                    if isinstance(result, dict):
                        self._add(result.get("url"), result.get("title"), opened=False)
        elif ((tool_name == "web__open" and kind == "page_excerpt")
              or (tool_name == "web__find" and kind == "page_find")):
            self._add(payload.get("final_url"), payload.get("title"), opened=True,
                      requested_url=payload.get("requested_url"))

    def append_to(self, response):
        if not self.attempted:
            return response
        lines = ["---", "**Sources retrieved**", ""]
        omitted = self._omitted
        for source in sorted(self._sources.values(), key=lambda item: not item.opened):
            line = source.markdown()
            if sum(len(item) + 1 for item in lines) + len(line) > _MAX_FOOTER_CHARS - 100:
                omitted = True
                continue
            lines.append(line)
        if not self._sources:
            lines.append("No live sources were retrieved for this turn.")
        if omitted:
            lines.extend(["", "Additional sources omitted to keep this list short."])
        return response.rstrip() + "\n\n" + "\n".join(lines)
