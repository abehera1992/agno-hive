# Generic MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all eKam-specific coupling from agno-hive so it works generically with any project's MCP server by changing only `.env`.

**Architecture:** A one-time bootstrap phase opens a raw MCP session at startup, fetches project patterns via file tools, detects memory capability, and injects the context into the coordinator before the Team is constructed. Memory functions resolve to DB, no-op, or MCP-native tiers based on what's configured.

**Tech Stack:** Python 3.11+, agno[mcp,ollama], asyncpg, mcp (transitive via agno[mcp]), pytest, pytest-asyncio

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `swarm/bootstrap.py` | Create | One-time MCP startup: fetch patterns, detect memory capability |
| `swarm/memory.py` | Modify | Add `resolve_memory_fns()` and named no-op tier |
| `swarm/agents.py` | Modify | Accept `store_fn`/`search_fn` params; strip eKam `_BASE_PREAMBLE` entries |
| `swarm/team.py` | Modify | Call bootstrap, use dynamic instructions, remove eKam rules + `build_swarm` |
| `config/config.py` | Modify | Remove hardcoded IPs, make `db_url` optional, add `patterns_glob` |
| `main.py` | Modify | Remove local pattern loading; pass task directly to `run_task_async` |
| `patterns/README.md` | Create | Convention docs: patterns live in target project |
| `tests/conftest.py` | Create | Shared pytest fixtures |
| `tests/test_config.py` | Create | Config defaults and optional fields |
| `tests/test_memory.py` | Create | `resolve_memory_fns` tier selection |
| `tests/test_bootstrap.py` | Create | Pattern discovery, fallback, memory detection |

---

## Task 1: Test Infrastructure

**Files:**
- Modify: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1.1: Add test dependencies to requirements.txt**

Replace the existing `requirements.txt` with:

```
agno[mcp,ollama]>=1.4.0
openai>=1.0.0
asyncpg>=0.29.0
python-dotenv>=1.0.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

- [ ] **Step 1.2: Install dependencies**

```bash
pip install -r requirements.txt
```

Expected: installs pytest and pytest-asyncio without errors.

- [ ] **Step 1.3: Create tests package**

Create `tests/__init__.py` as an empty file.

Create `tests/conftest.py`:

```python
import pytest


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    """Remove real env vars so tests use explicit values only."""
    for var in ("OLLAMA_HOST", "MCP_URL", "DB_URL", "PATTERNS_GLOB",
                "LEADER_MODEL", "CODER_MODEL", "REVIEWER_MODEL",
                "MEMORY_NAMESPACE", "STREAM", "MAX_ITERATIONS"):
        monkeypatch.delenv(var, raising=False)
```

- [ ] **Step 1.4: Verify pytest discovers tests**

```bash
pytest tests/ --collect-only
```

Expected: `no tests ran` (no test files yet — confirms pytest works).

- [ ] **Step 1.5: Commit**

```bash
git add requirements.txt tests/__init__.py tests/conftest.py
git commit -m "test: add pytest infrastructure"
```

---

## Task 2: Config Cleanup

**Files:**
- Modify: `config/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 2.1: Write failing tests**

Create `tests/test_config.py`:

```python
import importlib
import os
import sys


def _reload_config():
    """Reload config module so env changes take effect."""
    if "config.config" in sys.modules:
        del sys.modules["config.config"]
    return importlib.import_module("config.config")


def test_defaults_have_no_hardcoded_ips():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.ollama_host == ""
    assert cfg.mcp_url == ""


def test_db_url_defaults_to_none():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.db_url is None


def test_patterns_glob_default():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.patterns_glob == "patterns/**/*.md"


def test_patterns_glob_from_env(monkeypatch):
    monkeypatch.setenv("PATTERNS_GLOB", "docs/patterns/**/*.md")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.patterns_glob == "docs/patterns/**/*.md"


def test_db_url_from_env(monkeypatch):
    monkeypatch.setenv("DB_URL", "postgresql://user:pass@host/db")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.db_url == "postgresql://user:pass@host/db"


def test_stream_defaults_false():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.stream is False
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
pytest tests/test_config.py -v
```

