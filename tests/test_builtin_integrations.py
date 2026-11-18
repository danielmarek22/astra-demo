import json
from types import SimpleNamespace
import unittest

from app.integrations import (
    CapabilityId,
    IntegrationRegistry,
    InvocationContext,
    MemoryIntegration,
    ShellIntegration,
    ToolCall,
    WebIntegration,
)
from app.tools.bash_execution import BashExecutionTool
from app.tools.web_search import WebSearchTool
from app.tools.web.contracts import SearchResponse


class FakeWebTool:
    description = "Search the web."

    def __init__(self, available=True, result='{"results":[]}'):
        self.is_available = available
        self.client = SimpleNamespace(query_max_chars=512)
        self.result = result
        self.queries = []

    def run(self, query, time_range=None):
        self.queries.append(query)
        return self.result


class FakeMemoryHandler:
    def __init__(self, applied=True):
        self.applied = applied
        self.calls = []

    def handle_payload(self, session_id, payload):
        self.calls.append((session_id, payload))
        return self.applied


class BuiltinIntegrationTests(unittest.TestCase):
    def test_web_search_tool_uses_configured_result_limit(self):
        class FakeClient:
            is_available = True

            def __init__(self):
                self.calls = []

            def validate_query(self, query, time_range):
                return query

            def search(self, query, limit, time_range):
                self.calls.append((query, limit))
                return SearchResponse((), "2026-10-26T12:00:00+00:00")

        client = FakeClient()
        tool = WebSearchTool(client, max_results=7)

        self.assertEqual(json.loads(tool.run("current news"))["results"], [])
        self.assertEqual(client.calls, [("current news", 7)])

    def test_web_integration_remains_discoverable_during_outage(self):
        unavailable = IntegrationRegistry([WebIntegration(FakeWebTool(available=False))])
        self.assertEqual(len(unavailable.get_native_tools()), 1)

        tool = FakeWebTool()
        registry = IntegrationRegistry([WebIntegration(tool)])
        result = registry.invoke(
            ToolCall(CapabilityId("web", "search"), {"query": "current news"}),
            InvocationContext("session-1", "search"),
        )

        self.assertEqual(result.status.value, "success")
        self.assertEqual(tool.queries, ["current news"])

    def test_memory_integration_uses_real_session_and_structured_payload(self):
        handler = FakeMemoryHandler()
        registry = IntegrationRegistry([MemoryIntegration(handler)])
        payload = {"content": "remember this", "category": "preference", "importance": 3}

        result = registry.invoke(
            ToolCall(CapabilityId("memory", "write"), payload),
            InvocationContext("session-42", "remember this"),
        )

        self.assertEqual(result.status.value, "success")
        self.assertEqual(handler.calls, [("session-42", payload)])

    def test_shell_integration_returns_denied_status(self):
        registry = IntegrationRegistry([ShellIntegration(BashExecutionTool(timeout=2))])
        approvals = []

        result = registry.invoke(
            ToolCall(CapabilityId("shell", "execute"), {"command": "printf denied"}),
            InvocationContext(
                "session-1",
                "run it",
                approval_callback=lambda request: approvals.append(request) or False,
            ),
        )

        self.assertEqual(result.status.value, "denied")
        self.assertEqual(str(approvals[0].capability), "shell__execute")
        self.assertEqual(approvals[0].detail, "printf denied")


if __name__ == "__main__":
    unittest.main()
