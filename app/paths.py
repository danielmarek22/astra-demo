"""Project-relative paths; writable demo state never follows external links."""

from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = APP_ROOT / "static"
STATE_ROOT = APP_ROOT / ".demo-state"
DATA_DIR = STATE_ROOT / "manual"
DEFAULT_CONFIG_PATH = APP_ROOT / "app/config/assistant.yaml"
TEMPLATE_CONFIG_PATH = APP_ROOT / "app/config/assistant-template.yaml"


def resolve_app_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else APP_ROOT / candidate


def owned_state_path(path: str | Path) -> Path:
    candidate = resolve_app_path(path)
    try:
        relative = candidate.relative_to(STATE_ROOT)
    except ValueError as exc:
        raise ValueError(
            "State must be inside this demo's .demo-state directory"
        ) from exc
    if ".." in relative.parts:
        raise ValueError("State paths cannot contain parent traversal")
    current = STATE_ROOT
    for part in ("", *relative.parts):
        if part:
            current = current / part
        if current.is_symlink():
            raise ValueError("Demo state paths cannot contain symlinks")
    candidate.resolve().relative_to(STATE_ROOT.resolve())
    return candidate
