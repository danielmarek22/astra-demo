"""Narrow, revision-checked updates of integrations.mcp; no general config writer."""

import hashlib
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import yaml
from yaml.nodes import MappingNode

from app.config import MCPConfig


class MCPManagementError(Exception):
    def __init__(self, category, message, status=400):
        super().__init__(message)
        self.category, self.status = category, status


class MCPConfigStore:
    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
                raise ValueError
            data = self.path.read_bytes()
            # Refuse ambiguous layouts rather than rewriting unrelated YAML.
            tokens = yaml.scan(data)
            if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)) for token in tokens):
                raise ValueError
            root = yaml.safe_load(data)
            if not isinstance(root, dict) or not isinstance(root.get("integrations", {}), dict):
                raise TypeError
            node = yaml.compose(data)
            self._unique_keys(node)
            config = MCPConfig.model_validate(root.get("integrations", {}).get("mcp", {})).model_dump(exclude_none=True)
            return data, root, node, config, hashlib.sha256(data).hexdigest()
        except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError):
            raise MCPManagementError("configuration_unavailable", "MCP configuration cannot be safely read. Check the local YAML file.", 503) from None

    @classmethod
    def _unique_keys(cls, node):
        if isinstance(node, MappingNode):
            keys = [key.value for key, _ in node.value]
            if len(set(keys)) != len(keys):
                raise ValueError
            for _, child in node.value:
                cls._unique_keys(child)
        elif isinstance(node, yaml.nodes.SequenceNode):
            for child in node.value:
                cls._unique_keys(child)

    def read(self):
        _, _, _, config, revision = self._read()
        return config, revision

    @contextmanager
    def _write_lock(self):
        try:
            import fcntl
        except ImportError:
            raise MCPManagementError("persistence_unavailable", "MCP configuration writes require local file-lock support on this platform.", 503) from None
        try:
            with self.path.with_name(self.path.name + ".mcp.lock").open("a") as lock:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise MCPManagementError("busy", "Another MCP configuration write is still running.", 409) from None
                        time.sleep(0.01)
                try:
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            raise MCPManagementError("persistence_failed", "MCP configuration could not be saved.", 503) from None

    @staticmethod
    def _entry(node, key):
        if not isinstance(node, MappingNode) or node.flow_style:
            raise ValueError("Block mappings are required")
        return next(((name, value) for name, value in node.value if name.value == key), None)

    @staticmethod
    def _last_leaf(node):
        if isinstance(node, MappingNode) and node.value:
            return max(MCPConfigStore._last_leaf(child) for pair in node.value for child in pair)
        if isinstance(node, yaml.nodes.SequenceNode) and node.value:
            return max(MCPConfigStore._last_leaf(child) for child in node.value)
        return node.end_mark.index

    def _render(self, data, root_node, config):
        text = data.decode("utf-8")
        def line_start(index):
            return text.rfind("\n", 0, index) + 1
        def line_end(index):
            if index and text[index - 1] == "\n":
                return index
            end = text.find("\n", index)
            return end + 1 if end >= 0 else len(text)
        def block(indent):
            rendered = yaml.safe_dump({"mcp": config}, sort_keys=False, allow_unicode=True)
            return "".join(" " * indent + line for line in rendered.splitlines(keepends=True))
        integrations = self._entry(root_node, "integrations")
        if integrations is None:
            return text.rstrip("\n") + "\n\nintegrations:\n" + block(2)
        key, parent = integrations
        if not isinstance(parent, MappingNode):
            raise TypeError
        if not parent.value:
            start, end = line_start(key.start_mark.index), line_end(parent.end_mark.index)
            return text[:start] + "integrations:\n" + block(2) + text[end:]
        entry = self._entry(parent, "mcp")
        if entry is None:
            end = line_end(self._last_leaf(parent))
            return text[:end] + ("\n" if end and text[end - 1] != "\n" else "") + block(parent.value[0][0].start_mark.column) + text[end:]
        key, value = entry
        start, end = line_start(key.start_mark.index), line_end(self._last_leaf(value))
        # Do not splice a key sharing a line with other keys.
        if text[start:key.start_mark.index].strip():
            raise ValueError
        return text[:start] + block(key.start_mark.column) + text[end:]

    def write(self, config, expected_revision):
        config = MCPConfig.model_validate(config).model_dump(exclude_none=True)
        with self._write_lock():
            data, root, node, _, revision = self._read()
            if expected_revision != revision:
                raise MCPManagementError("stale_revision", "Configuration changed. Reload MCP before saving again.", 409)
            try:
                rendered = self._render(data, node, config).encode("utf-8")
                updated = yaml.safe_load(rendered)
                original = {**root, "integrations": {**root.get("integrations", {}), "mcp": config}}
                if updated != original:
                    raise ValueError
                mode = stat.S_IMODE(self.path.stat().st_mode)
                temporary = None
                try:
                    fd, temporary = tempfile.mkstemp(prefix=".mcp-", suffix=".yaml", dir=self.path.parent)
                    with os.fdopen(fd, "wb") as stream:
                        os.fchmod(stream.fileno(), mode)
                        stream.write(rendered)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if self.path.read_bytes() != data:
                        raise MCPManagementError("stale_revision", "Configuration changed. Reload MCP before saving again.", 409)
                    os.replace(temporary, self.path)
                    temporary = None
                finally:
                    if temporary is not None:
                        Path(temporary).unlink(missing_ok=True)
            except MCPManagementError:
                raise
            except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError):
                raise MCPManagementError("persistence_failed", "MCP configuration could not be safely saved. The previous configuration was retained.", 503) from None
            return hashlib.sha256(rendered).hexdigest()