Expected: FAIL — `test_defaults_have_no_hardcoded_ips` fails because current defaults have Tailscale IPs.

- [ ] **Step 2.3: Implement config changes**

Replace `config/config.py` entirely:

```python
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Ollama inference server
    ollama_host: str = os.getenv("OLLAMA_HOST", "")

    # Models
    leader_model: str = os.getenv("LEADER_MODEL", "qwen3:30b-a3b")
    coder_model: str = os.getenv("CODER_MODEL", "mistral-small3.1:24b")
    reviewer_model: str = os.getenv("REVIEWER_MODEL", "gemma3:27b")

    # MCP context server — point at any project's MCP server
    mcp_url: str = os.getenv("MCP_URL", "")

    # Pattern discovery glob — relative to the connected project root
    patterns_glob: str = os.getenv("PATTERNS_GLOB", "patterns/**/*.md")

    # Optional: PostgreSQL for shared conscience (claude_flow schema)
    db_url: str | None = os.getenv("DB_URL", None)
    memory_namespace: str = os.getenv("MEMORY_NAMESPACE", "agno-hive")

    # Swarm behaviour
    stream: bool = os.getenv("STREAM", "false").lower() == "true"
    max_iterations: int = int(os.getenv("MAX_ITERATIONS", "5"))


config = Config()
```

- [ ] **Step 2.4: Run tests to verify they pass**

```bash
pytest tests/test_config.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 2.5: Commit**

```bash
git add config/config.py tests/test_config.py
git commit -m "feat: remove hardcoded config defaults, add patterns_glob, db_url optional"
```

---

## Task 3: Memory Tier Resolution

**Files:**
- Modify: `swarm/memory.py`
- Create: `tests/test_memory.py`

- [ ] **Step 3.1: Write failing tests**

Create `tests/test_memory.py`:

```python
import pytest


def test_resolve_returns_db_fns_when_db_url_set():
    from swarm.memory import resolve_memory_fns
    store, search = resolve_memory_fns(db_url="postgresql://user:pass@host/db", has_mcp_memory=False)
    assert store.__name__ == "memory_store"
    assert search.__name__ == "memory_search"


def test_resolve_returns_noops_when_no_db():
    from swarm.memory import resolve_memory_fns
    store, search = resolve_memory_fns(db_url=None, has_mcp_memory=False)
    result = store("key", "value")
    assert "not configured" in result
    assert store.__name__ == "memory_store"


def test_resolve_returns_noops_when_mcp_memory_but_no_db():
    from swarm.memory import resolve_memory_fns
    store, search = resolve_memory_fns(db_url=None, has_mcp_memory=True)
    result = search("query")
    assert "not configured" in result
    assert search.__name__ == "memory_search"


def test_noop_store_returns_string():
    from swarm.memory import resolve_memory_fns
    store, _ = resolve_memory_fns(db_url=None, has_mcp_memory=False)
    result = store("my-key", "my-value")
    assert isinstance(result, str)
    assert "my-key" in result


def test_noop_search_returns_string():
    from swarm.memory import resolve_memory_fns
    _, search = resolve_memory_fns(db_url=None, has_mcp_memory=False)
    result = search("anything")
    assert isinstance(result, str)
```

- [ ] **Step 3.2: Run tests to verify they fail**

```bash
pytest tests/test_memory.py -v
```

Expected: FAIL — `resolve_memory_fns` does not exist yet.

- [ ] **Step 3.3: Add `resolve_memory_fns` to `swarm/memory.py`**

Append to the bottom of the existing `swarm/memory.py` (after the existing `memory_search` function):

```python


def _make_noop_store():
    def memory_store(key: str, value: str) -> str:
        """Persist a finding to the shared conscience. key should be descriptive."""
        return f"memory not configured — skipped: {key}"
    return memory_store


