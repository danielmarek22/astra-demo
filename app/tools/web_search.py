from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
from threading import BoundedSemaphore, Event, Lock, Thread
import time

import httpx

from app.logging import trace_event
from app.tools.web.contracts import SearchResponse, WebSearchError, WebSearchResult
from app.tools.web.formatting import format_search, normalize_response


logger = logging.getLogger(__name__)


class BoundedSearchClient:
    provider_name = "Web search"
    publication_date_source = "provider.published_at"

    def __init__(
        self, base_url: str = "http://localhost:8080", timeout: float = 10.0,
        max_retries: int = 2, retry_backoff_s: float = 0.25, *,
        total_timeout_s: float = 15.0, max_retry_after_s: float = 2.0,
        max_response_bytes: int = 1048576, title_max_chars: int = 240,
        url_max_chars: int = 2048, snippet_max_chars: int = 1000,
        query_max_chars: int = 512, transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self.total_timeout_s = total_timeout_s
        self.max_retry_after_s = max_retry_after_s
        self.max_response_bytes = max_response_bytes
        self.title_max_chars = title_max_chars
        self.url_max_chars = url_max_chars
        self.snippet_max_chars = snippet_max_chars
        self.query_max_chars = query_max_chars
        self.is_available = False
        self._transport = transport
        self._state_lock = Lock()
        self._slot = BoundedSemaphore(1)
        self._thread = None
        self._loop = None
        self._future = None
        self._closed = False
        self._worker_error = None

    def validate_query(self, query: str, time_range: str | None = None) -> str:
        if not isinstance(query, str) or not query.strip() or len(query) > self.query_max_chars:
            raise WebSearchError("invalid_arguments", f"Search query must contain 1–{self.query_max_chars} characters.")
        if any(not character.isprintable() and not character.isspace() for character in query):
            raise WebSearchError("invalid_arguments", "Search query contains unsupported control characters.")
        if time_range is not None and (not isinstance(time_range, str) or time_range not in {"day", "month", "year"}):
            raise WebSearchError("invalid_arguments", "Search time_range must be day, month, or year.")
        return " ".join(query.split())

    def probe(self) -> bool:
        trace_event("web_search", "probe_start", payload={"base_url": self.base_url})
        try:
            self._request(self._search_params("ping", 1, None), min(2.0, self.total_timeout_s), 0)
            self.is_available = True
        except WebSearchError:
            self.is_available = False
        trace_event("web_search", "probe_result", payload={"is_available": self.is_available})
        return self.is_available

    def search(self, query: str, limit: int = 5, time_range: str | None = None) -> SearchResponse:
        query = self.validate_query(query, time_range)
        params = self._search_params(query, limit, time_range)
        trace_event("web_search", "search_request", payload={"provider": self.provider_name, "params": params, "limit": limit})
        try:
            payload = self._request(params, self.total_timeout_s, self.max_retries)
        except WebSearchError:
            self.is_available = False
            raise
        self.is_available = True
        result = normalize_response(
            payload, datetime.now(timezone.utc).isoformat(), max_results=limit,
            title_max_chars=self.title_max_chars, url_max_chars=self.url_max_chars,
            snippet_max_chars=self.snippet_max_chars,
            publication_date_source=self.publication_date_source,
        )
        trace_event("web_search", "search_results", payload={
            "provider": self.provider_name,
            "returned_count": len(result.results), "omitted_count": result.omitted_count,
            "discarded_count": result.discarded_count, "warnings": result.warnings,
        })
        return result

    def _search_params(self, query: str, limit: int, time_range: str | None) -> dict:
        raise NotImplementedError

    def _http_request(self, params: dict) -> tuple[str, str, dict]:
        raise NotImplementedError

    def _normalize_payload(self, payload: object) -> dict:
        raise NotImplementedError

    def _start_worker(self):
        with self._state_lock:
            if self._closed:
                raise WebSearchError("unavailable", "Web search worker is closed; restart Astra to restore search.")
            if self._thread is None:
                ready = Event()

                def run_loop():
                    try:
                        self._loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(self._loop)
                    except Exception as error:
                        self._worker_error = error
                        ready.set()
                        return
                    ready.set()
                    try:
                        self._loop.run_forever()
                    finally:
                        self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                        self._loop.close()

                self._thread = Thread(target=run_loop, name="astra-web-search", daemon=True)
                self._thread.start()
                ready.wait()
            if self._worker_error is not None or not self._thread.is_alive():
                logger.error("Web search worker could not start: %s", self._worker_error)
                raise WebSearchError("unavailable", "Web search worker could not start; check the application logs.")

    def _request(self, params: dict, budget: float, retries: int) -> dict:
        deadline = time.monotonic() + budget
        if not self._slot.acquire(timeout=budget):
            raise WebSearchError("timeout", "Web search deadline expired waiting for another search.", retryable=True)
        try:
            self._start_worker()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebSearchError("timeout", "Web search deadline expired before dispatch.", retryable=True)
            with self._state_lock:
                if self._closed:
                    raise WebSearchError("unavailable", "Web search worker is closed.")
                self._future = asyncio.run_coroutine_threadsafe(self._bounded_fetch(params, deadline, retries), self._loop)
                future = self._future
            try:
                return future.result(timeout=remaining + 1.0)
            except FutureTimeoutError as error:
                future.cancel()
                self.close()
                raise WebSearchError("unavailable", "Web search cleanup could not be confirmed; worker stopped. Restart Astra before retrying.") from error
            except FutureCancelledError as error:
                raise WebSearchError("unavailable", "Web search stopped during application shutdown.") from error
        finally:
            with self._state_lock:
                self._future = None
            self._slot.release()

    async def _bounded_fetch(self, params: dict, deadline: float, retries: int) -> dict:
        try:
            return await asyncio.wait_for(self._fetch_with_retry(params, deadline, retries), max(0.0, deadline - time.monotonic()))
        except asyncio.TimeoutError as error:
            raise WebSearchError("timeout", "Web search exceeded its total deadline; retry with a narrower query.", retryable=True) from error

    async def _fetch_with_retry(self, params: dict, deadline: float, retries: int) -> dict:
        for attempt in range(retries + 1):
            delay = self.retry_backoff_s * 2**attempt
            try:
                async with httpx.AsyncClient(
                    transport=self._transport, timeout=self.timeout, follow_redirects=False,
                    trust_env=False, headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as client:
                    method, url, options = self._http_request(params)
                    async with client.stream(method, url, **options) as response:
                        if response.status_code == 429:
                            delay = max(delay, self._retry_after(response.headers.get("Retry-After")))
                            raise WebSearchError("rate_limited", f"{self.provider_name} rate limited the search or exhausted its allowance; try again later.", retryable=True)
                        if response.status_code >= 500:
                            raise WebSearchError("unavailable", f"{self.provider_name} returned HTTP {response.status_code}; try again later.", retryable=True)
                        if response.status_code != 200:
                            raise WebSearchError("http_error", f"{self.provider_name} returned HTTP {response.status_code}; check provider access and configuration.")
                        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                        if encoding not in {"", "identity"}:
                            raise WebSearchError("invalid_response", f"{self.provider_name} ignored the identity encoding request; uncompressed JSON is required.")
                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                        if content_type and content_type != "application/json" and not (content_type.startswith("application/") and content_type.endswith("+json")):
                            raise WebSearchError("invalid_response", f"{self.provider_name} returned non-JSON content; JSON search output is required.")
                        length = response.headers.get("Content-Length", "")
                        if length.isdigit() and (len(length) > 20 or int(length) > self.max_response_bytes):
                            raise WebSearchError("response_too_large", f"{self.provider_name} response exceeds the configured download limit.")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > self.max_response_bytes:
                                raise WebSearchError("response_too_large", f"{self.provider_name} response exceeds the configured download limit.")
                            body.extend(chunk)
                        try:
                            payload = json.loads(body)
                        except (ValueError, UnicodeError, RecursionError) as error:
                            raise WebSearchError("invalid_response", f"{self.provider_name} returned malformed JSON.") from error
                        payload = self._normalize_payload(payload)
                        if time.monotonic() >= deadline:
                            raise WebSearchError("timeout", "Web search exceeded its total deadline.", retryable=True)
                        return payload
            except httpx.TimeoutException as error:
                failure = WebSearchError("timeout", f"{self.provider_name} timed out; try again later.", retryable=True)
                failure.__cause__ = error
            except httpx.TransportError as error:
                failure = WebSearchError("unavailable", f"Cannot reach {self.provider_name}; check the search service and connection.", retryable=True)
                failure.__cause__ = error
            except WebSearchError as error:
                failure = error
            if not failure.retryable or attempt == retries:
                raise failure
            if delay >= deadline - time.monotonic():
                raise failure
            logger.warning("Web search attempt %d failed (%s); retrying in %.2fs", attempt + 1, failure.category, delay)
            await asyncio.sleep(delay)
        raise RuntimeError("Search retry loop did not produce an outcome")

    def _retry_after(self, value: str | None) -> float:
        if not value:
            return 0.0
        try:
            if value.strip().isdigit():
                delay = float(value)
            else:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0.0
        return min(self.max_retry_after_s, max(0.0, delay))

    def close(self):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is None or loop.is_closed() or thread is None:
            return

        async def cancel_pending():
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1.0)

        try:
            asyncio.run_coroutine_threadsafe(cancel_pending(), loop).result(timeout=2.0)
        except Exception:
            logger.exception("Web search worker cleanup failed")
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=3.0)
        if thread.is_alive():
            logger.error("Web search worker failed to stop; further requests are disabled")


