from html.parser import HTMLParser
import json
from urllib.parse import urlsplit, urlunsplit
import uuid

from app.tools.web.contracts import SearchResponse, WebSearchError, WebSearchResult


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, _attributes):
        if tag in {"script", "style"}:
            self.hidden += 1
        self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: object, limit: int) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    parser = _PlainText()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    text = " ".join("".join(character if character.isprintable() else " " for character in text).split())
    return text[:limit], len(text) > limit


def source_url(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if "\\" in value or any(character.isspace() or not character.isprintable() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or not parsed.hostname.strip("."):
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        default_port = parsed.scheme.lower() == "http" and port == 80 or parsed.scheme.lower() == "https" and port == 443
        if port is not None and not default_port:
            host += f":{port}"
        normalized = urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, parsed.fragment))
    except (ValueError, UnicodeError):
        return None
    return normalized if len(normalized) <= limit else None


def normalize_response(
    payload: dict, retrieved_at: str, *, max_results: int,
    title_max_chars: int, url_max_chars: int, snippet_max_chars: int,
    publication_date_source: str = "searxng.publishedDate",
) -> SearchResponse:
    records = []
    seen = set()
    discarded = 0
    omitted = 0
    warnings = []
    for item in payload["results"]:
        if not isinstance(item, dict):
            discarded += 1
            continue
        if any(item.get(field) is not None and not isinstance(item[field], str) for field in ("title", "content")):
            discarded += 1
            continue
        url = source_url(item.get("url"), url_max_chars)
        if url is None or url in seen:
            discarded += 1
            continue
        seen.add(url)
        if len(records) >= max_results:
            omitted += 1
            continue
        title, title_trimmed = plain_text(item.get("title"), title_max_chars)
        snippet, snippet_trimmed = plain_text(item.get("content"), snippet_max_chars)
        published, published_trimmed = plain_text(item.get("publishedDate"), 100)
        records.append(WebSearchResult(
            title=title, url=url, content=snippet, published_at=published or None,
            truncated=title_trimmed or snippet_trimmed or published_trimmed,
        ))
    failures = payload.get("unresponsive_engines")
    failure_count = sum(1 for failure in failures if isinstance(failure, (list, tuple)) and len(failure) == 2
                        and isinstance(failure[0], str) and failure[0].strip() and isinstance(failure[1], str)) if isinstance(failures, list) else 0
    if isinstance(failures, list):
        for failure in failures[:3]:
            if isinstance(failure, (list, tuple)) and len(failure) == 2:
                engine, _ = plain_text(failure[0], 60)
                reason, _ = plain_text(failure[1], 100)
                if engine:
                    warnings.append(f"Provider engine unavailable: {engine} ({reason or 'unspecified'}).")
        if len(failures) > 3:
            warnings.append("Additional provider engines were unavailable.")
    if discarded:
        warnings.append(f"Discarded {discarded} invalid or duplicate result records.")
    if not records and payload["results"]:
        warnings.append("The provider returned records, but none were usable.")
    return SearchResponse(
        results=tuple(records), retrieved_at=retrieved_at, warnings=tuple(warnings),
        discarded_count=discarded, omitted_count=omitted,
        truncated=bool(omitted) or any(record.truncated for record in records),
        provider_failure_count=failure_count, publication_date_source=publication_date_source,
    )


def format_search(response: SearchResponse, query: str, max_chars: int) -> str:
    search_id = uuid.uuid4().hex
    payload = {
        "kind": "search_results", "query": query, "retrieved_at": response.retrieved_at,
        "results": [], "returned_count": 0, "omitted_count": response.omitted_count,
        "discarded_count": response.discarded_count,
        "truncated": response.truncated, "warnings": list(response.warnings),
        "provider_failure_count": response.provider_failure_count,
    }

    def serialize():
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    while len(serialize()) > max_chars and payload["warnings"]:
        payload["warnings"].pop()
        payload["truncated"] = True
    for index, record in enumerate(response.results):
        source = {
            "source_id": f"search_{search_id}_{index + 1}", "content_kind": "search_snippet",
            "title": record.title, "url": record.url, "snippet": record.content,
            "retrieved_at": response.retrieved_at, "truncated": record.truncated,
        }
        if record.published_at is not None:
            source["published_at"] = record.published_at
            source["publication_date_source"] = response.publication_date_source
        payload["results"].append(source)
        payload["returned_count"] += 1
        if len(serialize()) > max_chars:
            payload["results"].pop()
            payload["returned_count"] -= 1
            payload["omitted_count"] += len(response.results) - index
            payload["truncated"] = True
            break
    result = serialize()
    if len(result) > max_chars:
        raise WebSearchError("output_limit", "Search metadata exceeds the configured output limit.")
    return result