def _make_noop_search():
    def memory_search(query: str) -> str:
        """Search the shared conscience for prior findings related to query."""
        return "memory not configured"
    return memory_search


def resolve_memory_fns(
    db_url: str | None,
    has_mcp_memory: bool,
) -> tuple:
    """Return (store_fn, search_fn) for the appropriate memory tier.

    Tier 1 — DB configured: direct asyncpg access (fast, persistent).
    Tier 2 — MCP has memory tools, no DB: no-op wrappers (agents use MCP tools directly).
    Tier 3 — neither: no-op wrappers (memory disabled).
    """
    if db_url:
        return memory_store, memory_search
    return _make_noop_store(), _make_noop_search()
```

- [ ] **Step 3.4: Run tests to verify they pass**

```bash
pytest tests/test_memory.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 3.5: Commit**

```bash
git add swarm/memory.py tests/test_memory.py
git commit -m "feat: add resolve_memory_fns with DB/noop tier selection"
```

---

## Task 4: Bootstrap Module

**Files:**
- Create: `swarm/bootstrap.py`
- Create: `tests/test_bootstrap.py`

- [ ] **Step 4.1: Write failing tests**

Create `tests/test_bootstrap.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_mock_session(tool_names: list[str], call_tool_side_effects: list):
    session = AsyncMock()
    session.list_tools.return_value = MagicMock(
        tools=[MagicMock(name=n) for n in tool_names]
    )
    session.call_tool.side_effect = call_tool_side_effects
    return session


def _text_result(*texts: str):
    return MagicMock(content=[MagicMock(text=t) for t in texts])


@pytest.mark.asyncio
async def test_load_from_session_discovers_patterns():
    from swarm.bootstrap import _load_from_session

    session = _make_mock_session(
        tool_names=["find_files", "get_file_content", "memory_search", "memory_store"],
        call_tool_side_effects=[
            _text_result("patterns/backend.md\npatterns/frontend.md"),  # find_files
            _text_result("# Backend Rules"),                             # get_file_content backend
            _text_result("# Frontend Rules"),                            # get_file_content frontend
        ],
    )

    context, has_memory = await _load_from_session(session, "patterns/**/*.md")

    assert "# Backend Rules" in context
    assert "# Frontend Rules" in context
    assert has_memory is True


@pytest.mark.asyncio
async def test_load_from_session_detects_no_memory():
    from swarm.bootstrap import _load_from_session

    session = _make_mock_session(
        tool_names=["find_files", "get_file_content"],
        call_tool_side_effects=[
            _text_result("patterns/rules.md"),
            _text_result("# Rules"),
        ],
    )

    _, has_memory = await _load_from_session(session, "patterns/**/*.md")
    assert has_memory is False


@pytest.mark.asyncio
async def test_load_from_session_falls_back_to_project_context():
    from swarm.bootstrap import _load_from_session

    session = _make_mock_session(
        tool_names=["get_project_context"],
        call_tool_side_effects=[
            _text_result(""),               # find_files returns empty
            _text_result("# DOCS content"), # get_project_context fallback
        ],
    )

    context, _ = await _load_from_session(session, "patterns/**/*.md")
    assert "# DOCS content" in context


@pytest.mark.asyncio
async def test_load_from_session_skips_failed_file_reads():
    from swarm.bootstrap import _load_from_session

    session = _make_mock_session(
        tool_names=["find_files", "get_file_content", "memory_search", "memory_store"],
        call_tool_side_effects=[
            _text_result("patterns/good.md\npatterns/bad.md"),
            _text_result("# Good Content"),  # good.md succeeds
            Exception("read error"),          # bad.md fails — should be skipped
        ],
    )

    context, _ = await _load_from_session(session, "patterns/**/*.md")
    assert "# Good Content" in context


@pytest.mark.asyncio
async def test_load_from_session_empty_when_no_tools():
    from swarm.bootstrap import _load_from_session

    session = _make_mock_session(
        tool_names=[],
        call_tool_side_effects=[
            _text_result(""),  # find_files empty
            _text_result(""),  # get_project_context empty
        ],
    )

    context, has_memory = await _load_from_session(session, "patterns/**/*.md")
    assert context == ""
    assert has_memory is False
```

