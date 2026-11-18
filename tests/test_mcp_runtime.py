import io
import logging
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.integrations import IntegrationRegistry
from app.integrations.mcp import MCPIntegration
from app.integrations.mcp_runtime import MCPRuntime, MCPRuntimeError

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_lifecycle_server.py"


def stdio_settings(**overrides):
    return {"enabled": True, "transport": "stdio", "command": sys.executable,
            "args": [str(FIXTURE)], "connect_timeout_s": 5.0,
            "env": {"ASTRA_TEST_MODE": "ASTRA_MCP_FIXTURE_MODE"}, **overrides}


def process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class MCPRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"ASTRA_MCP_FIXTURE_MODE": "pages"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def runtime(self, spec=None, **overrides):
        runtime = MCPRuntime({"enabled": True, "servers": {"demo": spec or stdio_settings()}, **overrides})
        self.addCleanup(runtime.close)
        return runtime

    def test_disabled_and_lifecycle_only_integration_are_inert(self):
        runtime = self.runtime(enabled=False)
        registry = IntegrationRegistry([MCPIntegration(runtime)])
        self.assertEqual(registry.get_native_tools(), [])
        with self.assertRaises(MCPRuntimeError) as error:
            runtime.refresh_catalog("demo")
        self.assertEqual(error.exception.category, "disabled")
        self.assertIsNone(runtime._thread)
        registry.close()
        runtime.close()
        self.assertEqual(runtime.status("demo").state, "closed")
        disabled_server = self.runtime(stdio_settings(enabled=False))
        with self.assertRaises(MCPRuntimeError) as error:
            disabled_server.connect("demo")
        self.assertEqual(error.exception.category, "disabled")
        self.assertIsNone(disabled_server._thread)

    def test_servers_have_independent_connections_and_errors(self):
        runtime = MCPRuntime({"enabled": True, "servers": {
            "good": stdio_settings(), "bad": stdio_settings(command="/nonexistent/astra-mcp-test-command"),
        }})
        self.addCleanup(runtime.close)
        runtime.refresh_catalog("good")
        with self.assertRaises(MCPRuntimeError):
            runtime.connect("bad")
        self.assertEqual(runtime.status("good").state, "connected")
        self.assertEqual(len(runtime.refresh_catalog("good")), 2)

    def test_unconfirmed_cleanup_is_visible_and_blocks_reconnect(self):
        runtime = self.runtime()

        async def fake_open(stack, _spec, _auth_failed):
            def broken_cleanup():
                raise RuntimeError("private-cleanup-detail")
            stack.callback(broken_cleanup)
            return SimpleNamespace(protocol_version="2026-07-28")

        try:
            with patch.object(runtime, "_open_client", fake_open):
                runtime.connect("demo")
                with self.assertRaises(MCPRuntimeError) as error:
                    runtime.disconnect("demo")
                self.assertEqual(error.exception.category, "shutdown_failed")
                self.assertNotIn("private-cleanup-detail", str(error.exception))
                with self.assertRaises(MCPRuntimeError) as error:
                    runtime.connect("demo")
                self.assertEqual(error.exception.category, "shutdown_failed")
                with self.assertRaises(MCPRuntimeError):
                    runtime.close()
                self.assertEqual(runtime.status("demo").state, "error")
                self.assertEqual(runtime.status("demo").error_category, "shutdown_failed")
        finally:
            # The deliberately broken fake owns no actual transport. Production
            # leaves unconfirmed cleanup quarantined instead of hiding failure.
            if runtime._loop is not None and not runtime._loop.is_closed():
                runtime._loop.call_soon_threadsafe(runtime._loop.stop)
                runtime._thread.join(timeout=2)

    def test_stdio_negotiates_paginates_reuses_and_shuts_down(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "pid"
            with patch.dict(os.environ, {"ASTRA_MCP_PID": str(pid_path)}):
                runtime = self.runtime(stdio_settings(env={
                    "ASTRA_TEST_PID_FILE": "ASTRA_MCP_PID", "ASTRA_TEST_MODE": "ASTRA_MCP_FIXTURE_MODE",
                }))
                self.assertIsNone(runtime._thread)
                status = runtime.connect("demo")
                self.assertEqual(status.state, "connected")
                self.assertIn(status.protocol_version, {"2026-07-28", "2025-11-25"})
                pid = int(pid_path.read_text())
                tools = runtime.refresh_catalog("demo")
                self.assertEqual([tool["name"] for tool in tools], ["echo", "add"])
                self.assertEqual(runtime.status("demo").tool_count, 2)
                tools[0]["name"] = "changed"
                self.assertEqual(runtime.catalog("demo")[0]["name"], "echo")
                runtime.refresh_catalog("demo")
                self.assertEqual(int(pid_path.read_text()), pid)
                runtime.close()
                self.assertFalse(process_exists(pid))
                self.assertFalse(runtime._thread.is_alive())

    def test_disconnect_allows_a_fresh_connection(self):
        runtime = self.runtime()
        runtime.disconnect("demo")
        self.assertIsNone(runtime._thread)
        runtime.refresh_catalog("demo")
        runtime.disconnect("demo")
        self.assertEqual(runtime.status("demo").state, "disconnected")
        self.assertEqual(runtime.catalog("demo"), [])
        self.assertEqual(len(runtime.refresh_catalog("demo")), 2)

    def test_failed_launch_can_be_retried_after_executable_appears(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "python"
            runtime = self.runtime(stdio_settings(command=str(executable)))
            with self.assertRaises(MCPRuntimeError) as error:
                runtime.connect("demo")
            self.assertEqual(error.exception.category, "launch_failed")
            self.assertEqual(runtime.status("demo").state, "error")
            executable.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
            executable.chmod(0o700)
            self.assertEqual(len(runtime.refresh_catalog("demo")), 2)

    def test_catalog_limits_errors_and_missing_secrets_fail_safely(self):
        for mode, options, category in (
            ("repeat", {}, "invalid_catalog"), ("duplicate", {}, "invalid_catalog"),
            ("pages", {"max_catalog_pages": 1}, "catalog_limit"),
            ("pages", {"max_catalog_tools": 1}, "catalog_limit"),
            ("large", {"max_catalog_bytes": 1024}, "catalog_limit"),
            ("fail", {}, "protocol_error"),
            ("slow", {"catalog_timeout_s": 0.15}, "catalog_timeout"),
        ):
            with self.subTest(mode=mode, options=options), patch.dict(os.environ, {"ASTRA_MCP_FIXTURE_MODE": mode}):
                runtime = self.runtime(stdio_settings(**options))
                with self.assertRaises(MCPRuntimeError) as error:
                    runtime.refresh_catalog("demo")
                self.assertEqual(error.exception.category, category)
                self.assertEqual(runtime.catalog("demo"), [])
                self.assertEqual(runtime.status("demo").state, "error")
                runtime.close()
        runtime = self.runtime(stdio_settings(env={"TOKEN": "ASTRA_MCP_MISSING_SECRET"}))
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(MCPRuntimeError) as error:
            runtime.connect("demo")
        self.assertEqual(error.exception.category, "credentials_missing")

    def test_deadline_kills_a_non_protocol_child(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "pid"
            script = "import os,time,pathlib; pathlib.Path(os.environ['PID_FILE']).write_text(str(os.getpid())); time.sleep(30)"
            with patch.dict(os.environ, {"ASTRA_MCP_PID": str(pid_path)}):
                runtime = self.runtime(stdio_settings(args=["-c", script], env={"PID_FILE": "ASTRA_MCP_PID"}, connect_timeout_s=0.3))
                with self.assertRaises(MCPRuntimeError) as error:
                    runtime.connect("demo")
                self.assertEqual(error.exception.category, "connection_timeout")
                self.assertFalse(process_exists(int(pid_path.read_text())))

    def test_shutdown_during_catalog_request_finishes_waiter_and_child(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "ASTRA_MCP_FIXTURE_MODE": "slow", "ASTRA_MCP_PID": str(Path(directory) / "pid"),
        }):
            runtime = self.runtime(stdio_settings(env={
                "ASTRA_TEST_MODE": "ASTRA_MCP_FIXTURE_MODE", "ASTRA_TEST_PID_FILE": "ASTRA_MCP_PID",
            }))
            runtime.connect("demo")
            pid = int((Path(directory) / "pid").read_text())
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(runtime.refresh_catalog, "demo")
                runtime.close()
                with self.assertRaises(MCPRuntimeError):
                    result.result(timeout=3)
            self.assertFalse(process_exists(pid))
            self.assertFalse(runtime._thread.is_alive())

    def test_concurrent_requests_are_serialized_and_queue_is_bounded(self):
        runtime = self.runtime(queue_timeout_s=0.05)
        started, release = threading.Event(), threading.Event()
        original = runtime._list_catalog

        async def blocked(client, spec):
            import asyncio
            started.set()
            await asyncio.to_thread(release.wait, 3)
            return await original(client, spec)

        with patch.object(runtime, "_list_catalog", blocked), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(runtime.refresh_catalog, "demo")
            try:
                self.assertTrue(started.wait(5))
                with self.assertRaises(MCPRuntimeError) as error:
                    runtime.connect("demo")
                self.assertEqual(error.exception.category, "busy")
            finally:
                release.set()
            self.assertEqual(len(result.result(timeout=5)), 2)


@contextmanager
def http_fixture(port):
    process = subprocess.Popen([sys.executable, str(FIXTURE), str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if process.poll() is not None:
                    raise AssertionError("HTTP fixture exited before startup")
                time.sleep(0.02)
        else:
            raise AssertionError("HTTP fixture startup timed out")
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


class MCPHTTPRuntimeTests(unittest.TestCase):
    def test_http_failure_reconnect_authentication_catalog_and_shutdown(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        settings = {"enabled": True, "servers": {"demo": {
            "enabled": True, "transport": "streamable_http", "url": f"http://127.0.0.1:{port}/mcp",
            "headers": {"Authorization": "ASTRA_MCP_AUTH"}, "connect_timeout_s": 2.0,
        }}}
        runtime = MCPRuntime(settings)
        self.addCleanup(runtime.close)
        with patch.dict(os.environ, {"ASTRA_MCP_AUTH": "Bearer fixture-token"}):
            with self.assertRaises(MCPRuntimeError):
                runtime.connect("demo")
            with http_fixture(port) as process:
                self.assertEqual(len(runtime.refresh_catalog("demo")), 2)
                self.assertEqual(runtime.status("demo").state, "connected")
                runtime.disconnect("demo")
                logs = io.StringIO()
                handler = logging.StreamHandler(logs)
                root = logging.getLogger()
                root.addHandler(handler)
                try:
                    with patch.dict(os.environ, {"ASTRA_MCP_AUTH": "bad-private-token"}), self.assertRaises(MCPRuntimeError) as error:
                        runtime.connect("demo")
                    self.assertEqual(error.exception.category, "authentication_failed")
                    self.assertNotIn("bad-private-token", str(error.exception) + logs.getvalue())
                    self.assertNotIn("fixture-secret", logs.getvalue())
                finally:
                    root.removeHandler(handler)
                self.assertEqual(len(runtime.refresh_catalog("demo")), 2)
                runtime.close()
                self.assertIsNone(process.poll())  # Externally owned HTTP service is retained.
                self.assertFalse(runtime._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
