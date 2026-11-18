import json
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from app.integrations import InvocationContext
from app.tools.langsearch import LangSearchClient
from app.tools.web.contracts import WebSearchError
from app.tools.web_search import WebSearchTool
from tests.test_web_search import TrackedStream


def response(values):
    return {"code": 200, "data": {"webPages": {"value": values}}}


class LangSearchTests(unittest.TestCase):
    def client(self, handler, key="test-private-token", **settings):
        with patch.dict("os.environ", {"ASTRA_TEST_SEARCH_KEY": key}):
            client = LangSearchClient(api_key_env="ASTRA_TEST_SEARCH_KEY", transport=httpx.MockTransport(handler), **settings)
        self.addCleanup(client.close)
        return client

    def test_snippets_dates_and_sources_use_existing_tool_contract(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json=response([
                {"name": "<b>Source</b>", "url": "https://example.org/", "snippet": "Useful text", "datePublished": "2026-10-20"},
                {"name": "No snippet", "url": "https://example.org/other"},
                {"url": "javascript:alert(1)"},
                {"url": "https://example.org/", "snippet": "duplicate"},
            ]))

        client = self.client(handle, base_url="https://untrusted.example", max_retries=20)
        self.assertFalse(client.probe())
        self.assertEqual(requests, [])
        with patch("app.tools.web_search.trace_event") as trace:
            result = json.loads(WebSearchTool(client, max_results=3).run("  local\n robots ", "month"))
        self.assertNotIn("test-private-token", repr(trace.call_args_list))
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://api.langsearch.com/v1/web-search")
        self.assertEqual(request.headers["authorization"], "Bearer test-private-token")
        self.assertEqual(request.headers["accept-encoding"], "identity")
        self.assertEqual(json.loads(request.content), {
            "query": "local robots", "count": 3, "freshness": "oneMonth", "contents": {"text": False},
        })
        self.assertEqual(result["returned_count"], 2)
        self.assertEqual(result["discarded_count"], 2)
        source = result["results"][0]
        self.assertEqual(source["title"], "Source")
        self.assertEqual(source["snippet"], "Useful text")
        self.assertEqual(source["content_kind"], "search_snippet")
        self.assertEqual(source["publication_date_source"], "langsearch.datePublished")
        self.assertEqual(result["results"][1]["snippet"], "")
        self.assertNotIn("published_at", result["results"][1])

    def test_empty_results_and_supported_freshness_filters(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=response([]))

        client = self.client(handle)
        for time_range, freshness in ((None, "noLimit"), ("day", "oneDay"), ("year", "oneYear")):
            with self.subTest(time_range=time_range):
                self.assertEqual(client.search("query", time_range=time_range).results, ())
                self.assertEqual(requests[-1]["freshness"], freshness)

    def test_missing_or_invalid_key_fails_before_dispatch_without_exposing_it(self):
        for key in ("", "secret\ninjected", "non-ascii-ą"):
            handler = Mock()
            client = self.client(handler, key=key)
            with self.subTest(key=key), self.assertRaises(WebSearchError) as raised:
                client.search("robots")
            self.assertEqual(raised.exception.category, "unavailable")
            self.assertIn("ASTRA_TEST_SEARCH_KEY", str(raised.exception))
            self.assertFalse(client.is_configured)
            self.assertIsNone(client._thread)
            handler.assert_not_called()

    def test_http_failures_do_not_retry_follow_redirects_or_echo_secrets(self):
        for status, category in ((401, "http_error"), (403, "http_error"), (429, "rate_limited"),
                                 (503, "unavailable"), (302, "http_error")):
            calls = []

            def handle(request):
                calls.append(request)
                return httpx.Response(status, headers={"Location": "https://elsewhere.example", "Retry-After": "600"},
                                      json={"message": "test-private-token"})

            client = self.client(handle, max_retries=20)
            with self.subTest(status=status), self.assertRaises(WebSearchError) as raised:
                client.search("query")
            self.assertEqual(raised.exception.category, category)
            self.assertNotIn("test-private-token", str(raised.exception))
            self.assertEqual(len(calls), 1)

    def test_invalid_envelopes_fail_instead_of_becoming_empty_success(self):
        for payload in ([], {"code": 429, "data": {"webPages": {"value": []}}},
                        {"code": 200}, {"code": 200, "data": []},
                        {"code": 200, "data": {"webPages": {"value": {}}}}):
            client = self.client(lambda _request: httpx.Response(200, json=payload))
            with self.subTest(payload=payload), self.assertRaises(WebSearchError) as raised:
                client.search("query")
            self.assertEqual(raised.exception.category, "invalid_response")

    def test_download_and_field_limits_apply_to_hosted_provider(self):
        stream = TrackedStream([b"a" * 600, b"b" * 600, b"never"])
        client = self.client(lambda _request: httpx.Response(200, stream=stream), max_response_bytes=1024)
        with self.assertRaises(WebSearchError) as raised:
            client.search("query")
        self.assertEqual(raised.exception.category, "response_too_large")
        self.assertEqual(stream.yielded, 2)
        self.assertTrue(stream.closed)
        client = self.client(lambda _request: httpx.Response(200, json=response([
            {"name": "a" * 100, "url": "https://example.org/", "snippet": "b" * 100},
        ])), title_max_chars=10, snippet_max_chars=20)
        result = client.search("query")
        self.assertEqual(result.results[0].title, "a" * 10)
        self.assertEqual(result.results[0].content, "b" * 20)
        self.assertTrue(result.truncated)

    def test_dripping_response_is_cancelled_at_total_deadline(self):
        stream = TrackedStream([b" "] * 50, delay=0.02)
        client = self.client(lambda _request: httpx.Response(200, stream=stream), total_timeout_s=0.08)
        with self.assertRaises(WebSearchError) as raised:
            client.search("query")
        self.assertEqual(raised.exception.category, "timeout")
        self.assertLess(stream.yielded, 50)
        self.assertTrue(stream.closed)

    def test_factory_selects_langsearch_without_startup_search_or_searxng(self):
        from app.config import Config
        from app.core import orchestrator_factory
        from app.storage.database import Database
        from tests.test_memory_store import FakeVectorStore

        database = Database(":memory:")
        self.addCleanup(database.close)
        vectors = FakeVectorStore()
        vectors.close = Mock()
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json=response([{"name": "Source", "url": "https://example.org/", "snippet": "text"}]))

        client = self.client(handle)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as config_file:
            config_file.write("integrations:\n  web:\n    enabled: true\n    provider: langsearch\n  scratchpad:\n    enabled: false\n")
            config_file.flush()
            config = Config(config_file.name)
        with patch.object(orchestrator_factory, "Database", return_value=database), \
                patch.object(orchestrator_factory, "VectorStore", return_value=vectors), \
                patch.object(orchestrator_factory, "OllamaClient"), \
                patch.object(orchestrator_factory, "LangSearchClient", return_value=client) as factory, \
                patch.object(orchestrator_factory, "SearXNGClient") as searxng:
            orchestrator = orchestrator_factory.build_orchestrator(config)
        self.addCleanup(orchestrator.close)
        searxng.assert_not_called()
        self.assertEqual(factory.call_args.kwargs["api_key_env"], "LANGSEARCH_API_KEY")
        self.assertEqual(factory.call_args.kwargs["max_response_bytes"], 1048576)
        self.assertEqual(requests, [])
        integration = orchestrator.tool_executor.registry.get_integration("web")
        result = integration._search({"query": "query"}, InvocationContext("session-a", "test"))
        self.assertEqual(result.status.value, "success")
        source = json.loads(result.content)["results"][0]
        self.assertEqual(orchestrator.web_sources.get("session-a", source["source_id"]).url, source["url"])
        orchestrator.close()