- [ ] **Step 4.2: Run tests to verify they fail**

```bash
pytest tests/test_bootstrap.py -v
```

Expected: FAIL — `swarm.bootstrap` does not exist yet.

- [ ] **Step 4.3: Implement `swarm/bootstrap.py`**

Create `swarm/bootstrap.py`:

```python
"""Bootstrap phase — runs once at startup before Team construction.

Opens a raw MCP client session, fetches project patterns via file tools,
detects whether the MCP server exposes memory tools, and returns both.
"""
from mcp import ClientSession
from mcp.client.sse import sse_client


async def bootstrap(
    mcp_url: str,
    timeout: int,
    patterns_glob: str = "patterns/**/*.md",
) -> tuple[str, bool]:
    """Return (project_context, has_mcp_memory).

    Falls back to ("", False) if the MCP server is unreachable.
    """
    try:
        async with sse_client(mcp_url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await _load_from_session(session, patterns_glob)
    except Exception as exc:
        print(f"[agno-hive] bootstrap warning: {exc}")
        return "", False


async def _load_from_session(
    session: ClientSession,
    patterns_glob: str,
) -> tuple[str, bool]:
    tools_result = await session.list_tools()
    tool_names = {t.name for t in tools_result.tools}
    has_mcp_memory = "memory_search" in tool_names and "memory_store" in tool_names

    context = await _fetch_patterns(session, patterns_glob)
    return context, has_mcp_memory


async def _fetch_patterns(session: ClientSession, patterns_glob: str) -> str:
    # Primary: discover and read pattern files
    try:
        find_result = await session.call_tool("find_files", {"pattern": patterns_glob})
        paths_text = _extract_text(find_result)
        paths = [p.strip() for p in paths_text.splitlines() if p.strip()]
        if paths:
            parts = []
            for path in paths:
                try:
                    content_result = await session.call_tool("get_file_content", {"path": path})
                    content = _extract_text(content_result)
                    if content:
                        parts.append(content)
                except Exception:
                    pass
            if parts:
                return "\n\n---\n\n".join(parts)
    except Exception:
        pass

    # Fallback: full project context
    try:
        ctx_result = await session.call_tool("get_project_context", {})
        return _extract_text(ctx_result)
    except Exception:
        return ""


def _extract_text(result) -> str:
    if not result or not result.content:
        return ""
    return "\n".join(
        item.text
        for item in result.content
        if hasattr(item, "text") and item.text
    )
```

- [ ] **Step 4.4: Run tests to verify they pass**

```bash
pytest tests/test_bootstrap.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 4.5: Commit**

```bash
git add swarm/bootstrap.py tests/test_bootstrap.py
git commit -m "feat: add bootstrap module for MCP-sourced project context"
```

---

## Task 5: Agents Refactor

**Files:**
- Modify: `swarm/agents.py`

No unit tests for agents — they wrap agno types that require a live model.

- [ ] **Step 5.1: Update `swarm/agents.py`**

Replace `swarm/agents.py` entirely:

```python
from agno.agent import Agent
from agno.tools.mcp import MCPTools
from .tool_fix import OllamaToolFix
from config.config import config


def get_model(model_id: str, host: str):
    """Ollama-first model factory. OllamaToolFix handles all Ollama tool call formats
    (native tool_calls, <tool_call> tags, <|python_tag|>, bare JSON)."""
    return OllamaToolFix(id=model_id, host=host)


_BASE_PREAMBLE = [
    "When working on a task:",
    "  1. Call memory_search() with relevant keywords to recall prior findings.",
    "After completing the task:",
    "  2. Call memory_store() with a descriptive key and any non-obvious insight.",
]


