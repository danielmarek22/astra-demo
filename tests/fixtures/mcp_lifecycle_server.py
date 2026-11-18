"""Harmless real-SDK server for deterministic client lifecycle tests."""

import asyncio
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ListToolsResult, Tool

pid_file = os.environ.get("ASTRA_TEST_PID_FILE")
if pid_file:
    Path(pid_file).write_text(str(os.getpid()))
mode = os.environ.get("ASTRA_TEST_MODE", "pages")
secret = os.environ.get("ASTRA_TEST_SECRET", "fixture-secret")
print(secret, file=sys.stderr)  # The host must not forward child stderr.


async def list_tools(_context, params):
    if mode == "slow":
        await asyncio.sleep(10)
    if mode == "fail":
        raise ValueError(secret)
    cursor = params.cursor if params else None
    tool = Tool(name="add" if cursor else "echo", description="A harmless test tool.",
                input_schema={"type": "object", "properties": {}})
    if mode == "large":
        tool.description = "x" * 4096
    return ListToolsResult(
        tools=[tool, tool] if mode == "duplicate" else [tool],
        next_cursor="second" if not cursor or mode == "repeat" else None,
    )


server = Server("Astra lifecycle fixture", on_list_tools=list_tools)


async def stdio():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    if len(sys.argv) > 1:
        import uvicorn
        from starlette.responses import PlainTextResponse

        app = server.streamable_http_app(json_response=True)

        async def protected(scope, receive, send):
            if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != b"Bearer fixture-token":
                await PlainTextResponse(secret, status_code=401)(scope, receive, send)
            else:
                await app(scope, receive, send)

        uvicorn.run(protected, host="127.0.0.1", port=int(sys.argv[1]), log_level="critical")
    else:
        asyncio.run(stdio())
