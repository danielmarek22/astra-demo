"""Controlled SDK server with state-file driven contracts and counted effects."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ElicitRequest,
    InputRequiredResult,
    ListToolsResult,
    TextContent,
    Tool,
)

state_path = Path(os.environ["ASTRA_MCP_STATE_FILE"])
state_path.with_suffix(".pid").write_text(str(os.getpid()))
secret = os.environ.get("ASTRA_MCP_CREDENTIAL", "fixture-private-token")
wire_stdout = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)


def state():
    return json.loads(state_path.read_text())


def tools():
    config = state()
    names = config.get("tools", ["echo", "error", "image", "structured", "input", "slow", "disconnect", "large", "raw"])
    result = []
    for name in names:
        schema = {"type": "object", "properties": {"text": {"type": "string"}},
                  "required": ["text"], "additionalProperties": False}
        if name == "echo" and config.get("revision") == 2:
            schema["properties"]["revision"] = {"type": "integer"}
            schema["required"].append("revision")
        kwargs = {"output_schema": {"type": "object", "properties": {"length": {"type": "integer"}}, "required": ["length"]}} if name == "structured" else {}
        result.append(Tool(name=name, description=f"Controlled {name} tool", input_schema=schema, **kwargs))
    return result


async def list_tools(_context, _params):
    return ListToolsResult(tools=tools())


async def call_tool(_context, params):
    config = state()
    counter = state_path.with_suffix(".calls")
    calls = json.loads(counter.read_text()) if counter.exists() else []
    calls.append(params.name)
    counter.write_text(json.dumps(calls))
    if params.name == "slow":
        await asyncio.sleep(10)
    if params.name == "disconnect":
        os._exit(0)
    if params.name == "raw":
        wire_stdout.write(b"x" * 65536 + b"\n")
        await asyncio.sleep(10)
    if params.name == "input":
        return InputRequiredResult(input_requests={"question": ElicitRequest(params={
            "mode": "form", "message": "Provide extra input", "requested_schema": {"type": "object", "properties": {}},
        })})
    if params.name == "image":
        return CallToolResult(content=[{"type": "image", "data": "aGVsbG8=", "mime_type": "image/png"}, TextContent(text="Image omitted")])
    if params.name == "structured":
        return CallToolResult(content=[], structured_content={"wrong": True} if config.get("bad_output") else {"length": len(params.arguments["text"])})
    text = "x" * config.get("result_chars", 100000) if params.name == "large" else params.arguments["text"]
    if params.name == "error":
        text = f"Controlled error containing {secret}"
    if config.get("echo_secret"):
        text += " " + secret
    return CallToolResult(content=[TextContent(text=text)], is_error=params.name == "error")


server = Server("Astra invocation fixture", on_list_tools=list_tools, on_call_tool=call_tool,
                get_tool_input_schema=lambda name: next((tool.input_schema for tool in tools() if tool.name == name), None))


async def stdio():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    if len(sys.argv) > 1:
        import uvicorn
        uvicorn.run(server.streamable_http_app(json_response=True), host="127.0.0.1", port=int(sys.argv[1]), log_level="critical")
    else:
        asyncio.run(stdio())
