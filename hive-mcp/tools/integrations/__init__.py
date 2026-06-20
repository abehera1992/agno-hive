"""External platform integrations — staging infrastructure.

Each platform module (notion.py, google.py, ...) lives in this directory.
Platform tools are imported and re-exported here; main.py registers them
conditionally based on which env vars are present.

Adding a new platform:
  1. Create tools/integrations/<platform>.py with tools + a _execute(tool, args) function
  2. Add its env var to config.py
  3. Import and re-export its public tools below
  4. Register them in main.py under an `if config.<PLATFORM>_KEY:` guard
  5. Add the env var to docker-compose.hive.yml and .env.example
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import PROJECT_ROOT, WRITE_REVIEW

# ── Pending action store ──────────────────────────────────────────────────────

_ACTION_DIR = PROJECT_ROOT / ".hive_pending_actions"

# Registry: platform name → executor function
# Each platform module registers itself on import via register_executor().
_EXECUTORS: dict = {}


def register_executor(platform: str, fn) -> None:
    """Called by each platform module to register its _execute(tool, args) function."""
    _EXECUTORS[platform] = fn


def action_dir() -> Path:
    _ACTION_DIR.mkdir(exist_ok=True)
    return _ACTION_DIR


def _stage_action(platform: str, tool: str, summary: str, args: dict) -> str:
    """Write a .hive_pending_actions/<id>.json and return an action_pending message."""
    action_id = uuid.uuid4().hex[:12]
    action = {
        "id": action_id,
        "platform": platform,
        "tool": tool,
        "summary": summary,
        "args": args,
    }
    (action_dir() / f"{action_id}.json").write_text(
        json.dumps(action, indent=2), encoding="utf-8"
    )
    return (
        f"action_pending: {platform}/{tool} — {summary}\n"
        f"action_id: {action_id}\n"
        f"The user will confirm or reject via the hive CLI. STOP — do not call any other tool."
    )


def confirm_action(action_id: str) -> str:
    """
    Execute a staged external platform action after human approval.
    Called by the hive CLI confirm flow — do NOT call this proactively.

    Args:
        action_id: 12-char hex ID returned in the action_pending message
    """
    action_file = action_dir() / f"{action_id}.json"
    if not action_file.exists():
        return f"action not found: {action_id}"
    try:
        action   = json.loads(action_file.read_text(encoding="utf-8"))
        platform = action["platform"]
        if platform not in _EXECUTORS:
            return f"no executor registered for platform: {platform}"
        result = _EXECUTORS[platform](action["tool"], action["args"])
        action_file.unlink(missing_ok=True)
        return result
    except Exception as e:
        return f"confirm_action failed: {e}"


def reject_action(action_id: str) -> str:
    """
    Discard a staged external platform action without executing it.
    Called by the hive CLI reject flow — do NOT call this proactively.

    Args:
        action_id: 12-char hex ID returned in the action_pending message
    """
    action_file = action_dir() / f"{action_id}.json"
    if not action_file.exists():
        return f"action not found: {action_id}"
    action_file.unlink(missing_ok=True)
    return f"action rejected and discarded: {action_id}"


# ── Platform tool re-exports (add each platform below) ───────────────────────
# Importing the platform module triggers register_executor() as a side effect.

try:
    from tools.integrations.notion import (  # noqa: F401
        notion_search,
        notion_get_page,
        notion_create_page,
        notion_update_page_props,
        notion_append_blocks,
        notion_append_markdown,
    )
except ImportError:
    pass  # notion module missing or notion-client not installed

try:
    from tools.integrations.migrations import run_migration  # noqa: F401  (registers "migration" executor)
except ImportError:
    pass