class SearXNGClient(BoundedSearchClient):
    provider_name = "SearXNG"
    publication_date_source = "searxng.publishedDate"

    def _search_params(self, query: str, limit: int, time_range: str | None) -> dict:
        params = {"q": query, "format": "json"}
        if time_range is not None:
            params["time_range"] = time_range
        return params

    def _http_request(self, params: dict) -> tuple[str, str, dict]:
        return "GET", f"{self.base_url}/search", {"params": params}

    def _normalize_payload(self, payload: object) -> dict:
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise WebSearchError("invalid_response", "SearXNG JSON must contain a results list.")
        return payload


class WebSearchTool:
    description = (
        "Discover relevant source URLs when no suitable source is known. If a suitable URL was supplied, "
        "start with web__open instead. Returns source URLs and bounded search snippets, "
        "not read-page evidence. Cite supporting URLs and report missing or failed evidence honestly."
    )

    def __init__(self, client: BoundedSearchClient, max_results: int = 5, max_output_chars: int = 8000):
        self.client = client
        self.max_results = max_results
        self.max_output_chars = max_output_chars

    def run(self, query: str, time_range: str | None = None) -> str:
        query = self.client.validate_query(query, time_range)
        response = self.client.search(query, limit=self.max_results, time_range=time_range)
        result = format_search(response, query, self.max_output_chars)
        trace_event("web_search", "tool_results", payload={"output_chars": len(result)})
        return result

    def close(self):
        self.client.close()