def make_coder(mcp: MCPTools, store_fn, search_fn) -> Agent:
    return Agent(
        name="Coder",
        model=get_model(config.coder_model, config.ollama_host),
        tools=[mcp, search_fn, store_fn],
        instructions=[
            *_BASE_PREAMBLE,
            "You are the implementation specialist. Write clean, idiomatic code.",
            "Always read relevant files via get_file_content() before modifying code.",
            "Follow patterns already established in the codebase.",
        ],
        role="Senior software engineer who implements features and fixes bugs.",
    )


def make_reviewer(mcp: MCPTools, search_fn) -> Agent:
    return Agent(
        name="Reviewer",
        model=get_model(config.reviewer_model, config.ollama_host),
        tools=[mcp, search_fn],
        instructions=[
            *_BASE_PREAMBLE,
            "You are the code reviewer. Check for correctness, security, and consistency.",
            "Be concise — flag real problems only, not style preferences.",
            "If the implementation looks correct, say so explicitly.",
        ],
        role="Senior engineer who reviews code for correctness and security.",
    )
```

- [ ] **Step 5.2: Verify no import errors**

```bash
python -c "from swarm.agents import make_coder, make_reviewer, get_model; print('ok')"
```

Expected: `ok`

- [ ] **Step 5.3: Commit**

```bash
git add swarm/agents.py
git commit -m "refactor: agents accept store_fn/search_fn params, strip eKam preamble"
```

---

## Task 6: Team Refactor

**Files:**
- Modify: `swarm/team.py`

- [ ] **Step 6.1: Replace `swarm/team.py` entirely**

```python
from agno.team import Team
from agno.tools.mcp import MCPTools
from .agents import make_coder, make_reviewer, get_model
from .bootstrap import bootstrap
from .memory import resolve_memory_fns
from config.config import config

_MCP_TIMEOUT = 60

_COORDINATOR_INSTRUCTIONS = [
    "Choose the FASTEST path to answer — do not call tools you don't need:",
    "",
    "For code pattern / convention questions ('how do we do X', 'what style do we use'):",
    "  1. find_files('**/<extension>') to discover real paths",
    "  2. search_files(pattern, glob) to verify the pattern across files",
    "  3. get_file_content(path) on 1-2 files if you need more detail",
    "  → Skip get_project_context and memory_search for these queries.",
    "",
    "For feature / architecture questions ('how does auth work', 'what is the X flow'):",
    "  1. get_context_section(topic) — returns only the relevant DOCS.md section",
    "  2. memory_search(keywords) if context section is insufficient",
    "  → Use get_project_context() only when you need the full overview.",
    "",
    "For implementation tasks (write code, fix a bug):",
    "  1. get_context_section(topic) for relevant architecture context",
    "  2. ALWAYS read at least one existing reference file of the same type before writing.",
    "     NEVER skip this step — guessing conventions produces broken code.",
    "  3. Delegate writing to Coder, review to Reviewer",
    "  4. memory_store() any non-obvious insight after completing",
    "",
    "── General rules ──────────────────────────────────────────────",
    "  - Base answers on file contents, not assumptions",
    "  - Synthesise member outputs into one coherent response",
]


async def run_task_async(task: str) -> str:
    project_context, has_mcp_memory = await bootstrap(
        config.mcp_url, _MCP_TIMEOUT, config.patterns_glob
    )
    store_fn, search_fn = resolve_memory_fns(config.db_url, has_mcp_memory)

    instructions = list(_COORDINATOR_INSTRUCTIONS)
    if project_context:
        instructions += ["", "── Project rules (loaded from MCP) ──────────────────", project_context]

    async with MCPTools(url=config.mcp_url, transport="sse", timeout_seconds=_MCP_TIMEOUT) as mcp:
        team = Team(
            name="AgnoHive",
            mode="coordinate",
            model=get_model(config.leader_model, config.ollama_host),
            members=[make_coder(mcp, store_fn, search_fn), make_reviewer(mcp, search_fn)],
            tools=[mcp, store_fn, search_fn],
            instructions=instructions,
            show_members_responses=True,
            max_iterations=config.max_iterations,
        )
        result = await team.arun(task)
        return result.content if hasattr(result, "content") else str(result)
