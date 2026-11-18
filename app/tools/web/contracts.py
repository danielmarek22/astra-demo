from dataclasses import dataclass


class WebSearchError(Exception):
    def __init__(self, category: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    content: str
    published_at: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class SearchResponse:
    results: tuple[WebSearchResult, ...]
    retrieved_at: str
    warnings: tuple[str, ...] = ()
    discarded_count: int = 0
    omitted_count: int = 0
    truncated: bool = False
    provider_failure_count: int = 0
    publication_date_source: str = "searxng.publishedDate"


class WebReaderError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class PageLink:
    url: str
    text: str


@dataclass(frozen=True)
class ReadDocument:
    requested_url: str
    final_url: str
    title: str
    retrieved_at: str
    markdown: str
    links: tuple[PageLink, ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentSnapshot:
    snapshot_id: str
    source_id: str
    requested_url: str
    final_url: str
    title: str
    retrieved_at: str
    lines: tuple[str, ...]
    links: tuple[PageLink, ...]
    truncated: bool
    warnings: tuple[str, ...]
