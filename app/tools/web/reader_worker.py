"""Standalone, network-isolated Crawl4AI worker using bounded JSON pipe messages."""

import asyncio
import base64
import json
import os
from pathlib import Path
import resource
import sys


async def serve(protocol_output):
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    reader = asyncio.StreamReader(limit=12582912)
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    pending = {}
    sequence = 0
    active = None

    def send(payload):
        protocol_output.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
        protocol_output.flush()

    crawler = AsyncWebCrawler(config=BrowserConfig(
        headless=True, verbose=False, ignore_https_errors=False, accept_downloads=False,
        use_persistent_context=False, max_pages_before_recycle=1,
        extra_args=["--disable-background-networking", "--disable-quic", "--js-flags=--max-old-space-size=128"],
    ), base_directory="/tmp")
    await crawler.start()
    send({"type": "ready", "browser_version": crawler.crawler_strategy.browser_manager.browser.version})

    async def read_page(command):
        nonlocal sequence
        pages = []
        callbacks = set()
        warnings = []
        initial_served = False
        request_count = 0
        operation = command["operation"]

        def warn(message):
            if len(warnings) < 5 and message not in warnings:
                warnings.append(message)

        async def intercept(route):
            nonlocal sequence, initial_served, request_count
            task = asyncio.current_task()
            callbacks.add(task)
            try:
                request = route.request
                if request.method != "GET":
                    await route.abort()
                    return
                if request.is_navigation_request() and request.frame == pages[0].main_frame:
                    if initial_served or request.url != command["url"].split("#", 1)[0]:
                        warn("Additional top-level navigation was blocked.")
                        await route.abort()
                        return
                    initial_served = True
                    await route.fulfill(status=200, content_type=command["content_type"], body=base64.b64decode(command["body"]))
                    return
                if request.resource_type not in {"document", "script", "stylesheet", "xhr", "fetch"}:
                    await route.abort()
                    return
                request_count += 1
                if request_count >= command["max_requests"]:
                    warn("Additional page requests exceeded the request limit.")
                    await route.abort()
                    return
                sequence += 1
                request_id = sequence
                future = asyncio.get_running_loop().create_future()
                pending[request_id] = future
                send({"type": "fetch", "operation": operation, "request_id": request_id,
                      "url": request.url, "document": request.resource_type == "document"})
                try:
                    result = await future
                finally:
                    pending.pop(request_id, None)
                if result.get("error"):
                    warn(str(result["error"])[:240])
                    await route.abort()
                else:
                    await route.fulfill(status=200, content_type=result["content_type"], body=base64.b64decode(result["body"]))
            except Exception:
                warn("A page subresource could not be read.")
                try:
                    await route.abort()
                except Exception:
                    pass
            finally:
                callbacks.discard(task)

        async def on_page(page, context, **_kwargs):
            pages.append(page)
            await context.route("**/*", intercept)
            await context.route_web_socket("**/*", lambda connection: connection.close())

        crawler.crawler_strategy.set_hook("on_page_context_created", on_page)
        try:
            result = await crawler.arun(url=command["url"], config=CrawlerRunConfig(
                cache_mode=CacheMode.DISABLED, extraction_strategy=None,
                markdown_generator=DefaultMarkdownGenerator(), page_timeout=10000,
                wait_until="domcontentloaded", delay_before_return_html=0.25,
                word_count_threshold=1, table_score_threshold=0, verbose=False,
                check_robots_txt=False, process_iframes=False,
            ))
            markdown = result.markdown.raw_markdown if result.markdown else ""
            encoded = markdown.encode("utf-8")
            maximum = command["max_document_bytes"]
            clipped = encoded[:maximum].decode("utf-8", errors="ignore")
            links = []
            for group in ("internal", "external"):
                for link in (result.links or {}).get(group, []):
                    if len(links) >= command["max_links"]:
                        break
                    url = link.get("href", "")
                    if isinstance(url, str) and len(url) <= 4096:
                        links.append({"url": url, "text": str(link.get("text", ""))[:240]})
            title = (result.metadata or {}).get("title", "")
            payload = {
                "type": "result", "operation": operation,
                "markdown": clipped, "title": str(title or "")[:240],
                "links": links, "truncated": len(encoded) > maximum,
                "warnings": list(dict.fromkeys(warnings))[:5],
                "success": result.success,
                "error": str(result.error_message or "")[:500],
            }
        except Exception:
            payload = {"type": "error", "operation": operation, "message": "The isolated reader could not extract this page."}
        finally:
            for callback in tuple(callbacks):
                callback.cancel()
            await asyncio.gather(*tuple(callbacks), return_exceptions=True)
            contexts = {page.context for page in pages}
            for context in contexts:
                await context.close()
            pending.clear()
        send(payload)

    try:
        while line := await reader.readline():
            message = json.loads(line)
            if message.get("command") == "read":
                if active is not None:
                    await active
                active = asyncio.create_task(read_page(message))
            elif message.get("command") == "fetch_result":
                future = pending.get(message["request_id"])
                if future is not None and not future.done():
                    future.set_result(message)
            elif message.get("command") == "stop":
                break
    finally:
        if active is not None:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        await crawler.close()


def main():
    output = sys.stdout
    sys.stdout = sys.stderr
    output.write(json.dumps({"type": "containment", "cgroup": Path("/proc/self/cgroup").read_text().strip()}) + "\n")
    output.flush()
    confirmation = json.loads(sys.stdin.readline())
    if confirmation != {"command": "containment_verified"}:
        raise RuntimeError("Reader resource containment was not verified.")
    os.environ["CRAWL4_AI_BASE_DIRECTORY"] = "/tmp"
    os.environ["CRAWL4AI_NO_CLOUD_NOTICE"] = "1"
    os.environ["CRAWL4AI_CHROMIUM_SANDBOX"] = "true"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    resource.setrlimit(resource.RLIMIT_FSIZE, (8388608, 8388608))
    asyncio.run(serve(output))


if __name__ == "__main__":
    main()
