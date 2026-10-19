"""Single-process runtime generations and a shared gate for all readers/writers."""

import logging
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import Config
from app.core.orchestrator_factory import build_orchestrator
from app.demo.seeds import seed_examples
from app.paths import STATE_ROOT, owned_state_path

logger = logging.getLogger(__name__)
_GENERATION_NAME = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class Generation:
    id: str
    directory: Path
    orchestrator: object
    manifest: dict


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
        except BaseException:
            if orchestrator is not None:
                orchestrator.close()
            self._remove(directory)
            raise
        replacement = Generation(generation_id, directory, orchestrator, manifest)
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
