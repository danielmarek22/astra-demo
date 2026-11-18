import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.core.execution_state import ExecutionLimits, ExecutionSettings
from app.core.prompt_budget import PromptBudget, tool_groups
from app.core.response_generator import ResponseGenerator
from app.core.tool_executor import ToolExecutor
from app.integrations import (
    CapabilityId,
    IntegrationRegistry,
    InvocationContext,
    RegisteredTool,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.integrations.mcp import MCPIntegration, capability_name
from app.integrations.mcp_runtime import MCPRuntime, tool_fingerprint
from app.llm.ollama_stream import OllamaClient
from tests.support import BufferedResultMock, consume_generator, make_response
from tests.test_mcp_runtime import process_exists

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_tools_server.py"


def definition(name="echo", schema=None, **kwargs):
    return {"name": name, "description": f"Use {name}", "input_schema": schema or {
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False,
    }, **kwargs}


class MCPAdapterTests(unittest.TestCase):
    def setUp(self):
        self.definitions = [definition(), definition("disabled")]
        self.runtime = MCPRuntime({"enabled": True, "servers": {"demo": {
            "enabled": True, "transport": "stdio", "command": sys.executable,
            "enabled_tools": ["echo"], "approval_required": False,
        }}})
        self.addCleanup(self.runtime.close)
        self.runtime._publish("demo", "connected")
        self.runtime.refresh_catalog = Mock(side_effect=lambda _id: deepcopy(self.definitions))
        self.runtime.invoke = Mock(return_value={"content": [{"type": "text", "text": "done"}], "is_error": False})
        self.adapter = MCPIntegration(self.runtime)
        self.registry = IntegrationRegistry([self.adapter])
        self.adapter.refresh_catalog("demo", self.registry)
        self.capability = capability_name("demo", "echo")
        self.call = ToolCall(self.capability, {"text": "hello"})

    def invoke(self, call=None, **context):
        return self.registry.invoke(call or self.call, InvocationContext("session", "test", **context))

    def test_mapping_is_stable_bounded_and_collision_resistant(self):
        names = ["echo", "ECHO", "a-b", "a.b", "a_b", "a__b", "x" * 128, "☃"]
        caps = [capability_name("demo", name) for name in names]
        self.assertEqual(len(set(caps)), len(names))
        for name, cap in zip(names, caps):
            self.assertEqual(cap, capability_name("demo", name))
            self.assertLessEqual(len(str(cap)), 64)
            self.assertEqual(CapabilityId.parse(str(cap)), cap)
        self.assertNotEqual(capability_name("other", "echo"), self.capability)

    def test_configured_fingerprint_detects_drift_on_first_refresh_after_restart(self):
        expected = tool_fingerprint(definition())
        self.assertEqual(self.adapter.enabled_tool_fingerprints("demo"), {"echo": expected})
        settings = self.runtime.server_settings("demo")
        settings["tool_fingerprints"] = {"echo": expected}
        restarted = MCPRuntime({"enabled": True, "servers": {"demo": settings}})
        self.addCleanup(restarted.close)
        restarted._publish("demo", "connected")
        changed = definition()
        changed["description"] = "Changed operation contract"
        restarted.refresh_catalog = Mock(return_value=[changed])
        adapter = MCPIntegration(restarted)
        registry = IntegrationRegistry([adapter])
        adapter.refresh_catalog("demo", registry)
        self.assertEqual(registry.invoke(self.call, InvocationContext("s", "")).status.value, "denied")
        self.assertIn("schema_changed", adapter.catalog_issues()[("demo", "echo")])
        adapter.configure_tools("demo", ["echo"], registry)
        self.assertEqual(adapter.enabled_tool_fingerprints("demo"), {"echo": tool_fingerprint(changed)})

    def test_forced_mapping_collision_does_not_publish_a_partial_catalog(self):
        before = self.registry.capture_tool_catalog()
        with patch("app.integrations.mcp.capability_name", return_value=self.capability), self.assertRaises(Exception) as error:
            self.adapter.refresh_catalog("demo", self.registry)
        self.assertEqual(error.exception.category, "name_collision")
        self.assertEqual(self.registry.capture_tool_catalog().revision, before.revision)
        self.assertEqual(self.invoke(tool_catalog=before).status.value, "success")

    def test_refresh_holds_no_registry_lock_and_same_server_publication_is_ordered(self):
        started, release, second = threading.Event(), threading.Event(), threading.Event()
        count = 0
        def listing(_server):
            nonlocal count
            count += 1
            if count == 1:
                started.set()
                self.assertTrue(release.wait(3))
            else:
                second.set()
            return deepcopy(self.definitions)
        self.runtime.refresh_catalog.side_effect = listing
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.adapter.refresh_catalog, "demo", self.registry)
            self.assertTrue(started.wait(2))
            later = pool.submit(self.adapter.refresh_catalog, "demo", self.registry)
            snapshot = pool.submit(self.registry.capture_tool_catalog).result(timeout=1)
            self.assertFalse(second.wait(0.05))
            self.assertEqual(self.invoke(tool_catalog=snapshot).status.value, "success")
            release.set()
            first.result(timeout=3)
            later.result(timeout=3)
            self.assertTrue(second.is_set())

    def test_approval_cannot_mutate_prevalidated_arguments_or_admitted_enablement(self):
        schema = {"type": "object", "properties": {"payload": {"type": "object", "properties": {
            "value": {"type": "integer", "maximum": 1}}, "required": ["value"]}}, "required": ["payload"]}
        self.definitions = [definition("nested", schema)]
        self.adapter.refresh_catalog("demo", self.registry)
        self.adapter.configure_tools("demo", ["nested"], self.registry, approval_required=True)
        arguments = {"payload": {"value": 1}}
        def approve(_request):
            arguments["payload"]["value"] = 999
            self.adapter.configure_tools("demo", [], self.registry)
            return True
        nested = ToolCall(capability_name("demo", "nested"), arguments)
        self.assertEqual(self.invoke(nested, approval_callback=approve).status.value, "success")
        self.assertEqual(self.runtime.invoke.call_args.args[2], {"payload": {"value": 1}})
        self.assertEqual(self.invoke(nested, approval_callback=approve).status.value, "denied")

    def test_enablement_authority_and_approval_are_independent(self):
        self.assertEqual([tool["function"]["name"] for tool in self.registry.get_native_tools()], [str(self.capability)])
        disabled = ToolCall(capability_name("demo", "disabled"), {"text": "x"})
        self.assertEqual(self.invoke(disabled).status.value, "denied")
        self.assertEqual(self.invoke(allowed_capabilities=frozenset()).status.value, "denied")
        self.assertEqual(self.invoke(event_id="event").status.value, "denied")
        self.assertEqual(self.registry.get_native_tools(invocation_context=InvocationContext("s", "", allowed_capabilities=frozenset())), [])
        self.runtime.invoke.assert_not_called()
        self.adapter.configure_tools("demo", ["echo"], self.registry, approval_required=True)
        self.assertEqual(self.invoke().status.value, "denied")
        approved = Mock(return_value=True)
        self.assertEqual(self.invoke(approval_callback=approved, allowed_capabilities=frozenset({self.capability})).status.value, "success")
        self.assertEqual(approved.call_args.args[0].capability, self.capability)
        self.assertEqual(self.runtime.invoke.call_count, 1)

    def test_arguments_are_validated_before_approval_or_dispatch(self):
        self.adapter.configure_tools("demo", ["echo"], self.registry, approval_required=True)
        approval = Mock(return_value=True)
        for arguments in ({}, {"text": 1}, {"text": "x", "extra": True}):
            result = self.invoke(ToolCall(self.capability, arguments), approval_callback=approval)
            self.assertEqual(result.status.value, "error")
        approval.assert_not_called()
        self.runtime.invoke.assert_not_called()

    def test_enable_disable_and_approval_policy_use_admitted_snapshots(self):
        old = self.registry.capture_tool_catalog()
        self.adapter.configure_tools("demo", ["disabled"], self.registry, approval_required=True)
        self.assertEqual(self.invoke().status.value, "denied")
        self.assertEqual(self.invoke(tool_catalog=old).status.value, "success")
        old_names = self.registry.get_native_tools(invocation_context=InvocationContext("s", "", tool_catalog=old))
        self.assertEqual([item["function"]["name"] for item in old_names], [str(self.capability)])
        forged = ToolCall(capability_name("demo", "disabled"), {"text": "x"})
        self.assertEqual(self.invoke(forged, tool_catalog=old, approval_callback=lambda _: True).status.value, "denied")
        self.assertEqual(self.invoke(forged).status.value, "denied")  # Enabled but still needs approval.
        self.assertEqual(self.invoke(forged, approval_callback=lambda _: True).status.value, "success")

    def test_schema_drift_removal_and_new_tools_never_inherit_enablement(self):
        old = self.registry.capture_tool_catalog()
        self.definitions[0]["input_schema"]["properties"]["revision"] = {"type": "integer"}
        self.definitions.append(definition("new"))
        self.adapter.refresh_catalog("demo", self.registry)
        self.assertEqual(self.invoke().status.value, "denied")
        self.assertIn("schema_changed", self.adapter.catalog_issues()[("demo", "echo")])
        self.assertEqual(self.invoke(tool_catalog=old).status.value, "success")
        self.adapter.configure_tools("demo", ["echo"], self.registry)
        self.assertEqual(self.invoke().status.value, "success")
        self.definitions = [definition("new")]
        self.adapter.refresh_catalog("demo", self.registry)
        self.assertEqual(self.invoke().status.value, "denied")
        self.definitions = [definition(), definition("new")]
        self.adapter.refresh_catalog("demo", self.registry)
        self.assertEqual(self.invoke().status.value, "denied")
        self.assertEqual(self.registry.get_native_tools(), [])

    def test_invalid_schemas_are_isolated_and_local_refs_work(self):
        valid = {"type": "object", "$defs": {"label": {"type": "string"}},
                 "properties": {"text": {"$ref": "#/$defs/label"}, "pattern": {"type": "string"}}, "required": ["text"]}
        bad = [
            {"type": "string"}, {"type": "object", "$ref": "https://example.com/schema"},
            {"type": "object", "$ref": "#/missing"}, {"type": "object", "$ref": "#/properties/x", "properties": {"x": {"$ref": "#/properties/x"}}},
            {"type": "object", "properties": {"text": {"pattern": "(a+)+$"}}},
            {"type": "object", "$schema": "http://json-schema.org/draft-07/schema#"},
        ]
        self.definitions = [definition("good", valid), *[definition(f"bad{index}", item) for index, item in enumerate(bad)]]
        self.adapter.refresh_catalog("demo", self.registry)
        self.adapter.configure_tools("demo", ["good"], self.registry)
        good = ToolCall(capability_name("demo", "good"), {"text": "value", "pattern": "allowed name"})
        self.assertEqual(self.invoke(good).status.value, "success")
        self.assertEqual(len(self.adapter.catalog_issues()), len(bad) + 2)  # Includes removed echo/disabled.
        with self.assertRaises(ValueError):
            self.adapter.configure_tools("demo", ["bad0"], self.registry)

    def test_registry_snapshot_copy_isolation_owner_checks_and_atomic_replacement(self):
        snapshot = self.registry.capture_tool_catalog()
        exposed = self.registry.get_native_tools(invocation_context=InvocationContext("s", "", tool_catalog=snapshot))
        exposed[0]["function"]["parameters"]["required"].clear()
        self.assertEqual(self.invoke(ToolCall(self.capability, {}), tool_catalog=snapshot).status.value, "error")
        alien = IntegrationRegistry().capture_tool_catalog()
        self.assertEqual(self.invoke(tool_catalog=alien).status.value, "denied")
        _, denied = consume_generator(ToolExecutor(self.registry).execute(self.call, "s", "", tool_catalog=alien))
        self.assertEqual(denied.status.value, "denied")
        with self.assertRaises(ValueError):
            self.registry.replace_tools(MCPIntegration(self.runtime), [])
        tools = self.adapter.registered_tools()
        with self.assertRaises(ValueError):
            self.registry.replace_tools(self.adapter, [*tools, tools[0]])
        self.assertEqual(self.invoke(tool_catalog=snapshot).status.value, "success")

    def test_concurrent_catalog_publication_and_dispatch_keep_consistent_snapshots(self):
        old = self.registry.capture_tool_catalog()
        def publish():
            for index in range(80):
                self.adapter.configure_tools("demo", ["echo"] if index % 2 else [], self.registry)
        def dispatch():
            for _ in range(80):
                self.assertEqual(self.invoke(tool_catalog=old).status.value, "success")
                snapshot = self.registry.capture_tool_catalog()
                exposed = self.registry.get_native_tools(invocation_context=InvocationContext("s", "", tool_catalog=snapshot))
                result = self.invoke(tool_catalog=snapshot)
                self.assertEqual(result.status.value, "success" if exposed else "denied")
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(publish), pool.submit(dispatch), pool.submit(dispatch)]
            for future in futures:
                future.result(timeout=10)

    def test_concurrent_manual_reviews_do_not_overwrite_a_new_approval_policy(self):
        entered, release, changed = threading.Event(), threading.Event(), threading.Event()
        from app.config import MCPServerConfig
        validate = MCPServerConfig.model_validate

        def held_validation(settings):
            if not settings["approval_required"]:
                entered.set()
                self.assertTrue(release.wait(3))
            return validate(settings)

        def require_approval():
            self.adapter.configure_tools("demo", ["echo"], self.registry, approval_required=True)
            changed.set()

        with patch.object(MCPServerConfig, "model_validate", side_effect=held_validation), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.adapter.configure_tools, "demo", ["echo"], self.registry)
            self.assertTrue(entered.wait(2))
            second = pool.submit(require_approval)
            try:
                self.assertFalse(changed.wait(0.05))
            finally:
                release.set()
            first.result(timeout=3)
            second.result(timeout=3)
        self.assertEqual(self.invoke().status.value, "denied")
        self.assertEqual(self.invoke(approval_callback=lambda _: True).status.value, "success")

    def test_logs_redact_arguments_results_and_credential_values(self):
        self.runtime._secret_values.add("configured-private-token")
        self.runtime.invoke.return_value = {"content": [{"type": "text", "text": "configured-private-token"}]}
        executor = ToolExecutor(self.registry)
        with patch("app.core.tool_executor.trace_event") as trace:
            _, result = consume_generator(executor.execute(ToolCall(self.capability, {"text": "private-argument"}), "s", ""))
        self.assertNotIn("configured-private-token", result.content)
        self.assertNotIn("private-argument", str(trace.call_args_list))
        self.assertNotIn("configured-private-token", str(trace.call_args_list))
        self.assertIn("schema_fingerprint", result.diagnostics)

    def test_runner_uses_same_snapshot_across_native_and_mcp_dispatches(self):
        native_calls = []
        native = SimpleNamespace(name="demo", registered_tools=lambda: [RegisteredTool(
            ToolSpec(CapabilityId("demo", "run"), "Native fixture", {"type": "object"}),
            lambda arguments, context: native_calls.append(context.tool_catalog) or ToolResult.success("native"),
        )])
        self.registry.register(native)
        executor = ToolExecutor(self.registry)
        llm = Mock()
        llm.resolve_think_value.return_value = True
        llm.generation_options.return_value = {"num_ctx": 32768, "num_predict": 256}
        requests = []
        def infer(**kwargs):
            requests.append(deepcopy(kwargs))
            if len(requests) == 1:
                self.adapter.configure_tools("demo", [], self.registry)
                return {"tool_calls": [{"function": {"name": str(self.capability), "arguments": {"text": "hello"}}}], "thinking": "retained thought"}
            if len(requests) == 2:
                return {"tool_calls": [{"function": {"name": "demo__run", "arguments": {}}}]}
            return {"content": "Finished"}
        llm.chat_buffered_result = BufferedResultMock(side_effect=infer)
        _, response = consume_generator(ResponseGenerator(llm, executor, Mock()).stream_late_routed_response(
            "s", [{"role": "user", "content": "Use the tools"}], "Use the tools"))
        self.assertEqual(response, "Finished")
        self.assertEqual(self.runtime.invoke.call_count, 1)
        self.assertEqual(len(native_calls), 1)
        self.assertTrue(all(str(self.capability) in [tool["function"]["name"] for tool in request["tools"]] for request in requests))
        self.assertEqual(requests[1]["messages"][-2]["thinking"], "retained thought")
        self.assertNotIn(str(self.capability), [tool["function"]["name"] for tool in executor.get_native_tools()])

    def test_dispatch_cap_and_empty_authority_cover_mcp_without_special_loops(self):
        executor = ToolExecutor(self.registry)
        llm = Mock()
        llm.resolve_think_value.return_value = True
        llm.generation_options.return_value = {"num_ctx": 32768, "num_predict": 256}
        call = {"tool_calls": [{"function": {"name": str(self.capability), "arguments": {"text": "hello"}}}]}
        llm.chat_buffered_result = BufferedResultMock(side_effect=[call, call, {"content": "Recovered"}])
        settings = ExecutionSettings(limits=ExecutionLimits(max_tool_dispatches=1))
        consume_generator(ResponseGenerator(llm, executor, Mock(), execution_settings=settings).stream_late_routed_response(
            "s", [{"role": "user", "content": "Use the tools"}], "Use the tools"))
        self.assertEqual(self.runtime.invoke.call_count, 1)


class MCPWireInvocationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_path = Path(directory.name) / "state.json"
        self.state_path.write_text("{}")
        environment = patch.dict(os.environ, {"ASTRA_MCP_FIXTURE_STATE": str(self.state_path), "ASTRA_MCP_TEST_CREDENTIAL": "fixture-private-token"})
        environment.start()
        self.addCleanup(environment.stop)

    def create_runtime(self, **options):
        settings = {"enabled": True, "transport": "stdio", "command": sys.executable, "args": [str(FIXTURE)],
                    "env": {"ASTRA_MCP_STATE_FILE": "ASTRA_MCP_FIXTURE_STATE", "ASTRA_MCP_CREDENTIAL": "ASTRA_MCP_TEST_CREDENTIAL"},
                    "enabled_tools": ["echo", "error", "image", "structured", "input", "slow", "disconnect", "large", "raw"],
                    "approval_required": False, **options}
        self.runtime = MCPRuntime({"enabled": True, "servers": {"fixture": settings}})
        self.addCleanup(self.runtime.close)
        self.adapter = MCPIntegration(self.runtime)
        self.registry = IntegrationRegistry([self.adapter])
        self.adapter.refresh_catalog("fixture", self.registry)
        return ToolExecutor(self.registry)

    def call(self, name, executor=None, **kwargs):
        executor = executor or ToolExecutor(self.registry)
        _, result = consume_generator(executor.execute(ToolCall(capability_name("fixture", name), {"text": "Hello"}), "s", "", **kwargs))
        return result

    def calls(self):
        path = self.state_path.with_suffix(".calls")
        return json.loads(path.read_text()) if path.exists() else []

    def test_real_sdk_success_error_structured_and_unsupported_content(self):
        self.create_runtime()
        success = self.call("echo")
        self.assertEqual(success.status.value, "success")
        self.assertEqual(json.loads(success.content)["content"][0]["text"], "Hello")
        failure = self.call("error")
        self.assertEqual(failure.status.value, "error")
        self.assertNotIn("fixture-private-token", failure.content)
        image = self.call("image")
        self.assertEqual(image.diagnostics["category"], "unsupported_content")
        self.assertNotIn("aGVsbG8=", image.content)
        structured = self.call("structured")
        self.assertEqual(json.loads(structured.content)["structured_content"], {"length": 5})
        required = self.call("input")
        self.assertEqual(required.status.value, "error")
        self.assertEqual(required.diagnostics["category"], "unsupported_input")
        self.assertEqual(self.calls().count("input"), 1)

    def test_drift_is_rejected_before_effect_and_requires_manual_review(self):
        self.create_runtime()
        snapshot = self.registry.capture_tool_catalog()
        self.state_path.write_text(json.dumps({"revision": 2}))
        result = self.call("echo", tool_catalog=snapshot)
        self.assertEqual(result.status.value, "unavailable")
        self.assertEqual(result.diagnostics["completion"], "not_started")
        self.assertEqual(self.calls(), [])
        self.adapter.refresh_catalog("fixture", self.registry)
        self.assertEqual(self.call("echo").status.value, "denied")
        self.assertIn("schema_changed", self.adapter.catalog_issues()[("fixture", "echo")])

    def test_timeout_disconnect_and_invalid_output_report_uncertainty_without_replay(self):
        self.create_runtime(call_timeout_s=0.15)
        timeout = self.call("slow")
        self.assertEqual(timeout.status.value, "error")
        self.assertEqual(timeout.diagnostics["completion"], "uncertain")
        self.assertEqual(self.calls(), ["slow"])
        self.adapter.refresh_catalog("fixture", self.registry)
        disconnected = self.call("disconnect")
        self.assertEqual(disconnected.diagnostics["completion"], "uncertain")
        self.assertEqual(self.calls(), ["slow", "disconnect"])
        self.state_path.write_text(json.dumps({"bad_output": True}))
        self.adapter.refresh_catalog("fixture", self.registry)
        invalid = self.call("structured")
        self.assertEqual(invalid.status.value, "error")
        self.assertEqual(invalid.diagnostics["completion"], "uncertain")
        self.assertEqual(invalid.diagnostics["category"], "invalid_output")

    def test_result_truncation_and_raw_receive_limits(self):
        self.create_runtime(max_result_chars=2048)
        truncated = self.call("large")
        self.assertEqual(truncated.status.value, "success")
        self.assertLessEqual(len(truncated.content), 2048)
        self.assertTrue(json.loads(truncated.content)["truncated"])
        self.runtime.close()
        self.create_runtime(max_message_bytes=4096)
        oversized = self.call("raw")
        self.assertEqual(oversized.status.value, "error")
        self.assertEqual(oversized.diagnostics["category"], "transport_limit")
        self.assertEqual(oversized.diagnostics["completion"], "uncertain")
        self.assertFalse(process_exists(int(self.state_path.with_suffix(".pid").read_text())))

    def test_large_sdk_result_fits_followup_http_payload_without_reexecution_or_approval_bypass(self):
        executor = self.create_runtime(approval_required=True, max_result_chars=16384)
        capability = capability_name("fixture", "large")
        selected = {"function": {"name": str(capability), "arguments": {"text": "Hello"}}}
        budget = PromptBudget(context_window_tokens=12000, completion_reserve_tokens=512,
                              safety_margin_tokens=128)
        for approved in (True, False):
            with self.subTest(approved=approved):
                client = OllamaClient("fixture", "http://localhost:11434", thinking_enabled=True,
                                      options={"num_ctx": 12000, "num_predict": 512})
                self.addCleanup(client.close)
                responses = iter([
                    make_response(lines=[json.dumps({"message": {
                        "thinking": "Keep this exact thought", "content": "Selected preamble", "tool_calls": [selected],
                    }, "done": True}).encode()]),
                    make_response(lines=[b'{"message":{"content":"Finished with the available evidence."},"done":true}']),
                ])
                requests = []

                def post(*_args, **kwargs):
                    requests.append(deepcopy(kwargs["json"]))
                    return next(responses)

                client.session.post = Mock(side_effect=post)
                approval = Mock(return_value=approved)
                generator = ResponseGenerator(client, executor, Mock(), prompt_budget=budget,
                                              execution_settings=ExecutionSettings(reasoning_enabled=True, limits=ExecutionLimits(
                                                  context_window_tokens=12000, num_predict=512)))
                before = len(self.calls())
                with patch("app.core.response_generator.trace_event") as trace:
                    _, answer = consume_generator(generator.stream_late_routed_response(
                        "fixture", [{"role": "user", "content": "Use the large fixture tool once"}],
                        "Use the large fixture tool once", allowed_capabilities={capability},
                        tool_approval_callback=approval))
                self.assertEqual(answer, "Finished with the available evidence.")
                self.assertEqual(len(requests), 2)
                approval.assert_called_once()
                self.assertEqual(len(self.calls()) - before, int(approved))
                final = requests[-1]
                self.assertEqual(len(tool_groups(final["messages"])), 1)
                call = next(message for message in final["messages"] if message.get("tool_calls"))
                self.assertEqual(call, {"role": "assistant", "content": "Selected preamble",
                                        "thinking": "Keep this exact thought", "tool_calls": [selected]})
                self.assertEqual(final["tools"], requests[0]["tools"])
                self.assertEqual(final["options"]["num_predict"], 512)
                self.assertEqual(final["options"]["num_ctx"], 12000)
                self.assertLessEqual(budget.estimate(final["messages"], final["tools"]), 11360)
                result = next(message["content"] for message in final["messages"] if message["role"] == "tool")
                if approved:
                    payload = json.loads(result[len("[success] "):])
                    self.assertTrue(payload["context_budget_truncated"])
                    self.assertEqual(payload["capability"], str(capability))
                    self.assertEqual(payload["server_id"], "fixture")
                    self.assertTrue(payload["schema_fingerprint"])
                    self.assertTrue(payload["content"][0]["text"])
                    self.assertTrue(any(event.kwargs["payload"].get("mcp_observations_reduced") == 1
                                        for event in trace.call_args_list))
                else:
                    self.assertTrue(result.startswith("[denied] "))
                    self.assertIn("not approved", result)

    def test_http_sdk_invocation_and_json_body_limits(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        process = subprocess.Popen([sys.executable, str(FIXTURE), str(port)], env={**os.environ,
            "ASTRA_MCP_STATE_FILE": str(self.state_path)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            self.create_runtime(transport="streamable_http", command=None, args=[], env={},
                                url=f"http://127.0.0.1:{port}/mcp", max_message_bytes=4096)
            self.assertEqual(self.call("echo").status.value, "success")
            oversized = self.call("large")
            self.assertEqual(oversized.status.value, "error")
            self.assertEqual(oversized.diagnostics["category"], "transport_limit")
            self.assertEqual(oversized.diagnostics["completion"], "uncertain")
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
