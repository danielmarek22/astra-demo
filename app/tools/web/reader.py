import asyncio
import base64
from concurrent.futures import CancelledError as FutureCancelledError, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import sys
from threading import BoundedSemaphore, Event, Lock, Thread
import time
import uuid

from app.tools.web.contracts import PageLink, ReadDocument, WebReaderError
from app.tools.web.fetch import PublicPageFetcher
from app.tools.web.formatting import plain_text, source_url
from app.tools.web.resources import ReaderResourceScope


def isolated_command(python_executable, browsers_path):
    executable = Path(python_executable).absolute()
    environment = executable.parent.parent
    if shutil.which("bwrap") is None or not (environment / "pyvenv.cfg").is_file():
        raise WebReaderError("reader_unavailable", "The local reader requires Linux bubblewrap and a virtual environment with requirements-web.txt installed.")
    browsers = Path(browsers_path).expanduser().resolve()
    if not browsers.is_dir():
        raise WebReaderError("reader_unavailable", "Install the reader's Chromium browser and configure its browser directory.")
    command = [shutil.which("bwrap"), "--die-with-parent", "--unshare-net", "--unshare-pid",
               "--unshare-ipc", "--unshare-uts", "--clearenv"]
    for directory in ("/usr", "/lib", "/lib64"):
        if Path(directory).exists():
            command.extend(("--ro-bind", directory, directory))
    for path in ("/etc/ssl", "/etc/fonts", "/etc/ld.so.cache"):
        if Path(path).exists():
            command.extend(("--ro-bind", path, path))
    command.extend((
        "--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev",
        "--size", "67108864", "--tmpfs", "/tmp", "--dir", "/opt",
        "--ro-bind", str(environment), "/opt/python",
        "--ro-bind", str(browsers), "/opt/browsers",
        "--ro-bind", str(Path(__file__).with_name("reader_worker.py")), "/opt/reader_worker.py",
        "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/tmp",
        "--setenv", "XDG_CACHE_HOME", "/tmp", "--setenv", "PYTHONNOUSERSITE", "1",
        "--setenv", "PLAYWRIGHT_BROWSERS_PATH", "/opt/browsers",
        "--setenv", "OPENBLAS_NUM_THREADS", "1", "--chdir", "/tmp",
        "/opt/python/bin/python", "-u", "/opt/reader_worker.py",
    ))
    return command


