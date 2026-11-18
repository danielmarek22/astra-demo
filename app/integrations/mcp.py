"""Manual MCP catalog adaptation into Astra's ordinary integration contracts."""

import hashlib
import json
import re
import threading
from dataclasses import dataclass, replace
from functools import partial

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError
from referencing import Registry

from app.config import MCPConfig, MCPServerConfig
from app.integrations.contracts import (
    ApprovalRequest,
    CapabilityId,
    RegisteredTool,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from app.integrations.mcp_generation import MCPServerGeneration
from app.integrations.mcp_runtime import MCPRuntime, MCPRuntimeError, tool_fingerprint


def capability_name(server_id: str, tool_name: str) -> CapabilityId:
    digest = hashlib.sha256(json.dumps([server_id, tool_name], ensure_ascii=True).encode()).hexdigest()[:32]
    stem = re.sub(r"[^a-z0-9]+", "_", f"{server_id}_{tool_name}".lower()).strip("_")[:26].rstrip("_")
    return CapabilityId("mcp", f"{stem}_{digest}")


class _SchemaIssue(ValueError):
    """Application-owned reason and schema location; never an SDK exception body."""

    def __init__(self, reason, path=()):
        super().__init__(reason)
        self.path = path


class MCPArgumentLimit(ValueError):
    """Finite input or validation work exceeded an application-owned bound."""


def validate_mcp_arguments(schema, arguments):
    """Keep the original Draft 2020-12 constraints, with a per-call work budget."""
    try:
        encoded = json.dumps(arguments, allow_nan=False)
        if len(encoded.encode()) > 65536:  # Absolute maximum configurable MCP argument limit.
            raise MCPArgumentLimit
        nodes = 0

        def bound(value, depth=0):
            nonlocal nodes
            nodes += 1
            if depth > 32 or nodes > 8192:
                raise MCPArgumentLimit
            if isinstance(value, dict):
                for child in value.values():
                    bound(child, depth + 1)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    bound(child, depth + 1)

        bound(arguments)
        remaining = 16384

        def budgeted(delegate):
            def validate(validator, value, instance, subschema):
                nonlocal remaining
                remaining -= 1
                if remaining < 0:
                    raise MCPArgumentLimit
                yield from delegate(validator, value, instance, subschema)
            return validate

        # The closure is private to this invocation, including all recursive descents.
        # Admission forbids nested dialect changes, so evolve cannot select an unbounded validator.
        bounded = validators.extend(Draft202012Validator, {
            keyword: budgeted(delegate) for keyword, delegate in Draft202012Validator.VALIDATORS.items()
        })
        bounded(schema, registry=Registry()).validate(arguments)
    except (TypeError, ValueError, RecursionError) as error:
        raise MCPArgumentLimit from error


def _validate_schema(schema, limit, *, allow_recursive=True):
    """Bounded local schemas; input recursion must consume instance structure."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise _SchemaIssue("MCP schemas must describe an object")
    encoded = json.dumps(schema, allow_nan=False)
    if len(encoded.encode()) > limit:
        raise _SchemaIssue("MCP schema exceeds the size limit")
    if schema.get("$schema", "https://json-schema.org/draft/2020-12/schema") not in {
        "https://json-schema.org/draft/2020-12/schema", "https://json-schema.org/draft/2020-12/schema#",
    }:
        raise _SchemaIssue("Unsupported MCP schema dialect", ("$schema",))
    count = 0

    def bound(item, depth=0):
        nonlocal count
        count += 1
        if depth > 32 or count > 8192:
            raise _SchemaIssue("MCP schema is too complex")
        if isinstance(item, dict):
            for child in item.values():
                bound(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                bound(child, depth + 1)

    bound(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise _SchemaIssue("Invalid JSON Schema 2020-12 definition", tuple(error.absolute_path)) from None

    def walk(item, active=(), depth=0, progress=0, path=()):
        nonlocal count
        count += 1
        if count > 16384 or depth > 32:
            raise _SchemaIssue("MCP schema reference expansion is excessive", path)
        if not isinstance(item, dict):
            return
        if path and "$schema" in item:
            raise _SchemaIssue("Nested schema dialect declarations are unsupported", (*path, "$schema"))
        for keyword in ("$id", "$dynamicRef", "$recursiveRef", "patternProperties"):
            if keyword in item:
                raise _SchemaIssue(f"Unsupported schema keyword {keyword}", (*path, keyword))
        if "pattern" in item and item["pattern"] != r"^\d{4}-\d{2}-\d{2}$":
            raise _SchemaIssue("Unsupported regular expression; only the fixed YYYY-MM-DD pattern is supported", (*path, "pattern"))
        if "$ref" in item:
            reference = item["$ref"]
            if not isinstance(reference, str) or not reference.startswith("#/"):
                raise _SchemaIssue("References must be local JSON pointers; external references are forbidden", (*path, "$ref"))
            target = schema
            target_path = ()
            try:
                for part in reference[2:].split("/"):
                    key = part.replace("~1", "/").replace("~0", "~")
                    target_path += (key,)
                    if isinstance(target, list):
                        if not re.fullmatch(r"0|[1-9][0-9]*", key):
                            raise KeyError
                        target = target[int(key)]
                    else:
                        target = target[key]
            except (KeyError, IndexError, TypeError):
                raise _SchemaIssue("Local JSON pointer does not resolve to a schema", (*path, "$ref")) from None
            if not isinstance(target, (dict, bool)):
                raise _SchemaIssue("Schema reference must resolve to a schema", (*path, "$ref"))
            previous = next((level for ref, level in reversed(active) if ref == reference), None)
            if previous is not None:
                if not allow_recursive:
                    raise _SchemaIssue("Recursive output schemas are unsupported", (*path, "$ref"))
                if progress <= previous:
                    raise _SchemaIssue("Reference cycle must descend into an object property or array item", (*path, "$ref"))
            else:
                walk(target, (*active, (reference, progress)), depth + 1, progress, target_path)
        for keyword in ("properties", "$defs", "definitions", "dependentSchemas"):
            for key, child in item.get(keyword, {}).items():
                walk(child, active, depth + 1, progress + (keyword == "properties"), (*path, keyword, key))
        for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
            for index, child in enumerate(item.get(keyword, [])):
                walk(child, active, depth + 1, progress + (keyword == "prefixItems"), (*path, keyword, index))
        for keyword in ("items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems", "propertyNames", "contains", "not", "if", "then", "else"):
            if keyword in item:
                consumes = keyword in {"items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems", "propertyNames", "contains"}
                walk(item[keyword], active, depth + 1, progress + consumes, (*path, keyword))
    walk(schema)


@dataclass(frozen=True)
class _Binding:
    server_id: str
    name: str
    fingerprint: str
    definition_json: str
    enabled: bool
    approval_required: bool
    max_result_chars: int
    generation: MCPServerGeneration


class MCPIntegration:
    name = "mcp"
    redact_logs = True

    def __init__(self, runtime: MCPRuntime):
        self.runtime = runtime
        self._lock = threading.RLock()
        self._closed = False
        self._refresh_slots = {key: threading.Lock() for key in runtime.server_ids()}
        self._generations = {key: MCPServerGeneration(runtime, key) for key in runtime.server_ids()}
        self._all_generations = list(self._generations.values())
        self._configuration = runtime.configuration()
        self._catalog_revision = 0
        self._bindings: dict[CapabilityId, _Binding] = {}
        self._seen = {(server, name): fingerprint for server in runtime.server_ids()
                      for name, fingerprint in runtime.server_settings(server)["tool_fingerprints"].items()}
        self._issues = {}
        self._enabled_names = {key: set(runtime.server_settings(key)["enabled_tools"]) for key in runtime.server_ids()}
        self._approvals = {key: runtime.server_settings(key)["approval_required"] for key in runtime.server_ids()}
        self._tool_approvals = {key: runtime.server_settings(key)["tool_approvals"] for key in runtime.server_ids()}

    def _registered(self, capability, binding):
        definition = json.loads(binding.definition_json)
        runtime = binding.generation.runtime
        return RegisteredTool(
            ToolSpec(capability, runtime.redact(definition.get("description") or f"External tool {binding.name}")[:4096],
                     definition["input_schema"]),
            partial(self._invoke, capability, binding),
            available=lambda: runtime.status(binding.server_id).state == "connected",
            enabled=binding.enabled, redact_logs=True,
            catalog_resource=binding.generation if binding.enabled else None,
        )

    def registered_tools(self):
        with self._lock:
            return [self._registered(capability, binding) for capability, binding in self._bindings.items()]

    def catalog_issues(self):
        with self._lock:
            return dict(self._issues)

    def refresh_catalog(self, server_id, registry):
        slot = self._refresh_slots[server_id]
        if not slot.acquire(timeout=self.runtime.queue_timeout_s):
            raise MCPRuntimeError("busy", "Another MCP catalog refresh is still running.")
        try:
            return self._refresh_catalog(server_id, registry)
        finally:
            slot.release()

    def _refresh_catalog(self, server_id, registry):
        # Network operations hold neither the registry nor adapter publication lock.
        with self._lock:
            self._require_open()
            generation = self._generations[server_id]
        runtime = generation.runtime
        definitions = runtime.refresh_catalog(server_id)
        settings = runtime.server_settings(server_id)
        with self._lock:
            self._require_open()
            if self._generations.get(server_id) is not generation:
                raise MCPRuntimeError("configuration_changed", "MCP configuration changed; reload before refreshing.")
            bindings = {cap: replace(item, enabled=False) if item.server_id == server_id else item
                        for cap, item in self._bindings.items()}
            seen, selected = dict(self._seen), set(self._enabled_names[server_id])
            issues = {key: value for key, value in self._issues.items() if key[0] != server_id}
            present = set()
            for definition in definitions:
                name = definition["name"]
                key = (server_id, name)
                present.add(name)
                capability = capability_name(server_id, name)
                existing = bindings.get(capability)
                if existing is not None and (existing.server_id, existing.name) != key:
                    raise MCPRuntimeError("name_collision", "MCP capability mapping collision; catalog was not published.")
                field = "input_schema"
                try:
                    _validate_schema(definition["input_schema"], settings["max_schema_bytes"])
                    if definition.get("output_schema") is not None:
                        field = "output_schema"
                        _validate_schema(definition["output_schema"], settings["max_schema_bytes"], allow_recursive=False)
                    if runtime.redact(definition, credential_fields=False) != definition:
                        raise ValueError("MCP definition contains configured secret values")
                    fingerprint = tool_fingerprint(definition)
                    if key in seen and seen[key] != fingerprint:
                        selected.discard(name)
                        issues[key] = "schema_changed: explicit enablement review required"
                    elif name not in selected and self._issues.get(key, "").startswith("schema_changed"):
                        issues[key] = self._issues[key]
                    seen[key] = fingerprint
                    bindings[capability] = _Binding(
                        server_id, name, fingerprint, json.dumps(definition, allow_nan=False),
                        name in selected and settings["enabled"],
                        self._tool_approvals[server_id].get(name, self._approvals[server_id]),
                        settings["max_result_chars"], generation,
                    )
                except (ValueError, TypeError, KeyError, IndexError, SchemaError, RecursionError) as error:
                    selected.discard(name)
                    seen[key] = None
                    if isinstance(error, _SchemaIssue):
                        path = "/".join(str(runtime.redact(str(part))).replace("~", "~0").replace("/", "~1") for part in error.path)
                        issues[key] = runtime.redact(f"invalid_schema: {field}#/{path[:512]}: {error}")
                    else:
                        issues[key] = "invalid_schema: definition is invalid, excessive, or contains configured secrets"
            for key in tuple(seen):
                if key[0] == server_id and key[1] not in present:
                    selected.discard(key[1])
                    seen[key] = None
                    issues[key] = "removed: tool is no longer advertised"
            registry.replace_tools(self, [self._registered(cap, binding) for cap, binding in bindings.items()])
            self._bindings, self._seen, self._issues = bindings, seen, issues
            self._enabled_names[server_id] = selected
            generation.catalog_loaded = True
            self._catalog_revision += 1
            return self.registered_tools()

    def configure_tools(self, server_id, enabled_tools, registry, *, approval_required=None):
        """Trusted local manual configuration; persistence/UI belong to Increment 3."""
        settings = self._generations[server_id].runtime.server_settings(server_id)
        settings["enabled_tools"] = enabled_tools
        settings["tool_fingerprints"] = {}  # This explicit manual review accepts the current contracts.
        with self._lock:
            self._require_open()
            settings["approval_required"] = self._approvals[server_id] if approval_required is None else approval_required
            validated = MCPServerConfig.model_validate(settings)
            selected = set(validated.enabled_tools)
            known = {item.name for item in self._bindings.values() if item.server_id == server_id
                     and self._seen.get((server_id, item.name)) == item.fingerprint}
            if not selected <= known:
                raise ValueError("Only supported tools in the current catalog may be enabled")
            bindings = {
                cap: replace(item, enabled=item.name in selected and settings["enabled"],
                             approval_required=validated.approval_required)
                if item.server_id == server_id else item for cap, item in self._bindings.items()
            }
            registry.replace_tools(self, [self._registered(cap, item) for cap, item in bindings.items()])
            self._bindings = bindings
            self._enabled_names[server_id] = selected
            self._approvals[server_id] = validated.approval_required
            self._catalog_revision += 1

    def apply_configuration(self, settings, registry, *, persist):
        """Publish validated local configuration after its MCP-only write succeeds.

        The management service serializes this with refresh and manual reviews.
        Retired generations remain bound to admitted catalogs until they drain.
        """
        settings = MCPConfig.model_validate(settings).model_dump(exclude_none=True)
        with self._lock:
            self._require_open()
            generations = {}
            changed = set()
            policy_fields = {"description", "enabled_tools", "tool_fingerprints", "tool_approvals", "approval_required"}
            for server, spec in settings["servers"].items():
                previous = self._configuration["servers"].get(server, {})
                connection_changed = (
                    server not in self._generations
                    or settings["enabled"] != self._configuration["enabled"]
                    or settings["queue_timeout_s"] != self._configuration["queue_timeout_s"]
                    or {key: value for key, value in spec.items() if key not in policy_fields}
                    != {key: value for key, value in previous.items() if key not in policy_fields}
                )
                if connection_changed:
                    runtime = MCPRuntime({**settings, "servers": {server: spec}}, redaction_source=self.runtime)
                    generations[server] = MCPServerGeneration(runtime, server, owns_runtime=True)
                    changed.add(server)
                else:
                    generations[server] = self._generations[server]
            bindings = {}
            for cap, binding in self._bindings.items():
                spec = settings["servers"].get(binding.server_id)
                generation = generations.get(binding.server_id, binding.generation)
                enabled = bool(spec and settings["enabled"] and spec["enabled"]
                               and binding.server_id not in changed
                               and generation.catalog_loaded
                               and binding.name in spec["enabled_tools"]
                               and spec["tool_fingerprints"].get(binding.name) == binding.fingerprint
                               and self._seen.get((binding.server_id, binding.name)) == binding.fingerprint)
                bindings[cap] = replace(
                    binding, enabled=enabled, generation=generation,
                    approval_required=spec["tool_approvals"].get(binding.name, spec["approval_required"]) if spec else True,
                )
            tools = [self._registered(cap, binding) for cap, binding in bindings.items()]
            # Validate before disk writes, including registry ownership/namespace rules.
            registry._validated_tools(self.name, tools)
            persist(settings)
            registry.replace_tools(self, tools)
            retired = set(self._generations.values()) - set(generations.values())
            self._all_generations.extend(generation for generation in generations.values()
                                         if generation not in self._all_generations)
            self._all_generations = [generation for generation in self._all_generations
                                     if not generation.closed.is_set() or generation.cleanup_failed]
            self._generations = generations
            self._bindings = bindings
            self._configuration = settings
            self._enabled_names = {key: set(spec["enabled_tools"]) for key, spec in settings["servers"].items()}
            self._approvals = {key: spec["approval_required"] for key, spec in settings["servers"].items()}
            self._tool_approvals = {key: spec["tool_approvals"] for key, spec in settings["servers"].items()}
            self._refresh_slots = {key: self._refresh_slots.get(key, threading.Lock()) for key in generations}
            self._catalog_revision += 1
            for binding in bindings.values():
                if binding.enabled:
                    self._issues.pop((binding.server_id, binding.name), None)
        for generation in retired:
            generation.retire()

    def management_view(self):
        """Safe configuration/catalog metadata, never resolved credentials or SDK errors."""
        from dataclasses import asdict
        with self._lock:
            servers = []
            for server, spec in self._configuration["servers"].items():
                runtime = self._generations[server].runtime
                tools = []
                for capability, binding in self._bindings.items():
                    if binding.server_id != server:
                        continue
                    definition = json.loads(binding.definition_json)
                    issue = self._issues.get((server, binding.name))
                    valid = (self._generations[server].catalog_loaded
                             and self._seen.get((server, binding.name)) == binding.fingerprint)
                    tools.append({"name": runtime.redact(binding.name), "capability": str(capability),
                                  "description": runtime.redact(definition.get("description", "")),
                                  "definition": runtime.redact({key: definition[key] for key in (
                                      "name", "title", "description", "input_schema", "output_schema", "annotations",
                                  ) if key in definition}, credential_fields=False),
                                  "fingerprint": binding.fingerprint, "enabled": binding.enabled,
                                  "selected": (binding.name in spec["enabled_tools"]
                                               and spec["tool_fingerprints"].get(binding.name) == binding.fingerprint),
                                  "approval_required": binding.approval_required, "valid": valid,
                                  "issue": runtime.redact(issue), "pending_review": bool(issue and issue.startswith("schema_changed"))})
                known = {item["name"] for item in tools}
                for (owner, name), issue in self._issues.items():
                    if owner == server and name not in known:
                        tools.append({"name": runtime.redact(name), "description": "Tool definition rejected. See the validation reason below.",
                                      "enabled": False, "valid": False, "pending_review": False,
                                      "approval_required": True, "issue": runtime.redact(issue), "fingerprint": None})
                servers.append({"id": server, "config": runtime.redact(spec, credential_fields=False),
                                "status": asdict(runtime.status(server)), "tools": tools})
            return {"enabled": self._configuration["enabled"], "catalog_revision": self._catalog_revision,
                    "servers": servers, "cleanup_pending": sum(not item.closed.is_set() for item in self._all_generations
                                                              if item not in self._generations.values()),
                    "cleanup_failed": any(item.cleanup_failed for item in self._all_generations)}

    def enabled_tool_fingerprints(self, server_id):
        """Local configuration export; contains no resolved credential values."""
        with self._lock:
            return {binding.name: binding.fingerprint for binding in self._bindings.values()
                    if binding.server_id == server_id and binding.enabled}

    def _invoke(self, capability, binding, arguments, context):
        runtime = binding.generation.runtime
        provenance = {"server_id": binding.server_id, "tool_name": binding.name,
                      "capability": str(capability), "schema_fingerprint": binding.fingerprint}
        diagnostics = {**provenance, "redact_logs": True, "completion": "not_started"}
        try:
            encoded = json.dumps(arguments, allow_nan=False)
            arguments = json.loads(encoded)
            if len(encoded.encode()) > runtime.server_settings(binding.server_id)["max_argument_bytes"]:
                diagnostics["category"] = "argument_limit"
                return ToolResult.error("MCP arguments exceeded the configured limit.", diagnostics)
            if binding.approval_required:
                request = ApprovalRequest(
                    capability, "Approve external tool?", "This MCP operation requires local approval.",
                    f"{binding.server_id} / {binding.name}", json.dumps(runtime.redact(arguments), ensure_ascii=True),
                )
                if context.approval_callback is None or not context.approval_callback(request):
                    diagnostics["category"] = "approval_denied"
                    return ToolResult(ToolResultStatus.DENIED, "MCP operation was not approved.", diagnostics=runtime.redact(diagnostics))
            result = runtime.invoke(binding.server_id, binding.name, arguments, binding.fingerprint)
            diagnostics["completion"] = "confirmed"
            return self._normalize(binding, result, provenance)
        except MCPRuntimeError as error:
            diagnostics.update(category=error.category, completion=error.completion)
            if error.completion == "uncertain":
                diagnostics["suppress_for_turn"] = True
            content = str(error)
            if error.completion == "uncertain" and "uncertain" not in content:
                content += " Completion is uncertain; do not retry automatically."
            if error.category in {"disabled", "closed", "schema_changed", "credentials_missing", "authentication_failed", "connection_failed"} and error.completion == "not_started":
                return ToolResult(ToolResultStatus.UNAVAILABLE, content, diagnostics=runtime.redact(diagnostics))
            return ToolResult.error(content, runtime.redact(diagnostics))
        except (ValueError, TypeError, RecursionError):
            return ToolResult.error("MCP arguments or response were not supported finite JSON data.", diagnostics)

    def _normalize(self, binding, result, provenance):
        runtime = binding.generation.runtime
        diagnostics = {**provenance, "redact_logs": True, "completion": "confirmed"}
        if result.get("result_type") == "input_required" or "input_requests" in result:
            diagnostics.update(category="unsupported_input", completion="uncertain", suppress_for_turn=True)
            return ToolResult.error("MCP requested unsupported client input; completion is uncertain. No follow-up request was sent.", diagnostics)
        content, unsupported = [], set()
        for block in result.get("content", []):
            if block.get("type") == "text":
                content.append({"type": "text", "text": runtime.redact(block.get("text", ""))})
            elif block.get("type") == "resource_link":
                content.append({key: runtime.redact(block[key]) for key in ("type", "uri", "name", "title", "mime_type") if key in block})
            else:
                unsupported.add(block.get("type", "unknown"))
        payload = {"kind": "mcp_tool_result", **{key: runtime.redact(value) for key, value in provenance.items()}, "content": content,
                   "structured_content": runtime.redact(result.get("structured_content")), "truncated": False}
        category = "mcp_tool_error" if result.get("is_error") else "unsupported_content" if unsupported else "success"
        if unsupported:
            payload["unsupported_content"] = sorted(unsupported)
        render = lambda: json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if len(render()) > binding.max_result_chars:
            payload.update(truncated=True, structured_content=None, structured_content_omitted=True)
            text = "\n".join(block.get("text", "") for block in content)
            payload["content"] = [{"type": "text", "text": ""}]
            remaining = max(0, binding.max_result_chars - len(render()) - 64)
            payload["content"][0]["text"] = runtime.redact(text[:remaining])
            while len(render()) > binding.max_result_chars and payload["content"][0]["text"]:
                payload["content"][0]["text"] = payload["content"][0]["text"][:len(payload["content"][0]["text"]) // 2]
        diagnostics.update(category=category, truncated=payload["truncated"])
        if category == "success":
            return ToolResult.success(render(), diagnostics=runtime.redact(diagnostics))
        return ToolResult.error(render(), runtime.redact(diagnostics))

    def _require_open(self):
        if self._closed:
            raise MCPRuntimeError("closed", "The MCP integration is closed.")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            runtimes = {id(generation.runtime): generation.runtime for generation in self._all_generations}
            runtimes[id(self.runtime)] = self.runtime
        failure = None
        for runtime in runtimes.values():
            try:
                runtime.close()
            except MCPRuntimeError as error:
                failure = error
        if failure is not None:
            raise failure
