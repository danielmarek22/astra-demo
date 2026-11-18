"""Bounded concrete transports for the pinned SDK; no agent or JSON-RPC routing."""

import asyncio
import os
import signal
from contextlib import asynccontextmanager, suppress

import anyio
import httpx2
from mcp.client.stdio import get_default_environment
from mcp.shared.message import SessionMessage
from mcp.types import jsonrpc_message_adapter


class MCPTransportLimitError(Exception):
    pass


@asynccontextmanager
async def bounded_stdio(spec, env, on_error):
    process = await asyncio.create_subprocess_exec(
        spec.command, *spec.args, cwd=spec.cwd, env=get_default_environment() | env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=os.name == "posix",
        limit=spec.max_message_bytes,
    )
    incoming_send, incoming = anyio.create_memory_object_stream(0)
    outgoing, outgoing_receive = anyio.create_memory_object_stream(0)
    stopped = False
    reader_done = asyncio.Event()

    async def reader():
        try:
            async with incoming_send:
                while True:
                    try:
                        line = await process.stdout.readuntil(b"\n")
                    except asyncio.IncompleteReadError as error:
                        if error.partial:
                            raise MCPTransportLimitError("Incomplete MCP message") from None
                        return
                    except (asyncio.LimitOverrunError, ValueError):
                        raise MCPTransportLimitError("MCP message exceeded the receive limit") from None
                    if len(line) > spec.max_message_bytes:
                        raise MCPTransportLimitError("MCP message exceeded the receive limit")
                    await incoming_send.send(SessionMessage(jsonrpc_message_adapter.validate_json(line)))
        except Exception as error:  # noqa: BLE001 -- signal only safe diagnostics to the owner
            on_error(error)
            with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
                await incoming_send.send(MCPTransportLimitError("MCP transport received an invalid or oversized message"))
        finally:
            reader_done.set()

    async def writer():
        async with outgoing_receive:
            try:
                async for message in outgoing_receive:
                    encoded = (message.message.model_dump_json(by_alias=True, exclude_none=True) + "\n").encode()
                    if len(encoded) > spec.max_message_bytes:
                        raise MCPTransportLimitError("MCP outgoing message exceeded the limit")
                    process.stdin.write(encoded)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionError, MCPTransportLimitError) as error:
                on_error(error)
                await incoming_send.send(MCPTransportLimitError("MCP transport write failed"))

    async def stop():
        async def discard_stdout():
            while await process.stdout.read(65536):
                pass

        # Consume a rejected partial line so asyncio can observe EOF and close
        # its pipe transport, even when the StreamReader paused at its limit.
        drain = asyncio.create_task(discard_stdout())
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                await asyncio.wait_for(process.wait(), timeout=1)
        finally:
            # A parent may exit while its descendants still hold the pipes open.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            with suppress(asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                await asyncio.wait_for(drain, timeout=1)
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(reader)
            group.start_soon(writer)
            try:
                yield incoming, outgoing
            finally:
                group.cancel_scope.cancel()
                with anyio.CancelScope(shield=True):
                    await reader_done.wait()
                    await stop()
                    stopped = True
    finally:
        if not stopped:
            with anyio.CancelScope(shield=True):
                await stop()
        await incoming.aclose()
        await outgoing.aclose()


class _BoundedResponse(httpx2.AsyncByteStream):
    def __init__(self, stream, limit, on_error):
        self.stream, self.limit, self.on_error = stream, limit, on_error

    async def __aiter__(self):
        received = 0
        async for chunk in self.stream:
            received += len(chunk)
            if received > self.limit:
                error = MCPTransportLimitError("MCP HTTP response exceeded the receive limit")
                self.on_error(error)
                raise error
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class BoundedHTTPTransport(httpx2.AsyncBaseTransport):
    def __init__(self, limit, on_error):
        self.transport = httpx2.AsyncHTTPTransport(retries=0, trust_env=False)
        self.limit, self.on_error = limit, on_error

    async def handle_async_request(self, request):
        response = await self.transport.handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            error = MCPTransportLimitError("Compressed MCP responses are unsupported")
        elif 300 <= response.status_code < 400:
            error = MCPTransportLimitError("MCP redirects are unsupported; configure the final endpoint")
        else:
            response.stream = _BoundedResponse(response.stream, self.limit, self.on_error)
            return response
        await response.aclose()
        self.on_error(error)
        raise error

    async def aclose(self):
        await self.transport.aclose()
