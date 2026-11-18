import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import yaml

from app.config import Config, MCPConfig


class MCPConfigTests(unittest.TestCase):
    def test_optional_reviewed_tool_fingerprints_are_strict(self):
        server = {"transport": "stdio", "command": "python", "enabled_tools": ["echo"]}
        configured = MCPConfig.model_validate({"servers": {"demo": {**server, "tool_fingerprints": {"echo": "a" * 64}}}})
        self.assertEqual(configured.servers["demo"].tool_fingerprints, {"echo": "a" * 64})
        for fingerprints in ({"echo": "bad"}, {"unknown": "a" * 64}, {"echo": "A" * 64}, {"echo": 1}):
            with self.subTest(fingerprints=fingerprints), self.assertRaises(ValueError):
                MCPConfig.model_validate({"servers": {"demo": {**server, "tool_fingerprints": fingerprints}}})

    def test_defaults_and_yaml_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump({"integrations": {"mcp": {"servers": {
                "demo": {"transport": "stdio", "command": "python", "enabled_tools": ["echo"]},
            }}}}))
            settings = Config(path).integrations["mcp"]
        self.assertFalse(settings["enabled"])
        self.assertFalse(settings["servers"]["demo"]["enabled"])
        self.assertTrue(settings["servers"]["demo"]["approval_required"])
        self.assertEqual(MCPConfig().servers, {})

    def test_strict_transport_and_policy_validation(self):
        stdio = {"transport": "stdio", "command": "python"}
        http = {"transport": "streamable_http", "url": "http://127.0.0.1:3001/mcp"}
        invalid = [
            {"enabled": "true"}, {"approval_required": 1}, {"transport": "sse"},
            {"command": ""}, {"command": " python"}, {"command": "python\n"},
            {"cwd": "relative"}, {"url": "https://example.com/mcp"},
            {"headers": {"Authorization": "TOKEN"}}, {"enabled_tools": ["*"]},
            {"enabled_tools": ["echo", "echo"]}, {"enabled_tools": [1]},
            {"env": {"TOKEN": "literal-secret-value"}}, {"env": {"BAD-NAME": "TOKEN"}},
            {"connect_timeout_s": 0}, {"connect_timeout_s": float("nan")},
            {"catalog_timeout_s": float("inf")}, {"max_catalog_pages": True},
            {"max_catalog_tools": 0}, {"max_catalog_bytes": 1}, {"unknown": True},
            {"args": ["bad\x00argument"]},
        ]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                MCPConfig.model_validate({"servers": {"demo": {**stdio, **change}}})
        for change in (
            {"url": "https://user:secret@example.com/mcp"}, {"url": "https://example.com/?key=secret"},
            {"url": "https://example.com/#fragment"}, {"url": "file:///tmp/server"},
            {"url": "http://localhost:0/mcp"}, {"url": "http://localhost:bad/mcp"},
            {"command": "python"}, {"args": ["server.py"]}, {"env": {"TOKEN": "TOKEN"}},
            {"headers": {"Authorization\n": "TOKEN"}}, {"headers": {"Host": "HOST"}},
            {"headers": {"Mcp-Protocol-Version": "VERSION"}},
            {"headers": {"Authorization": "TOKEN", "authorization": "TOKEN"}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                MCPConfig.model_validate({"servers": {"demo": {**http, **change}}})
        for settings in ({"enabled": 1}, {"queue_timeout_s": 0}, {"servers": {"Bad-ID": stdio}},
                         {"servers": {"x" * 49: stdio}}, {"servers": {"demo": {}}}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                MCPConfig.model_validate(settings)

    def test_configuration_is_copied_and_secrets_are_references(self):
        settings = {"servers": {"demo": {"transport": "stdio", "command": "python", "env": {"TOKEN": "ASTRA_TEST_TOKEN"}}}}
        before = deepcopy(settings)
        config = MCPConfig.model_validate(settings)
        config.servers["demo"].env["TOKEN"] = "ANOTHER_TOKEN"
        self.assertEqual(settings, before)


if __name__ == "__main__":
    unittest.main()
