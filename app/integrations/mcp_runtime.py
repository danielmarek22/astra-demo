"""Owned, bounded MCP connections and invocation; no agent reasoning or routing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass

from jsonschema.exceptions import ValidationError

from app.config import MCPConfig


def _private_sdk_logs(record: logging.LogRecord) -> bool:
    # SDK diagnostics can include received bodies, commands and exception values.
    # Astra reports safe categories instead. Other applications/threads are unaffected.
    return record.threadName != "astra-mcp"


class MCPRuntimeError(Exception):
    """Only safe, application-owned messages cross the runtime boundary."""

    def __init__(self, category: str, message: str, *, completion: str = "not_started"):
        super().__init__(message)
        self.category = category
        self.completion = completion


def tool_fingerprint(definition: dict) -> str:
    contract = {key: definition.get(key) for key in (
        "name", "title", "description", "input_schema", "output_schema", "annotations",
    )}
    return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _expected_transport_exit(error):
    from app.integrations.mcp_transport import MCPTransportLimitError
    children = getattr(error, "exceptions", None)
    if children:
        return all(_expected_transport_exit(child) for child in children)
    return isinstance(error, MCPTransportLimitError)


@dataclass(frozen=True)
class MCPServerStatus:
    server_id: str
    state: str
    protocol_version: str | None = None
    tool_count: int = 0
    error_category: str | None = None
    error_message: str | None = None


@dataclass
class _Connection:
    queue: asyncio.Queue
    task: asyncio.Task | None = None


def _safe_error(error: BaseException) -> MCPRuntimeError:
    if isinstance(getattr(error, "exceptions", None), tuple):
        failures = [item for item in error.exceptions if not isinstance(item, asyncio.CancelledError)]
        return _safe_error(failures[0]) if failures else _safe_error(asyncio.CancelledError())
    if isinstance(error, MCPRuntimeError):
        return error
    if getattr(getattr(error, "response", None), "status_code", None) in {401, 403}:
        return MCPRuntimeError("authentication_failed", "MCP authentication was rejected; check the configured credentials.")
    if isinstance(error, FileNotFoundError):
        return MCPRuntimeError("launch_failed", "MCP executable or working directory was not found.")
    if isinstance(error, ImportError):
        return MCPRuntimeError("sdk_unavailable", "Install requirements-integrations.txt to connect MCP servers.")
    if isinstance(getattr(error, "code", None), int):
        return MCPRuntimeError("protocol_error", "MCP server rejected the protocol request.")
    if isinstance(error, RuntimeError) and isinstance(error.__cause__, ValidationError):
        return MCPRuntimeError("invalid_output", "MCP returned invalid structured output.")
    if isinstance(error, TimeoutError):
        return MCPRuntimeError("timeout", "The MCP operation exceeded its deadline.")
    return MCPRuntimeError("connection_failed", "MCP connection or protocol failed; check the server configuration and availability.")


@contextmanager
def _deadline(seconds: float):
    """Cancel the owner itself; SDK contexts must not move to a wait_for task.

    A timer also works on Python 3.10, which has no asyncio.timeout. An AnyIO
    cancel scope cannot be exited while newly entered SDK task scopes are live.
    """
    task = asyncio.current_task()
    expired = False

    def expire():
        nonlocal expired
        expired = True
        task.cancel()

    timer = asyncio.get_running_loop().call_later(seconds, expire)
    try:
        yield
    except asyncio.CancelledError:
        if not expired:
            raise
        if hasattr(task, "uncancel"):
            task.uncancel()
        raise TimeoutError from None
    finally:
        timer.cancel()


class MCPRuntime:
    """Own SDK contexts in persistent tasks on one lazily started event-loop thread.

    Catalog publication requires explicit management requests. Invocation preflight
    only checks admitted contracts. The SDK is imported only on connection, so
    disabled MCP remains inert even without the dependency.
    """

    def __init__(self, settings: dict, *, redaction_source=None):
        self._settings = MCPConfig.model_validate(deepcopy(settings))
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._slots = {key: threading.Lock() for key in self._settings.servers}
        self._statuses = {
            key: MCPServerStatus(key, "disconnected" if self._settings.enabled and spec.enabled else "disabled")
            for key, spec in self._settings.servers.items()
        }
        self._catalogs: dict[str, list[dict]] = {}
        self._secret_lock = redaction_source._secret_lock if redaction_source is not None else threading.RLock()
        self._secret_values: set[str] = redaction_source._secret_values if redaction_source is not None else set()
        self._cleanup_errors: dict[str, MCPRuntimeError] = {}
        self._connections: dict[str, _Connection] = {}  # Accessed only on the MCP loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def status(self, server_id: str) -> MCPServerStatus:
        with self._lock:
            if server_id not in self._statuses:
                raise MCPRuntimeError("unknown_server", "The MCP server is not configured.")
            return self._statuses[server_id]

    def catalog(self, server_id: str) -> list[dict]:
        self.status(server_id)
        with self._lock:
            return deepcopy(self._catalogs.get(server_id, []))

    def connect(self, server_id: str) -> MCPServerStatus:
        return self._submit(server_id, "connect")

    def refresh_catalog(self, server_id: str) -> list[dict]:
        return self._submit(server_id, "catalog")

    def server_settings(self, server_id: str) -> dict:
        self.status(server_id)
        return self._settings.servers[server_id].model_dump(exclude_none=True)

    def configuration(self) -> dict:
        return self._settings.model_dump(exclude_none=True)

    def server_ids(self) -> tuple[str, ...]:
        return tuple(self._settings.servers)

    @property
    def queue_timeout_s(self) -> float:
        return self._settings.queue_timeout_s

    def redact(self, value, *, credential_fields=True):
        with self._secret_lock:
            secrets = tuple(sorted(self._secret_values, key=len, reverse=True))

        def scrub(item):
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace(secret, "[redacted]")
                return item
            if isinstance(item, list):
                return [scrub(child) for child in item]
            if isinstance(item, dict):
                return {
                    scrub(str(key)): "[redacted]" if credential_fields and re.search(r"(?:token|password|secret|authorization|cookie|api_key)", str(key), re.IGNORECASE)
                    else scrub(child) for key, child in item.items()
                }
            return item
        return scrub(value)

    def invoke(self, server_id: str, name: str, arguments: dict, fingerprint: str) -> dict:
        spec = self._settings.servers[server_id]
        try:
            encoded = json.dumps(arguments, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise MCPRuntimeError("invalid_arguments", "MCP arguments must be finite JSON data.") from None
        if len(encoded.encode()) > spec.max_argument_bytes:
            raise MCPRuntimeError("argument_limit", "MCP arguments exceeded the configured limit.")
        return self._submit(server_id, "invoke", (name, json.loads(encoded), fingerprint))

    def disconnect(self, server_id: str) -> None:
        self._submit(server_id, "disconnect")

    def _publish(self, server_id: str, state: str, *, protocol_version=None, error=None) -> None:
        with self._lock:
            if state != "connected":
                self._catalogs.pop(server_id, None)
            self._statuses[server_id] = MCPServerStatus(
                server_id, state, protocol_version, len(self._catalogs.get(server_id, [])),
                error.category if error else None, str(error) if error else None,
            )

    def _start_loop(self) -> asyncio.AbstractEventLoop:
        # Called with the admission lock held; close cannot race thread creation.
        if self._loop is not None:
            return self._loop
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._thread = threading.Thread(target=run, name="astra-mcp", daemon=True)
        self._thread.start()
        if not ready.wait(timeout=5):
            self._closed = True
            raise MCPRuntimeError("runtime_failed", "The MCP runtime could not start.")
        return self._loop

    def _submit(self, server_id: str, operation: str, payload=None):
        self.status(server_id)
        spec = self._settings.servers[server_id]
        if not self._settings.enabled or not spec.enabled:
            raise MCPRuntimeError("disabled", "The MCP server is disabled.")
        slot = self._slots[server_id]
        if not slot.acquire(timeout=self._settings.queue_timeout_s):
            raise MCPRuntimeError("busy", "Another MCP management operation is still running.")
        try:
            with self._lock:
                if self._closed:
                    raise MCPRuntimeError("closed", "The MCP runtime is closed.")
                if server_id in self._cleanup_errors:
                    raise self._cleanup_errors[server_id]
                if operation == "disconnect" and self._loop is None:
                    return None
                loop = self._start_loop()
                future = asyncio.run_coroutine_threadsafe(self._request(server_id, operation, payload), loop)
            deadline = spec.connect_timeout_s + spec.catalog_timeout_s + spec.call_timeout_s + spec.shutdown_timeout_s + 2
            try:
                return future.result(timeout=deadline)
            except FutureTimeoutError:
                future.cancel()
                loop.call_soon_threadsafe(self._cancel_connection, server_id)
                error = MCPRuntimeError("runtime_timeout", "MCP cleanup could not be confirmed; connection cancellation was requested.")
                self._publish(server_id, "error", error=error)
                if operation == "invoke":
                    error.completion = "uncertain"
                raise error from None
        finally:
            slot.release()

    def _cancel_connection(self, server_id: str) -> None:
        connection = self._connections.get(server_id)
        if connection is not None and connection.task is not None:
            connection.task.cancel()

    async def _request(self, server_id: str, operation: str, payload=None):
        connection = self._connections.get(server_id)
        if connection is None or connection.task.done():
            if operation == "disconnect":
                self._publish(server_id, "disconnected")
                return None
            connection = _Connection(asyncio.Queue())
            self._connections[server_id] = connection
            connection.task = asyncio.create_task(self._owner(server_id, connection), name=f"mcp-owner-{server_id}")
        response = asyncio.get_running_loop().create_future()
        connection.queue.put_nowait((operation, response, payload))
        return await response

    def _resolve_references(self, references: dict[str, str], *, headers=False) -> dict[str, str]:
        values = {}
        for key, reference in references.items():
            value = os.environ.get(reference)
            if not value:
                raise MCPRuntimeError("credentials_missing", "A required MCP environment reference is missing or empty.")
            if headers and (len(value) > 8192 or any(ord(char) < 32 for char in value)):
                raise MCPRuntimeError("credentials_invalid", "An MCP header environment reference has an invalid value.")
            with self._secret_lock:
                self._secret_values.add(value)
                if headers and value.lower().startswith("bearer "):
                    self._secret_values.add(value[7:])
            values[key] = value
        return values

    async def _open_client(self, stack: AsyncExitStack, spec, auth_failed):
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.types import Implementation

        from app.integrations.mcp_transport import BoundedHTTPTransport, bounded_stdio

        for name in tuple(logging.Logger.manager.loggerDict):
            if name.startswith(("mcp.", "httpx2", "httpcore2")):
                logging.getLogger(name).addFilter(_private_sdk_logs)

        def wire_error(_error):
            auth_failed[0] = MCPRuntimeError("transport_limit", "MCP transport rejected an invalid, oversized, compressed or redirected response.")

        if spec.transport == "stdio":
            transport = bounded_stdio(spec, self._resolve_references(spec.env), wire_error)
        else:
            async def record_status(response):
                # The SDK converts non-JSON 401/403 responses into generic RPC
                # errors. Retain only the status classification, never the body.
                if response.status_code in {401, 403}:
                    auth_failed[0] = True

            client = await stack.enter_async_context(httpx2.AsyncClient(
                headers={"Accept-Encoding": "identity", **self._resolve_references(spec.headers, headers=True)},
                timeout=httpx2.Timeout(spec.catalog_timeout_s, connect=spec.connect_timeout_s),
                trust_env=False,
                transport=BoundedHTTPTransport(spec.max_message_bytes, wire_error),
                event_hooks={"response": [record_status]},
            ))
            transport = streamable_http_client(spec.url, http_client=client, max_sse_event_size=spec.max_message_bytes)
        # No result cache, optional callbacks, sampling, roots, or extensions.
        return await stack.enter_async_context(Client(
            transport, read_timeout_seconds=max(spec.catalog_timeout_s, spec.call_timeout_s), cache=None,
            client_info=Implementation(name="Astra", version="development"),
        ))

    async def _list_catalog(self, client, spec) -> list[dict]:
        tools, names, cursors = [], set(), set()
        cursor, received = None, 0
        for _ in range(spec.max_catalog_pages):
            page = await client.list_tools(cursor=cursor, cache_mode="bypass")
            received += len(page.model_dump_json().encode("utf-8"))
            if received > spec.max_catalog_bytes or len(tools) + len(page.tools) > spec.max_catalog_tools:
                raise MCPRuntimeError("catalog_limit", "MCP catalog exceeded the configured size limit.")
            for tool in page.tools:
                if tool.name in names:
                    raise MCPRuntimeError("invalid_catalog", "MCP catalog contains duplicate tool names.")
                names.add(tool.name)
                tools.append(tool.model_dump(mode="json", exclude_none=True))
            cursor = page.next_cursor
            if cursor is None:
                return tools
            if cursor in cursors:
                raise MCPRuntimeError("invalid_catalog", "MCP catalog repeated a pagination cursor.")
            cursors.add(cursor)
        raise MCPRuntimeError("catalog_limit", "MCP catalog exceeded the configured page limit.")

    async def _owner(self, server_id: str, connection: _Connection) -> None:
        spec = self._settings.servers[server_id]
        stack = AsyncExitStack()
        current = None
        error = None
        auth_failed = [False]
        call_started = False
        try:
            current = await connection.queue.get()
            self._publish(server_id, "connecting")
            try:
                with _deadline(spec.connect_timeout_s):
                    client = await self._open_client(stack, spec, auth_failed)
            except TimeoutError:
                raise MCPRuntimeError("connection_timeout", "MCP connection exceeded its deadline.") from None
            self._publish(server_id, "connected", protocol_version=client.protocol_version)
            while True:
                operation, response, payload = current
                call_started = False
                if operation == "disconnect":
                    break
                result = self.status(server_id)
                if operation == "catalog":
                    try:
                        with _deadline(spec.catalog_timeout_s):
                            result = await self._list_catalog(client, spec)
                    except TimeoutError:
                        raise MCPRuntimeError("catalog_timeout", "MCP catalog retrieval exceeded its deadline.") from None
                    with self._lock:
                        self._catalogs[server_id] = deepcopy(result)
                    self._publish(server_id, "connected", protocol_version=client.protocol_version)
                if operation == "invoke":
                    name, arguments, expected = payload
                    # Read-only preflight never publishes or enables tools. It
                    # catches drift on legacy servers as well as reconnections.
                    with _deadline(spec.catalog_timeout_s):
                        catalog = await self._list_catalog(client, spec)
                    with self._lock:
                        self._catalogs[server_id] = deepcopy(catalog)
                    definition = next((tool for tool in catalog if tool["name"] == name), None)
                    if definition is None or tool_fingerprint(definition) != expected:
                        raise MCPRuntimeError("schema_changed", "MCP tool contract changed or disappeared; refresh and review its enablement.")
                    try:
                        with _deadline(spec.call_timeout_s):
                            call_started = True
                            outcome = await client.session.call_tool(
                                name, arguments, read_timeout_seconds=spec.call_timeout_s,
                                allow_input_required=True,
                            )
                    except TimeoutError:
                        raise MCPRuntimeError("call_timeout", "MCP call timed out; completion is uncertain. Do not retry automatically.", completion="uncertain") from None
                    result = outcome.model_dump(mode="json", exclude_none=True)
                if not response.done():
                    response.set_result(result)
                current = None
                call_started = False
                current = await connection.queue.get()
        except BaseException as failure:  # noqa: BLE001 -- owner must resolve waiters and release contexts on cancellation
            error = (
                auth_failed[0] if isinstance(auth_failed[0], MCPRuntimeError) else
                MCPRuntimeError("authentication_failed", "MCP authentication was rejected; check the configured credentials.")
                if auth_failed[0] else _safe_error(failure)
            )
            if call_started:
                error.completion = "uncertain"
                from mcp.types import HEADER_MISMATCH
                if getattr(failure, "code", None) == HEADER_MISMATCH:
                    error = MCPRuntimeError("schema_changed", "MCP tool schema was rejected before execution; refresh and review the catalog.")
        finally:
            try:
                with _deadline(spec.shutdown_timeout_s):
                    await stack.aclose()
            except BaseException as cleanup_failure:  # noqa: BLE001 -- distinguish completed task-group exit from failed cleanup
                if _expected_transport_exit(cleanup_failure):
                    error = MCPRuntimeError("transport_limit", "MCP transport rejected an invalid or oversized response.",
                                            completion="uncertain" if call_started else "not_started")
                else:
                    error = MCPRuntimeError("shutdown_failed", "MCP connection cleanup failed; do not reuse this connection.", completion="uncertain" if call_started else "not_started")
                    with self._lock:
                        self._cleanup_errors[server_id] = error
            if server_id in self._cleanup_errors:
                self._publish(server_id, "error", error=error)
            elif self._closed:
                self._publish(server_id, "closed")
                error = MCPRuntimeError("closed", "The MCP runtime is closed.", completion="uncertain" if call_started else "not_started")
            else:
                self._publish(server_id, "error" if error else "disconnected", error=error)
            pending = [current] if current is not None else []
            while not connection.queue.empty():
                pending.append(connection.queue.get_nowait())
            for operation, response, _payload in pending:
                if not response.done():
                    if error:
                        response.set_exception(error)
                    else:
                        response.set_result(None)

    async def _shutdown(self) -> None:
        tasks = [item.task for item in self._connections.values() if item.task is not None and not item.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            timeout = max(spec.shutdown_timeout_s for spec in self._settings.servers.values()) + 1
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                raise MCPRuntimeError("shutdown_failed", "MCP shutdown could not confirm connection cleanup.")
        if self._cleanup_errors:
            raise MCPRuntimeError("shutdown_failed", "MCP shutdown could not confirm connection cleanup.")

    def close(self) -> None:
        with self._close_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                loop, thread = self._loop, self._thread
                for key in self._statuses:
                    if key in self._cleanup_errors:
                        self._publish(key, "error", error=self._cleanup_errors[key])
                    else:
                        self._publish(key, "closed")
                if loop is None:
                    return
                future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            timeout = max(spec.shutdown_timeout_s for spec in self._settings.servers.values()) + 2
            try:
                future.result(timeout=timeout)
            except FutureTimeoutError:
                raise MCPRuntimeError("shutdown_failed", "MCP shutdown exceeded its cleanup deadline.") from None
            # Do not stop a loop with unconfirmed cleanup or orphan its processes.
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
            if thread.is_alive():
                raise MCPRuntimeError("shutdown_failed", "The MCP runtime thread did not stop.")
