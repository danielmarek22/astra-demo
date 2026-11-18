"""Serialized host management of MCP configuration, publication and connections."""

import threading
from contextlib import contextmanager
from copy import deepcopy

from pydantic import ValidationError

from app.config import MCPConfig, MCPServerConfig
from app.integrations.mcp_config_store import MCPManagementError


class MCPManagement:
    def __init__(self, integration, registry, store):
        self.integration, self.registry, self.store = integration, registry, store
        self._config = deepcopy(integration._configuration)
        self._lock = threading.Lock()

    @contextmanager
    def _operation(self):
        if not self._lock.acquire(timeout=5):
            raise MCPManagementError("busy", "Another MCP management operation is still running.", 409)
        try:
            yield
        finally:
            self._lock.release()

    def _revision(self, expected=None):
        config, revision = self.store.read()
        if config != self._config:
            raise MCPManagementError("external_configuration_change", "MCP was edited outside this application. Restart Astra to load that configuration.", 409)
        if expected is not None and expected != revision:
            raise MCPManagementError("stale_revision", "Configuration changed. Reload MCP before saving again.", 409)
        return revision

    def _view(self):
        return {**self.integration.management_view(), "revision": self._revision()}

    def view(self):
        with self._operation():
            return self._view()

    def _save(self, config, revision):
        config = MCPConfig.model_validate(config).model_dump(exclude_none=True)
        self.integration.apply_configuration(config, self.registry,
                                             persist=lambda candidate: self.store.write(candidate, revision))
        self._config = config

    def _server(self, server_id):
        if server_id not in self._config["servers"]:
            raise MCPManagementError("unknown_server", "The MCP server is not configured.", 404)
        return self._config["servers"][server_id]

    def change(self, operation, payload, server_id=None):
        with self._operation():
            revision = self._revision(payload.get("revision"))
            if not isinstance(payload.get("revision"), str):
                raise MCPManagementError("revision_required", "Reload MCP before changing its configuration.", 409)
            config = deepcopy(self._config)
            if operation == "global":
                if type(payload.get("enabled")) is not bool:
                    raise ValueError
                config["enabled"] = payload["enabled"]
            elif operation == "create":
                server_id = payload.get("id")
                if server_id in config["servers"]:
                    raise MCPManagementError("duplicate_server", "That MCP server ID already exists.", 409)
                spec = self._server_config(payload.get("config"), creating=True)
                config["servers"][server_id] = spec
            else:
                previous = self._server(server_id)
                if operation == "edit":
                    spec = self._server_config(payload.get("config"))
                    # Policy changes have their own catalog-guarded endpoint.
                    for key in ("enabled_tools", "tool_fingerprints", "tool_approvals"):
                        spec[key] = deepcopy(previous[key])
                    connection_keys = {"transport", "command", "args", "cwd", "env", "url", "headers"}
                    if any(previous.get(key) != spec.get(key) for key in connection_keys):
                        spec.update(enabled_tools=[], tool_fingerprints={}, tool_approvals={})
                    config["servers"][server_id] = spec
                elif operation == "remove":
                    del config["servers"][server_id]
                elif operation == "tools":
                    view = self.integration.management_view()
                    if type(payload.get("catalog_revision")) is not int or payload["catalog_revision"] != view["catalog_revision"]:
                        raise MCPManagementError("stale_catalog", "The tool catalog changed. Reload and review the current tools.", 409)
                    tools = {tool["name"]: tool for server in view["servers"] if server["id"] == server_id
                             for tool in server["tools"] if tool["valid"] or tool.get("selected")}
                    selected = payload.get("enabled_tools")
                    fingerprints = payload.get("fingerprints")
                    approvals = payload.get("tool_approvals", {})
                    if not isinstance(selected, list) or not isinstance(fingerprints, dict) or not isinstance(approvals, dict):
                        raise ValueError
                    if not all(isinstance(name, str) for name in selected) or not set(selected) <= tools.keys() or not approvals.keys() <= tools.keys():
                        raise MCPManagementError("unsupported_tool", "Only validated tools in the current catalog may be selected.", 409)
                    if set(fingerprints) != set(selected) or any(fingerprints[name] != tools[name]["fingerprint"] for name in selected):
                        raise MCPManagementError("stale_catalog", "A selected contract changed. Reload and review it before enabling.", 409)
                    config["servers"][server_id].update(
                        enabled_tools=selected, tool_fingerprints=fingerprints, tool_approvals=approvals,
                        approval_required=payload.get("approval_required", previous["approval_required"]),
                    )
                elif operation in {"connect", "disconnect", "refresh"}:
                    runtime = self.integration._generations[server_id].runtime
                    if operation == "refresh":
                        self.integration.refresh_catalog(server_id, self.registry)
                        pins = self.integration.enabled_tool_fingerprints(server_id)
                        config["servers"][server_id].update(enabled_tools=sorted(pins), tool_fingerprints=pins)
                        self._save(config, revision)
                    else:
                        getattr(runtime, operation)(server_id)
                    return self._view()
                else:
                    raise ValueError
            self._save(config, revision)
            return self._view()

    @staticmethod
    def _server_config(raw, *, creating=False):
        if not isinstance(raw, dict) or set(raw) & {"enabled_tools", "tool_fingerprints", "tool_approvals"}:
            raise ValueError
        spec = MCPServerConfig.model_validate(raw).model_dump(exclude_none=True)
        if creating:
            spec.update(enabled_tools=[], tool_fingerprints={}, tool_approvals={})
        return spec


def validation_message(error):
    """Do not echo rejected input values or validation contexts to clients/logs."""
    if isinstance(error, ValidationError):
        fields = sorted({str(item["loc"][0]) for item in error.errors(include_input=False, include_context=False) if item["loc"]})
        return "Invalid MCP configuration fields: " + ", ".join(fields[:12]) + "."
    return "Invalid MCP management request. Check the configuration fields."
