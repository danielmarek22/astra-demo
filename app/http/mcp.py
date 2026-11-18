"""Same-application MCP management endpoints; all synchronous work is offloaded."""

import hmac
import ipaddress
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.integrations.mcp_config_store import MCPManagementError
from app.integrations.mcp_management import validation_message
from app.integrations.mcp_runtime import MCPRuntimeError

router = APIRouter(prefix="/api/mcp")


def _boundary(request, *, write=False):
    # DNS rebinding and cross-site forms must not reach a local process launcher.
    host = request.url.hostname
    configured = getattr(getattr(request.app.state, "settings", None), "server", {}).get("host", "127.0.0.1")
    allowed = {"localhost", "127.0.0.1", "::1"}
    if configured not in {"0.0.0.0", "::"}:
        allowed.add(configured)
    else:
        try:
            ipaddress.ip_address(host)
            allowed.add(host)
        except ValueError:
            pass
    origin = request.headers.get("origin")
    if (host not in allowed or request.headers.get("x-astra-mcp-client") != "1"
            or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"):
        raise HTTPException(403, "MCP management requires the Astra application.")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "MCP management requires the same application origin.")
    if write:
        token = getattr(request.app.state, "mcp_token", "")
        if not token or not hmac.compare_digest(request.headers.get("x-astra-mcp-token", ""), token):
            raise HTTPException(403, "Reload the MCP tab before changing configuration.")
        if request.headers.get("content-type", "").lower().split(";", 1)[0].strip() != "application/json":
            raise HTTPException(415, "MCP management requests require JSON.")


def _response(data):
    return JSONResponse(data, headers={"Cache-Control": "no-store", "Vary": "Origin", "X-Content-Type-Options": "nosniff"})


async def _perform(request, operation, server_id=None):
    _boundary(request, write=True)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 256 * 1024:
            raise HTTPException(413, "MCP management request is too large.")
        body.extend(chunk)
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise TypeError
        permitted = {
            "global": {"revision", "enabled"},
            "create": {"revision", "id", "config"},
            "edit": {"revision", "config"},
            "tools": {"revision", "catalog_revision", "enabled_tools", "fingerprints", "tool_approvals", "approval_required"},
        }.get(operation, {"revision"})
        if not payload.keys() <= permitted:
            raise ValueError
        result = await request.app.state.run_mcp(request, "change", operation, payload, server_id)
    except MCPManagementError as error:
        return _response_error(error.status, error.category, str(error))
    except MCPRuntimeError as error:
        return _response_error(503, error.category, str(error))
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        return _response_error(422, "invalid_configuration", validation_message(error))
    return _response(result)


def _response_error(status, category, message):
    return JSONResponse({"error": {"category": category, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


@router.get("")
async def get_mcp(request: Request):
    _boundary(request)
    try:
        data = await request.app.state.run_mcp(request, "view")
    except MCPManagementError as error:
        return _response_error(error.status, error.category, str(error))
    return _response({**data, "csrf_token": request.app.state.mcp_token})


@router.put("")
async def set_mcp(request: Request):
    return await _perform(request, "global")


@router.post("/servers")
async def create_server(request: Request):
    return await _perform(request, "create")


@router.put("/servers/{server_id}")
async def edit_server(server_id: str, request: Request):
    return await _perform(request, "edit", server_id)


@router.delete("/servers/{server_id}")
async def remove_server(server_id: str, request: Request):
    return await _perform(request, "remove", server_id)


@router.put("/servers/{server_id}/tools")
async def select_tools(server_id: str, request: Request):
    return await _perform(request, "tools", server_id)


@router.post("/servers/{server_id}/{operation}")
async def server_operation(server_id: str, operation: str, request: Request):
    if operation not in {"connect", "disconnect", "refresh"}:
        raise HTTPException(404, "Unknown MCP management operation.")
    return await _perform(request, operation, server_id)
