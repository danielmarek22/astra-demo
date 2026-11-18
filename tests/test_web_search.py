import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
import json
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

import httpx

from app.integrations import CapabilityId, IntegrationRegistry, InvocationContext, ToolCall, WebIntegration
from app.tools.web.contracts import SearchResponse, WebSearchError, WebSearchResult
from app.tools.web.formatting import format_search, source_url
from app.tools.web_search import SearXNGClient, WebSearchTool


STAMP = "2026-10-26T12:00:00+00:00"
SOURCE = {"title": "Source title", "url": "https://example.org/article?version=1", "content": "Source snippet"}


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            self.yielded += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class WebSearchTests(unittest.TestCase):
    def client(self, handler=None, **settings):
        settings.setdefault("max_retries", 0)
        handler = handler or (lambda _request: httpx.Response(200, json={"results": [SOURCE]}))
        client = SearXNGClient(transport=httpx.MockTransport(handler), **settings)
        self.addCleanup(client.close)
        return client

    def test_structured_sources_keep_links_dates_and_provenance_without_inference(self):
        client = self.client(lambda _request: httpx.Response(200, json={
            "results": [{**SOURCE, "publishedDate": "2026-10-20T08:00:00Z"}],
        }))
        tool = WebSearchTool(client)
        self.assertFalse(hasattr(tool, "summarizer"))
        result = json.loads(tool.run("  Polish\n research  ", time_range="month"))
        source = result["results"][0]
        self.assertEqual(result["kind"], "search_results")
        self.assertEqual(result["query"], "Polish research")
        self.assertEqual(source["content_kind"], "search_snippet")
        self.assertEqual(source["url"], SOURCE["url"])
        self.assertEqual(source["snippet"], SOURCE["content"])
        self.assertEqual(source["published_at"], "2026-10-20T08:00:00Z")
        self.assertEqual(source["publication_date_source"], "searxng.publishedDate")
        self.assertEqual(source["retrieved_at"], result["retrieved_at"])
        self.assertIsNotNone(datetime.fromisoformat(source["retrieved_at"]).tzinfo)
        self.assertTrue(source["source_id"].startswith("search_"))

    def test_empty_search_is_successful_and_does_not_invent_a_date(self):
        client = self.client(lambda _request: httpx.Response(200, json={"results": []}))
        registry = IntegrationRegistry([WebIntegration(WebSearchTool(client))])
        result = registry.invoke(ToolCall(CapabilityId("web", "search"), {"query": "nothing"}), InvocationContext("session-a", "search"))
        self.assertEqual(result.status.value, "success")
        self.assertEqual(json.loads(result.content)["results"], [])
        self.assertEqual(json.loads(result.content)["returned_count"], 0)
        ordinary = json.loads(WebSearchTool(self.client()).run("ordinary"))["results"][0]
        self.assertNotIn("published_at", ordinary)

    def test_queries_and_time_filters_are_validated_before_dispatch(self):
        client = self.client(query_max_chars=8)
        for query, time_range in (("", None), ("  ", None), ("a" * 9, None), ("nul\x00", None), (True, None), ("valid", "week"), ("valid", {})):
            with self.subTest(query=query, time_range=time_range), self.assertRaises(WebSearchError) as raised:
                client.search(query, time_range=time_range)
            self.assertEqual(raised.exception.category, "invalid_arguments")
        self.assertIsNone(client._thread)

    def test_time_range_and_identity_encoding_reach_provider(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(200, json={"results": []})

        client = self.client(handle, base_url="http://localhost:8080/searx/")
        client.search("test", time_range="year")
        self.assertEqual(calls[0].url.path, "/searx/search")
        self.assertEqual(dict(calls[0].url.params), {"q": "test", "format": "json", "time_range": "year"})
        self.assertEqual(calls[0].headers["accept-encoding"], "identity")

    def test_malformed_json_and_wrong_result_shapes_are_explicit_failures(self):
        for body in (b"not JSON", b"[]", b"{}", b'{"results":null}', b'{"results":{}}', b'[' * 5000):
            with self.subTest(body=body[:30]):
                client = self.client(lambda _request: httpx.Response(200, content=body))
                with self.assertRaises(WebSearchError) as raised:
                    client.search("test")
                self.assertEqual(raised.exception.category, "invalid_response")

    def test_probe_checks_status_and_shape_and_recovers_on_later_call(self):
        for response in (httpx.Response(503), httpx.Response(200, content=b"not JSON"), httpx.Response(200, json={})):
            with self.subTest(status=response.status_code):
                outcomes = [response, httpx.Response(200, json={"results": [SOURCE]})]
                client = self.client(lambda _request: outcomes.pop(0))
                registry = IntegrationRegistry([WebIntegration(WebSearchTool(client))])
                self.assertFalse(client.probe())
                self.assertEqual(len(registry.get_native_tools()), 1)
                result = registry.invoke(ToolCall(CapabilityId("web", "search"), {"query": "test"}), InvocationContext("session-a", "test"))
                self.assertEqual(result.status.value, "success")
                self.assertTrue(client.is_available)

    def test_probe_accepts_valid_empty_response_without_retrying_an_outage(self):
        self.assertTrue(self.client(lambda _request: httpx.Response(200, json={"results": []})).probe())
        calls = []

        def offline(request):
            calls.append(request)
            raise httpx.ConnectError("offline")

        self.assertFalse(self.client(offline, max_retries=5).probe())
        self.assertEqual(len(calls), 1)

    def test_invalid_records_are_skipped_and_queries_are_not_stripped_from_urls(self):
        payload = {"results": [None, "invalid", {"url": "javascript:alert(1)"}, {"url": "https://example.org", "title": {}},
                               SOURCE, {**SOURCE, "url": "HTTPS://EXAMPLE.ORG:443/article?version=1"},
                               {**SOURCE, "url": "https://example.org/article?version=2"}]}
        result = self.client(lambda _request: httpx.Response(200, json=payload)).search("test")
        self.assertEqual([record.url for record in result.results], [SOURCE["url"], "https://example.org/article?version=2"])
        self.assertEqual(result.discarded_count, 5)
        self.assertTrue(result.warnings)

    def test_invalid_and_overlong_urls_are_rejected_without_truncation(self):
        for url in (None, 12, "https://", "https://.", "https://user:secret@example.org/", "file:///tmp/secret", "http://example.org:invalid", "https://example.org/a b", "https://exa\nmple.org", "http://example.org\\evil", "https://example.org/" + "x" * 300):
            with self.subTest(url=url):
                self.assertIsNone(source_url(url, 128))
        self.assertEqual(source_url("http://[2001:db8::1]:80/path?a=1#part", 128), "http://[2001:db8::1]/path?a=1#part")

    def test_text_is_cleaned_and_capped_without_losing_source_link(self):
        payload = {"results": [{**SOURCE, "title": "<b>Żółć &amp; text</b><script>secret</script>",
                               "content": "<style>secret</style><p>Żółć\n " + "x" * 100 + "</p>"}]}
        client = self.client(lambda _request: httpx.Response(200, json=payload), title_max_chars=8, snippet_max_chars=12)
        result = json.loads(WebSearchTool(client).run("test"))
        source = result["results"][0]
        self.assertEqual(source["title"], "Żółć & t")
        self.assertEqual(len(source["snippet"]), 12)
        self.assertNotIn("secret", source["snippet"])
        self.assertNotIn("<", source["snippet"])
        self.assertEqual(source["url"], SOURCE["url"])
        self.assertTrue(source["truncated"])
        self.assertTrue(result["truncated"])

    def test_result_count_and_partial_provider_failures_remain_bounded(self):
        payload = {"results": [{**SOURCE, "url": f"https://example.org/{index}"} for index in range(9)],
                   "unresponsive_engines": [["engine" * 100, "rate limited" * 100]] * 10, "private_provider_field": "DO_NOT_RETURN"}
        client = self.client(lambda _request: httpx.Response(200, json=payload))
        result = json.loads(WebSearchTool(client, max_results=2).run("test"))
        self.assertEqual(result["returned_count"], 2)
        self.assertEqual(result["omitted_count"], 7)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["warnings"]), 4)
        self.assertLess(sum(len(warning) for warning in result["warnings"]), 800)
        self.assertNotIn("DO_NOT_RETURN", json.dumps(result))

    def test_all_invalid_results_are_distinguished_from_provider_empty_result(self):
        payload = {"results": [{"url": "file:///etc/passwd"}]}
        result = self.client(lambda _request: httpx.Response(200, json=payload)).search("test")
        self.assertEqual(result.results, ())
        self.assertEqual(result.discarded_count, 1)
        self.assertTrue(any("none were usable" in warning for warning in result.warnings))

    def test_serialized_output_accounts_for_escaping_and_keeps_complete_records(self):
        records = tuple(WebSearchResult("Title", f"https://example.org/{index}", 'Quote " and newline\n' * 10) for index in range(10))
        response = SearchResponse(records, STAMP)
        for maximum in (1024, 1800, 3000):
            with self.subTest(maximum=maximum):
                text = format_search(response, "test", maximum)
                self.assertLessEqual(len(text), maximum)
                result = json.loads(text)
                self.assertGreater(result["returned_count"], 0)
                self.assertEqual(result["returned_count"] + result["omitted_count"], 10)
                for source in result["results"]:
                    self.assertEqual(source["retrieved_at"], STAMP)
                    self.assertTrue(source["source_id"])
                    self.assertTrue(source["url"].startswith("https://"))

    def test_large_records_are_omitted_without_cutting_their_urls_or_json(self):
        response = SearchResponse((WebSearchResult("Title", "https://example.org/" + "x" * 2000, "snippet"),), STAMP)
        result = json.loads(format_search(response, "test", 1024))
        self.assertEqual(result["results"], [])
        self.assertEqual(result["omitted_count"], 1)
        self.assertTrue(result["truncated"])

    def test_warning_metadata_cannot_overflow_output_and_ids_are_not_reused(self):
        response = SearchResponse((), STAMP, warnings=("x" * 2000,))
        text = format_search(response, "test", 1024)
        self.assertLessEqual(len(text), 1024)
        self.assertTrue(json.loads(text)["truncated"])
        response = SearchResponse((WebSearchResult("Title", SOURCE["url"], "snippet"),), STAMP)
        first = json.loads(format_search(response, "test", 1024))["results"][0]["source_id"]
        second = json.loads(format_search(response, "test", 1024))["results"][0]["source_id"]
        self.assertNotEqual(first, second)

    def test_streaming_limit_stops_collection_and_closes_the_response(self):
        stream = TrackedStream([b"x" * 512] * 10)
        client = self.client(lambda _request: httpx.Response(200, stream=stream), max_response_bytes=1024)
        with self.assertRaises(WebSearchError) as raised:
            client.search("test")
        self.assertEqual(raised.exception.category, "response_too_large")
        self.assertEqual(stream.yielded, 3)
        self.assertTrue(stream.closed)

    def test_content_length_rejects_oversized_body_before_streaming(self):
        stream = TrackedStream([b"x"])
        client = self.client(lambda _request: httpx.Response(200, headers={"Content-Length": "2000"}, stream=stream), max_response_bytes=1024)
        with self.assertRaises(WebSearchError) as raised:
            client.search("test")
        self.assertEqual(raised.exception.category, "response_too_large")
        self.assertEqual(stream.yielded, 0)
        self.assertTrue(stream.closed)

    def test_html_compression_and_redirects_are_rejected_before_reading(self):
        for status, headers, category in ((200, {"Content-Type": "text/html"}, "invalid_response"),
                                           (200, {"Content-Encoding": "gzip"}, "invalid_response"),
                                           (302, {"Location": "http://127.0.0.1/private"}, "http_error")):
            with self.subTest(headers=headers):
                stream = TrackedStream([b"body"])
                client = self.client(lambda _request: httpx.Response(status, headers=headers, stream=stream))
                with self.assertRaises(WebSearchError) as raised:
                    client.search("test")
                self.assertEqual(raised.exception.category, category)
                self.assertEqual(stream.yielded, 0)
                self.assertTrue(stream.closed)

    def test_429_retry_after_is_clamped_and_followed_by_success(self):
        outcomes = [httpx.Response(429, headers={"Retry-After": "999999"}), httpx.Response(200, json={"results": []})]
        calls = []

        def handle(request):
            calls.append(request)
            return outcomes.pop(0)

        client = self.client(handle, max_retries=1, retry_backoff_s=0, max_retry_after_s=0.02)
        started = time.monotonic()
        self.assertEqual(client.search("test").results, ())
        self.assertGreaterEqual(time.monotonic() - started, 0.018)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(calls), 2)

    def test_retry_after_dates_invalid_values_and_exhaustion(self):
        client = self.client(max_retry_after_s=2)
        fixed = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        with patch("app.tools.web_search.datetime") as clock:
            clock.now.return_value = fixed
            self.assertEqual(client._retry_after(format_datetime(fixed.replace(second=30))), 2)
        for value in ("invalid", "-10", "", None):
            self.assertEqual(client._retry_after(value), 0)
        limited = self.client(lambda _request: httpx.Response(429))
        with self.assertRaises(WebSearchError) as raised:
            limited.search("test")
        self.assertEqual(raised.exception.category, "rate_limited")
        self.assertTrue(raised.exception.retryable)

    def test_backoff_that_cannot_fit_does_not_start_another_request(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(503)

        client = self.client(handle, max_retries=20, retry_backoff_s=2, total_timeout_s=0.05)
        started = time.monotonic()
        with self.assertRaises(WebSearchError):
            client.search("test")
        self.assertEqual(len(calls), 1)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_total_deadline_cancels_slow_headers_and_allows_later_recovery(self):
        cancelled = Event()
        calls = []

        async def handle(request):
            calls.append(request)
            if len(calls) == 1:
                try:
                    await asyncio.sleep(2)
                finally:
                    cancelled.set()
            return httpx.Response(200, json={"results": []})

        client = self.client(handle, total_timeout_s=0.05)
        started = time.monotonic()
        with self.assertRaises(WebSearchError) as raised:
            client.search("test")
        self.assertEqual(raised.exception.category, "timeout")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(client.search("recovery").results, ())

    def test_total_deadline_cancels_dripping_body_and_closes_stream(self):
        stream = TrackedStream([b" "] * 100, delay=0.01)
        client = self.client(lambda _request: httpx.Response(200, stream=stream), total_timeout_s=0.05, timeout=1.0)
        started = time.monotonic()
        with self.assertRaises(WebSearchError) as raised:
            client.search("test")
        self.assertEqual(raised.exception.category, "timeout")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(stream.closed)
        self.assertLess(stream.yielded, 100)

    def test_waiting_for_another_search_is_included_in_deadline(self):
        entered = Event()

        async def handle(_request):
            entered.set()
            await asyncio.sleep(0.15)
            return httpx.Response(200, json={"results": []})

        client = self.client(handle, total_timeout_s=0.5)
        with ThreadPoolExecutor(max_workers=1) as callers:
            running = callers.submit(client.search, "first")
            self.assertTrue(entered.wait(1))
            client.total_timeout_s = 0.03
            with self.assertRaises(WebSearchError) as raised:
                client.search("second")
            self.assertIn("waiting", str(raised.exception))
            self.assertEqual(running.result(timeout=1).results, ())

    def test_close_is_repeatable_and_stops_worker_and_active_request(self):
        entered, cancelled = Event(), Event()

        async def handle(_request):
            entered.set()
            try:
                await asyncio.sleep(5)
            finally:
                cancelled.set()
            return httpx.Response(200, json={"results": []})

        client = self.client(handle)
        with ThreadPoolExecutor(max_workers=1) as callers:
            future = callers.submit(client.search, "test")
            self.assertTrue(entered.wait(1))
            client.close()
            client.close()
            with self.assertRaises(WebSearchError) as raised:
                future.result(timeout=1)
            self.assertEqual(raised.exception.category, "unavailable")
        self.assertTrue(cancelled.is_set())
        self.assertFalse(client._thread.is_alive())
        with self.assertRaises(WebSearchError):
            client.search("closed")

    def test_worker_start_failure_is_actionable_and_close_without_start_is_safe(self):
        client = self.client()
        with patch("app.tools.web_search.asyncio.new_event_loop", side_effect=PermissionError("blocked")):
            with self.assertRaises(WebSearchError) as raised:
                client.search("test")
        self.assertEqual(raised.exception.category, "unavailable")
        self.assertFalse(client._thread.is_alive())
        unopened = self.client()
        unopened.close()
        unopened.close()
        self.assertIsNone(unopened._thread)

    def test_registry_reports_expected_failures_and_enforces_authority_and_schema(self):
        client = self.client(lambda _request: httpx.Response(429))
        registry = IntegrationRegistry([WebIntegration(WebSearchTool(client))])
        call = ToolCall(CapabilityId("web", "search"), {"query": "test"})
        denied = registry.invoke(call, InvocationContext("session-a", "event", allowed_capabilities=frozenset()))
        self.assertEqual(denied.status.value, "denied")
        self.assertIsNone(client._thread)
        for arguments in ({"query": "x" * 513}, {"query": "test", "time_range": "week"}, {"query": "test", "session_id": "other"}):
            rejected = registry.invoke(ToolCall(call.capability, arguments), InvocationContext("session-a", "test"))
            self.assertEqual(rejected.status.value, "error")
            self.assertIsNone(client._thread)
        result = registry.invoke(call, InvocationContext("session-a", "test"))
        self.assertEqual(result.status.value, "error")
        self.assertEqual(result.diagnostics, {"category": "rate_limited", "retryable": True, "suppress_for_turn": True})
        self.assertEqual(len(registry.get_native_tools()), 1)
        registry.close()
        self.assertFalse(client._thread.is_alive())

    def test_factory_wires_search_limits_and_shutdown_without_a_summarization_call(self):
        from app.config import Config
        from app.core import orchestrator_factory
        from app.storage.database import Database
        from tests.test_memory_store import FakeVectorStore

        database = Database(":memory:")
        self.addCleanup(database.close)
        vectors = FakeVectorStore()
        vectors.close = Mock()
        client = self.client(query_max_chars=64)
        client.probe = Mock(return_value=False)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as config_file:
            config_file.write("context:\n  prompt_budget:\n    enabled: true\n    context_window_tokens: 16000\nintegrations:\n  web:\n    enabled: true\n    query_max_chars: 64\n    max_results: 3\n    max_output_chars: 2048\n  scratchpad:\n    enabled: false\n")
            config_file.flush()
            config = Config(config_file.name)
        with patch.object(orchestrator_factory, "Database", return_value=database), \
                patch.object(orchestrator_factory, "VectorStore", return_value=vectors), \
                patch.object(orchestrator_factory, "OllamaClient") as llm_factory, \
                patch.object(orchestrator_factory, "SearXNGClient", return_value=client) as search_factory:
            orchestrator = orchestrator_factory.build_orchestrator(config)
        self.addCleanup(orchestrator.close)
        self.assertEqual(search_factory.call_args.kwargs["query_max_chars"], 64)
        self.assertEqual(search_factory.call_args.kwargs["max_response_bytes"], 1048576)
        self.assertEqual(search_factory.call_args.kwargs["total_timeout_s"], 15.0)
        client.probe.assert_not_called()
        self.assertIsNone(client._thread)
        integration = orchestrator.tool_executor.registry.get_integration("web")
        self.assertEqual(orchestrator.prompt_budget.context_window_tokens, 16000)
        self.assertIs(orchestrator._response_generator().prompt_budget, orchestrator.prompt_budget)
        self.assertEqual(integration.tool.max_results, 3)
        self.assertEqual(integration.tool.max_output_chars, 2048)
        result = integration._search({"query": "test"}, InvocationContext("session-a", "test"))
        self.assertEqual(result.status.value, "success")
        source = json.loads(result.content)["results"][0]
        self.assertEqual(orchestrator.web_sources.get("session-a", source["source_id"]).url, source["url"])
        self.assertIsNone(integration.reader_service.backend)
        self.assertEqual(len(integration.registered_tools()), 1)
        llm_factory.return_value.stream_chat.assert_not_called()
        llm_factory.return_value.chat_buffered_result.assert_not_called()
        orchestrator.close()
        self.assertFalse(client._thread.is_alive())
        self.assertFalse(orchestrator.web_sources._entries)

    def test_factory_registers_page_tools_only_with_validated_activation_settings(self):
        from app.config import Config
        from app.core import orchestrator_factory
        from app.storage.database import Database
        from tests.test_memory_store import FakeVectorStore

        database = Database(":memory:")
        self.addCleanup(database.close)
        vectors = FakeVectorStore()
        vectors.close = Mock()
        client = self.client()
        client.probe = Mock(return_value=True)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as config_file:
            config_file.write("llm:\n  generation:\n    num_ctx: 16384\ncontext:\n  prompt_budget:\n    enabled: true\nintegrations:\n  web:\n    enabled: true\n    reader:\n      enabled: true\n      max_cpu_percent: 150\n      max_tasks: 200\n  scratchpad:\n    enabled: false\n")
            config_file.flush()
            config = Config(config_file.name)
        backend = Mock()
        with patch.object(orchestrator_factory, "Database", return_value=database), \
                patch.object(orchestrator_factory, "VectorStore", return_value=vectors), \
                patch.object(orchestrator_factory, "OllamaClient"), \
                patch.object(orchestrator_factory, "SearXNGClient", return_value=client), \
                patch.object(orchestrator_factory, "Crawl4AIReader", return_value=backend) as reader_factory:
            orchestrator = orchestrator_factory.build_orchestrator(config)
        self.addCleanup(orchestrator.close)
        integration = orchestrator.tool_executor.registry.get_integration("web")
        self.assertEqual({str(tool.spec.capability) for tool in integration.registered_tools()},
                         {"web__search", "web__open", "web__find"})
        self.assertEqual(reader_factory.call_args.kwargs["max_cpu_percent"], 150)
        self.assertEqual(reader_factory.call_args.kwargs["max_tasks"], 200)
        self.assertIsNotNone(orchestrator.prompt_budget)
        orchestrator.close()
        backend.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
