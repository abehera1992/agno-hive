"""Hash-based incremental index state — tracks which files have been indexed."""
import json
from pathlib import Path


def _state_path(project_id: str) -> Path:
    base = Path.home() / ".agno-hive" / "index-state"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{project_id}.json"


def load(project_id: str) -> dict[str, str]:
    p = _state_path(project_id)
    return json.loads(p.read_text()) if p.exists() else {}


def save(project_id: str, state: dict[str, str]) -> None:
    _state_path(project_id).write_text(json.dumps(state, indent=2))


def diff(old: dict[str, str], current: dict[str, str]) -> tuple[list[str], list[str]]:
    """Return (changed_or_new paths, deleted paths)."""
    changed = [k for k, v in current.items() if old.get(k) != v]
    deleted = [k for k in old if k not in current]
    return changed, deleted