```

- [ ] **Step 6.2: Verify no import errors**

```bash
python -c "from swarm.team import run_task_async; print('ok')"
```

Expected: `ok`

- [ ] **Step 6.3: Run full test suite to confirm nothing regressed**

```bash
pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 6.4: Commit**

```bash
git add swarm/team.py
git commit -m "feat: bootstrap MCP context at startup, dynamic coordinator instructions, remove eKam rules"
```

---

## Task 7: main.py Cleanup

**Files:**
- Modify: `main.py`

- [ ] **Step 7.1: Replace `main.py` entirely**

```python
"""AgnoHive entry point.
Usage:
  python3 main.py "your task here"   # single task
  python3 main.py                    # interactive loop
"""
import asyncio
import sys
from swarm.team import run_task_async


async def main() -> None:
    if len(sys.argv) > 1:
        result = await run_task_async(" ".join(sys.argv[1:]))
        print(result)
    else:
        print("AgnoHive - type 'exit' to quit.")
        while True:
            try:
                user_task = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if user_task.lower() in ("exit", "quit", ""):
                break
            result = await run_task_async(user_task)
            print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 7.2: Verify no import errors**

```bash
python -c "import main; print('ok')"
```

Expected: `ok`

- [ ] **Step 7.3: Commit**

```bash
git add main.py
git commit -m "refactor: remove local pattern loading from main.py"
```

---

## Task 8: Patterns README & Cleanup

**Files:**
- Create: `patterns/README.md`

- [ ] **Step 8.1: Verify eKam pattern files are gone**

```bash
ls patterns/
```

Expected: directory is empty (files were moved to the ekam repo earlier).

If any `.md` files remain, move them to the ekam project and delete them here.

- [ ] **Step 8.2: Create `patterns/README.md`**

```markdown
# patterns/

Pattern files live in the **target project**, not here.

agno-hive discovers them at startup by calling `find_files(PATTERNS_GLOB)` on the
connected MCP server and reading each file via `get_file_content()`.

## Convention

Store pattern markdown files in a `patterns/` directory in your project:

```
your-project/
  patterns/
    backend.md     ← SQLAlchemy, FastAPI, etc.
    frontend.md    ← React, RTK Query, SCSS, etc.
```

The MCP server's file tools (`find_files`, `get_file_content`) must be able to read them.

## Customising the glob

Set `PATTERNS_GLOB` in `.env` to match your project's directory structure:

```
PATTERNS_GLOB=docs/patterns/**/*.md
```

Default: `patterns/**/*.md`
```

- [ ] **Step 8.3: Commit**

```bash
git add patterns/README.md
git commit -m "docs: add patterns/README explaining convention for target projects"
```

---

## Task 9: Smoke Test

This task requires a live MCP server. Run it against the ekam project.

- [ ] **Step 9.1: Verify `.env` is correctly configured**

```bash
cat .env
```

Confirm all four required values are set:
- `OLLAMA_HOST` — ZGX Tailscale IP
- `MCP_URL` — ekam dev PC Tailscale IP + port
- `LEADER_MODEL`, `CODER_MODEL`, `REVIEWER_MODEL` — model names

- [ ] **Step 9.2: Run a pattern question (fast path)**

```bash
python main.py "How do we handle async database queries in this project?"
```

Expected:
- Bootstrap log appears (or silent success)
- Answer references SQLAlchemy async patterns from the ekam patterns files
- No hardcoded eKam rules in the answer — they should come from the MCP-fetched patterns

- [ ] **Step 9.3: Run an architecture question**

```bash
python main.py "What is the storage service responsible for?"
```

Expected: answer based on `get_context_section("storage")` from DOCS.md.

- [ ] **Step 9.4: Run a fresh test suite**

```bash
pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 9.5: Final commit if any fixes were needed**

```bash
git add -p
git commit -m "fix: smoke test corrections"
```
