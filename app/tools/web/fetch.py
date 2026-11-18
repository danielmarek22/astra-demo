import asyncio
from dataclasses import dataclass
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from app.tools.web.contracts import WebReaderError
from app.tools.web.formatting import source_url


@dataclass(frozen=True)
class FetchedPage:
    url: str
    body: bytes
    content_type: str


def public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and (address.ipv4_mapped or address.sixtofour or address.teredo or address.scope_id):
        return False
    return address.is_global and not any((address.is_multicast, address.is_reserved, address.is_unspecified, address.is_loopback, address.is_link_local, address.is_private))


class PublicPageFetcher:
    def __init__(self, *, max_response_bytes=2097152, max_total_bytes=4194304,
                 max_requests=32, max_redirects=3, timeout_s=10.0, resolver=None, transport=None):
        self.max_response_bytes = max_response_bytes
        self.max_total_bytes = max_total_bytes
        self.max_requests = max_requests
        self.max_redirects = max_redirects
        self.timeout_s = timeout_s
        self.resolver = resolver
        self.transport = transport
        self.collected_bytes = 0
        self.requests = 0

    async def _addresses(self, host, port):
        if self.resolver is not None:
            return await self.resolver(host, port)
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            return list(dict.fromkeys(record[4][0] for record in records))

    async def fetch(self, url, *, document=False):
        for redirect in range(self.max_redirects + 1):
            normalized = source_url(url, 4096)
            if normalized is None:
                raise WebReaderError("blocked_url", "Only valid HTTP(S) page URLs without credentials are allowed.")
            parsed = urlsplit(normalized)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if port not in {80, 443} or "%" in parsed.hostname:
                raise WebReaderError("blocked_url", "The page reader allows only standard public HTTP(S) destinations.")
            try:
                addresses = await self._addresses(parsed.hostname, port)
            except OSError as error:
                raise WebReaderError("network_error", "The page hostname could not be resolved.") from error
            if not addresses or len(addresses) > 32 or not all(public_address(address) for address in addresses):
                raise WebReaderError("blocked_address", "This request resolves to a non-public or unsupported address.")
            if self.requests >= self.max_requests:
                raise WebReaderError("request_limit", "The page exceeded the configured request limit.")
            self.requests += 1
            original = httpx.URL(normalized)
            pinned = original.copy_with(host=addresses[0], fragment=None)
            host = original.netloc.decode("ascii")
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout_s, trust_env=False, follow_redirects=False) as client:
                    request = client.build_request("GET", pinned, headers={
                        "Host": host, "Accept-Encoding": "identity",
                        "User-Agent": "AstraPageReader/1.0", "Accept": "text/html,application/xhtml+xml" if document else "*/*",
                    }, extensions={"sni_hostname": parsed.hostname})
                    response = await client.send(request, stream=True)
                    try:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirect == self.max_redirects or not response.headers.get("Location"):
                                raise WebReaderError("redirect_limit", "The page exceeded the redirect limit or returned an invalid redirect.")
                            url = urljoin(normalized, response.headers["Location"])
                            continue
                        if response.status_code != 200:
                            raise WebReaderError("http_error", f"The page returned HTTP {response.status_code}.")
                        mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        allowed = {"text/html", "application/xhtml+xml"} if document else {
                            "text/html", "application/xhtml+xml", "text/css", "text/javascript", "application/javascript", "application/json", "text/plain",
                        }
                        if mime not in allowed or "attachment" in response.headers.get("Content-Disposition", "").lower():
                            raise WebReaderError("unsupported_content", "This content type is unsupported by the page reader; PDF and downloads are not enabled.")
                        if response.headers.get("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
                            raise WebReaderError("unsupported_encoding", "The server returned compressed content despite the bounded reader's identity-encoding request.")
                        length = response.headers.get("Content-Length", "")
                        remaining = min(self.max_response_bytes, self.max_total_bytes - self.collected_bytes)
                        if length.isdigit() and (len(length) > 20 or int(length) > remaining):
                            raise WebReaderError("download_limit", "The page exceeded the configured download limit.")
                        body = bytearray()
                        async for chunk in response.aiter_raw():
                            if len(body) + len(chunk) > self.max_response_bytes or self.collected_bytes + len(chunk) > self.max_total_bytes:
                                raise WebReaderError("download_limit", "The page exceeded the configured download limit.")
                            body.extend(chunk)
                            self.collected_bytes += len(chunk)
                        return FetchedPage(str(original), bytes(body), response.headers["Content-Type"])
                    finally:
                        await response.aclose()
            except httpx.HTTPError as error:
                raise WebReaderError("network_error", "The page request failed or timed out; no evidence was retrieved.") from error
        raise WebReaderError("redirect_limit", "The page exceeded the redirect limit.")
