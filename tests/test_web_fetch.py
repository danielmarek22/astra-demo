import asyncio
import unittest

import httpx

from app.tools.web.contracts import WebReaderError
from app.tools.web.fetch import PublicPageFetcher, public_address


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def response(body=b"<h1>Guide</h1>", status=200, **headers):
    return httpx.Response(status, headers={"Content-Type": "text/html", **headers}, stream=Body([body]))


async def public_resolver(_host, _port):
    return ["93.184.215.14"]


class PublicFetchTests(unittest.IsolatedAsyncioTestCase):
    def fetcher(self, handler=None, **settings):
        settings.setdefault("resolver", public_resolver)
        return PublicPageFetcher(transport=httpx.MockTransport(handler or (lambda _request: response())), **settings)

    async def category(self, expected, fetcher, url="https://example.org/guide", **options):
        with self.assertRaises(WebReaderError) as raised:
            await fetcher.fetch(url, **options)
        self.assertEqual(raised.exception.category, expected)

    def test_public_ip_classification_rejects_special_and_transition_ranges(self):
        for address in ("127.0.0.1", "0.0.0.0", "10.1.2.3", "169.254.169.254", "192.168.1.1",
                        "100.64.0.1", "192.0.2.1", "198.18.0.1", "224.0.0.1", "255.255.255.255",
                        "::1", "::", "fe80::1", "fc00::1", "ff02::1", "2001:db8::1",
                        "::ffff:8.8.8.8", "2002:0808:0808::1", "fe80::1%lo", "not-an-ip"):
            with self.subTest(address=address):
                self.assertFalse(public_address(address))
        self.assertTrue(public_address("93.184.215.14"))
        self.assertTrue(public_address("2606:4700:4700::1111"))

    async def test_connection_is_ip_pinned_with_original_host_and_tls_name_without_credentials(self):
        seen = []

        def handle(request):
            seen.append(request)
            return response()

        fetcher = self.fetcher(handle)
        page = await fetcher.fetch("https://example.org/guide#section", document=True)
        self.assertEqual(page.url, "https://example.org/guide#section")
        self.assertEqual(str(seen[0].url), "https://93.184.215.14/guide")
        self.assertEqual(seen[0].headers["Host"], "example.org")
        self.assertEqual(seen[0].extensions["sni_hostname"], "example.org")
        self.assertEqual(seen[0].headers["Accept-Encoding"], "identity")
        self.assertNotIn("Cookie", seen[0].headers)
        self.assertNotIn("Authorization", seen[0].headers)

    async def test_private_mixed_empty_and_excessive_dns_answers_never_connect(self):
        for addresses in (["127.0.0.1"], ["93.184.215.14", "10.0.0.1"], [], ["8.8.8.8"] * 33):
            calls = []

            async def resolve(_host, _port):
                return addresses

            await self.category("blocked_address", self.fetcher(lambda request: calls.append(request), resolver=resolve))
            self.assertFalse(calls)

    async def test_each_fetch_and_redirect_revalidates_dns_before_connecting(self):
        answers = iter((["93.184.215.14"], ["127.0.0.1"]))
        calls = []

        async def resolve(_host, _port):
            return next(answers)

        def handle(request):
            calls.append(request)
            return response(status=302, Location="/redirect")

        await self.category("blocked_address", self.fetcher(handle, resolver=resolve))
        self.assertEqual(len(calls), 1)
        answers = iter((["93.184.215.14"], ["10.0.0.1"]))
        fetcher = self.fetcher(resolver=resolve)
        await fetcher.fetch("https://example.org/first")
        await self.category("blocked_address", fetcher)

    async def test_redirects_are_followed_in_broker_not_returned_to_browser(self):
        seen = []
        responses = iter((response(status=302, Location="https://other.example/next"), response(b"final")))

        def handle(request):
            seen.append(request.headers["Host"])
            return next(responses)

        page = await self.fetcher(handle).fetch("https://example.org/start", document=True)
        self.assertEqual(page.url, "https://other.example/next")
        self.assertEqual(page.body, b"final")
        self.assertEqual(seen, ["example.org", "other.example"])
        await self.category("redirect_limit", self.fetcher(lambda _request: response(status=302, Location="/again"), max_redirects=1))
        await self.category("blocked_url", self.fetcher(lambda _request: response(status=302, Location="file:///etc/passwd")))

    async def test_schemes_credentials_ports_and_invalid_urls_are_rejected_before_network(self):
        calls = []
        for url in ("file:///etc/passwd", "ftp://example.org/", "https://user:secret@example.org/",
                    "http://example.org:8080/", "https://example.org:bad/", "//example.org/",
                    "https://example.org/has space", "http://[fe80::1%lo]/", "https://example.org\\@127.0.0.1/"):
            await self.category("blocked_url", self.fetcher(lambda request: calls.append(request)), url)
        self.assertFalse(calls)

    async def test_unsupported_downloads_encoding_status_and_mime_are_explicit(self):
        for headers, category in (({"Content-Type": "application/pdf"}, "unsupported_content"),
                                  ({"Content-Disposition": "attachment; filename=guide.html"}, "unsupported_content"),
                                  ({"Content-Encoding": "gzip"}, "unsupported_encoding"),
                                  ({"Content-Type": "application/octet-stream"}, "unsupported_content")):
            body = response(**headers)
            await self.category(category, self.fetcher(lambda _request: body), document=True)
            self.assertTrue(body.is_closed)
        await self.category("http_error", self.fetcher(lambda _request: response(status=403)))
        await self.category("unsupported_content", self.fetcher(lambda _request: response(**{"Content-Type": "application/json"})), document=True)
        await self.fetcher(lambda _request: response(b"{}", **{"Content-Type": "application/json"})).fetch("https://example.org/data")

    async def test_stream_and_content_length_enforce_response_and_total_byte_limits(self):
        for headers in ({}, {"Content-Length": "6"}, {"Content-Length": "9" * 100}):
            body = response(b"123456", **headers)
            await self.category("download_limit", self.fetcher(lambda _request: body, max_response_bytes=5))
            self.assertTrue(body.is_closed)
        fetcher = self.fetcher(lambda _request: response(b"1234"), max_total_bytes=7)
        await fetcher.fetch("https://example.org/one")
        await self.category("download_limit", fetcher)
        self.assertEqual(fetcher.collected_bytes, 4)
        fetcher = self.fetcher(max_requests=1)
        await fetcher.fetch("https://example.org/one")
        await self.category("request_limit", fetcher)

    async def test_dns_network_timeout_and_cancellation_have_no_partial_evidence(self):
        async def failing_dns(_host, _port):
            raise OSError("unreachable")

        await self.category("network_error", self.fetcher(resolver=failing_dns))

        def failing_http(request):
            raise httpx.ReadTimeout("slow", request=request)

        await self.category("network_error", self.fetcher(failing_http))
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked(_request):
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        task = asyncio.create_task(self.fetcher(blocked).fetch("https://example.org/"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
