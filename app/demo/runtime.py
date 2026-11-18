"""Single-process runtime generations and a shared gate for all readers/writers."""

import logging
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from app.config import Config
from app.core.orchestrator_factory import build_orchestrator
from app.demo.seeds import seed_examples
from app.integrations.mcp import MCPIntegration
from app.integrations.mcp_config_store import MCPConfigStore
from app.integrations.mcp_management import MCPManagement
from app.paths import STATE_ROOT, owned_state_path

logger = logging.getLogger(__name__)
_GENERATION_NAME = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class Generation:
    id: str
    directory: Path
    orchestrator: object
    manifest: dict
    mcp_management: MCPManagement | None


def _prepare_mcp_management(orchestrator, directory: Path) -> MCPManagement | None:
    registry = getattr(getattr(orchestrator, "tool_executor", None), "registry", None)
    integration = registry.get_integration("mcp") if registry is not None else None
    if not isinstance(integration, MCPIntegration):
        return None
    # The upstream management API edits only the workspace-local copy. Reset uses
    # the launch configuration again, without reconnecting to any servers.
    config_path = directory / "mcp.yaml"
    mcp_config = integration.runtime.configuration()
    with config_path.open("x", encoding="utf-8") as handle:
        config_path.chmod(0o600)
        yaml.safe_dump({"integrations": {"mcp": mcp_config}}, handle)
    return MCPManagement(integration, registry, MCPConfigStore(config_path))


class DemoRuntime:
    def __init__(self, settings: Config, *, builder: Callable = build_orchestrator):
        self.settings = settings
        self.builder = builder
        self.gate = threading.Lock()
        self.current: Generation | None = None

    def _remove(self, directory: Path) -> None:
        directory = owned_state_path(directory)
        if directory.parent != STATE_ROOT or not _GENERATION_NAME.fullmatch(
            directory.name
        ):
            raise ValueError("Refusing to remove an unowned generation")
        marker = owned_state_path(directory / ".demo-generation")
        if marker.read_text() != directory.name:
            raise ValueError("Missing generation ownership marker")
        for child in directory.rglob("*"):
            if child.is_symlink():
                raise ValueError("Refusing cleanup of a generation containing symlinks")
        shutil.rmtree(directory)

    def replace(self) -> Generation:
        """Caller holds gate. Preparation errors leave the old generation usable."""
        generation_id = uuid.uuid4().hex
        directory = owned_state_path(STATE_ROOT / generation_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / ".demo-generation").write_text(generation_id)
        orchestrator = None
        try:
            orchestrator = self.builder(self.settings, state_dir=directory)
            manifest = seed_examples(orchestrator, directory)
            mcp_management = _prepare_mcp_management(orchestrator, directory)
        except BaseException:
            if orchestrator is not None:
                orchestrator.close()
            self._remove(directory)
            raise
        replacement = Generation(generation_id, directory, orchestrator, manifest, mcp_management)
        old = self.current
        try:
            if old:
                old.orchestrator.close()
        except BaseException:
            self.current = None
            orchestrator.close()
            self._remove(directory)
            raise
        self.current = replacement
        if old:
            try:
                self._remove(old.directory)
            except Exception:
                logger.exception("Closed old demo generation could not be removed")
        return replacement

    def close(self) -> None:
        if self.current:
            old, self.current = self.current, None
            old.orchestrator.close()
            self._remove(old.directory)