class Crawl4AIReader:
    def __init__(
        self, *, python_executable=None, browsers_path=None, total_timeout_s=30.0,
        max_response_bytes=2097152, max_total_bytes=4194304, max_requests=32,
        max_redirects=3, max_document_bytes=524288, max_links=20, max_memory_mb=1536,
        url_max_chars=2048,
        max_cpu_percent=100, max_tasks=256,
        fetcher_factory=PublicPageFetcher,
    ):
        self.python_executable = python_executable or sys.executable
        self.browsers_path = browsers_path or os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
        self.total_timeout_s = total_timeout_s
        self.max_document_bytes = max_document_bytes
        self.max_links = max_links
        self.max_memory_mb = max_memory_mb
        self.url_max_chars = url_max_chars
        self.max_cpu_percent = max_cpu_percent
        self.max_tasks = max_tasks
        self._resource_scope = None
        self.fetcher_factory = fetcher_factory
        self.fetch_settings = dict(max_response_bytes=max_response_bytes, max_total_bytes=max_total_bytes,
                                   max_requests=max_requests, max_redirects=max_redirects)
        self._lock = Lock()
        self._slot = BoundedSemaphore(1)
        self._loop = None
        self._thread = None
        self._process = None
        self._closed = False
        self._memory_error = False
        self._stderr_task = None
        self._memory_task = None
        self._active_task = None
        self.last_metrics = {}

    def _start_loop(self):
        with self._lock:
            if self._closed:
                raise WebReaderError("reader_unavailable", "The page reader is closed.")
            if self._thread is None:
                ready = Event()

                def run_loop():
                    try:
                        self._loop = asyncio.new_event_loop()
                    except Exception:
                        ready.set()
                        return
                    asyncio.set_event_loop(self._loop)
                    ready.set()
                    self._loop.run_forever()
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                    self._loop.close()

                self._thread = Thread(target=run_loop, name="astra-web-reader", daemon=True)
                self._thread.start()
                if not ready.wait(2) or self._loop is None:
                    raise WebReaderError("reader_unavailable", "The page reader event loop could not start.")

    def read(self, url):
        started = time.monotonic()
        if not self._slot.acquire(timeout=self.total_timeout_s):
            raise WebReaderError("timeout", "The page read timed out waiting for another read.")
        try:
            self._start_loop()
            remaining = self.total_timeout_s - (time.monotonic() - started)
            self.last_metrics = {"queue_wait_ms": round((time.monotonic() - started) * 1000, 2)}
            with self._lock:
                if self._closed:
                    raise WebReaderError("reader_unavailable", "The page reader is closed.")
                future = asyncio.run_coroutine_threadsafe(self._bounded_read(url, remaining), self._loop)
            try:
                return future.result(timeout=max(0, remaining) + 3)
            except FutureCancelledError as error:
                raise WebReaderError("reader_unavailable", "The page reader was closed during this read.") from error
            except FutureTimeoutError as error:
                future.cancel()
                self.close()
                raise WebReaderError("reader_unavailable", "Reader cleanup could not be confirmed; the worker was quarantined.") from error
        finally:
            self.last_metrics["total_ms"] = round((time.monotonic() - started) * 1000, 2)
            self._slot.release()

    async def _bounded_read(self, url, remaining):
        self._active_task = asyncio.current_task()
        try:
            return await asyncio.wait_for(self._read(url), max(0, remaining))
        except asyncio.TimeoutError as error:
            await self._stop_process()
            raise WebReaderError("timeout", "The page read exceeded its total deadline; the isolated worker was stopped.") from error
        except (OSError, ValueError, KeyError, TypeError) as error:
            await self._stop_process()
            raise WebReaderError("reader_unavailable", "The isolated reader failed; check its dependencies and configuration.") from error
        except BaseException:
            await self._stop_process()
            raise
        finally:
            self._active_task = None

    async def _ensure_process(self):
        try:
            import psutil
        except ImportError as error:
            raise WebReaderError("reader_unavailable", "Install psutil from requirements-web.txt in the application environment to monitor the page reader.") from error
        if self._process is not None and self._process.returncode is None:
            return
        await self._stop_process()
        self._memory_error = False
        self._resource_scope = ReaderResourceScope(
            max_memory_mb=self.max_memory_mb, max_cpu_percent=self.max_cpu_percent, max_tasks=self.max_tasks,
        )
        self._process = await asyncio.create_subprocess_exec(
            *self._resource_scope.command(isolated_command(self.python_executable, self.browsers_path)),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=12582912,
        )

        process = self._process

        async def drain_errors():
            while await process.stderr.read(4096):
                pass

        self._stderr_task = asyncio.create_task(drain_errors())
        self._memory_task = asyncio.create_task(self._monitor_memory(psutil))
        containment = await self._message()
        if containment.get("type") != "containment":
            raise WebReaderError("resource_isolation", "The reader did not complete its resource-containment handshake.")
        self.last_metrics["hard_resource_limits"] = self._resource_scope.verify(containment.get("cgroup"))
        await self._send({"command": "containment_verified"})
        ready = await self._message()
        if ready.get("type") != "ready":
            raise WebReaderError("reader_unavailable", "The isolated reader could not start; check its optional dependencies, browser, and bubblewrap permissions.")

    async def _monitor_memory(self, psutil):
        process = psutil.Process(self._process.pid)
        peak = 0
        while self._process.returncode is None:
            try:
                rss = sum(child.memory_info().rss for child in [process, *process.children(recursive=True)] if child.is_running())
                peak = max(peak, rss)
                self.last_metrics["peak_worker_rss_sum_mb"] = round(peak / 1048576, 2)
                if rss > self.max_memory_mb * 1048576:
                    self._memory_error = True
                    self._kill_process()
                    return
            except psutil.Error:
                pass
            await asyncio.sleep(0.05)

    async def _message(self):
        line = await self._process.stdout.readline()
        if not line:
            category = "memory_limit" if self._memory_error else "reader_unavailable"
            raise WebReaderError(category, "The isolated reader stopped or exceeded a resource limit. Check the systemd user session, cgroup controllers, reader dependencies and Chromium installation.")
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError) as error:
            raise WebReaderError("reader_unavailable", "The isolated reader returned an invalid protocol message.") from error
        if not isinstance(message, dict):
            raise WebReaderError("reader_unavailable", "The isolated reader returned an invalid protocol message.")
        return message

    async def _send(self, payload):
        self._process.stdin.write((json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode())
        await self._process.stdin.drain()

    async def _read(self, url):
        url = source_url(url, self.url_max_chars)
        if url is None:
            raise WebReaderError("blocked_url", "The page URL is invalid or exceeds the configured URL limit.")
        fetcher = self.fetcher_factory(**self.fetch_settings)
        initial = await fetcher.fetch(url, document=True)
        if source_url(initial.url, self.url_max_chars) is None:
            raise WebReaderError("blocked_url", "The final page URL exceeds the configured URL limit.")
        await self._ensure_process()
        operation = uuid.uuid4().hex
        await self._send({
            "command": "read", "operation": operation, "url": initial.url,
            "body": base64.b64encode(initial.body).decode("ascii"), "content_type": initial.content_type,
            "max_document_bytes": self.max_document_bytes, "max_links": self.max_links,
            "max_requests": self.fetch_settings["max_requests"],
        })
        while True:
            message = await self._message()
            if message.get("operation") != operation:
                raise WebReaderError("reader_unavailable", "The isolated reader returned a mismatched operation.")
            if message.get("type") == "fetch":
                reply = {"command": "fetch_result", "request_id": message["request_id"]}
                try:
                    page = await fetcher.fetch(message.get("url"), document=message.get("document") is True)
                    reply.update(body=base64.b64encode(page.body).decode("ascii"), content_type=page.content_type)
                except WebReaderError as error:
                    reply["error"] = f"{error.category}: {error}"
                await self._send(reply)
                continue
            if message.get("type") != "result":
                raise WebReaderError("extraction_failed", "The isolated reader could not extract usable page evidence.")
            markdown = message.get("markdown")
            if not isinstance(markdown, str) or not markdown.strip():
                raise WebReaderError("empty_content", "The page contained no usable extracted text.")
            if message.get("success") is not True:
                detail, _ = plain_text(message.get("error"), 300)
                raise WebReaderError("extraction_failed", "The page reader reported blocked or inadequate content; no page evidence was accepted. " + detail)
            encoded = markdown.encode("utf-8")
            markdown = encoded[:self.max_document_bytes].decode("utf-8", errors="ignore")
            title, _ = plain_text(message.get("title"), 240)
            links = []
            raw_links = message.get("links", [])
            raw_warnings = message.get("warnings", [])
            if not isinstance(raw_links, list) or not isinstance(raw_warnings, list):
                raise WebReaderError("reader_unavailable", "The isolated reader returned invalid page metadata.")
            for item in raw_links[:self.max_links]:
                link = source_url(item.get("url"), self.url_max_chars) if isinstance(item, dict) else None
                if link:
                    text, _ = plain_text(item.get("text"), 240)
                    links.append(PageLink(link, text))
            warnings = tuple(str(warning)[:240] for warning in raw_warnings[:5])
            self.last_metrics.update(download_bytes=fetcher.collected_bytes, request_count=fetcher.requests)
            return ReadDocument(url, initial.url, title, datetime.now(timezone.utc).isoformat(), markdown,
                                tuple(links), bool(message.get("truncated")) or len(encoded) > self.max_document_bytes, warnings)

    def _kill_process(self):
        if self._process is not None and self._process.returncode is None:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def _stop_process(self):
        process = self._process
        if process is not None and process.stdin is not None:
            process.stdin.close()
        self._kill_process()
        if process is not None:
            await asyncio.wait_for(process.wait(), 2)
            if process.stdin is not None:
                try:
                    await asyncio.wait_for(process.stdin.wait_closed(), 1)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        for task in (self._memory_task, self._stderr_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (self._memory_task, self._stderr_task) if task is not None), return_exceptions=True)
        self._process = None
        self._memory_task = self._stderr_task = None

    async def _shutdown(self):
        active = self._active_task
        if active is not None:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        await self._stop_process()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=3)
            finally:
                self._kill_process()
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=3)
