# Batch Concurrency Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give agents real parallel I/O for independent file reads/searches, without needing control over agno's own tool-dispatch loop (confirmed out of reach in this repo — see the Phase 0 spike recorded in the AGNOHive 2.3.1 Notion page: tool dispatch lives entirely inside the `agno` package's `Team.arun`/`Agent.arun`/`MCPTools`).

**Architecture:** A single tool call can still parallelize internally even though the model issues tool calls one at a time. Add `get_files_batch(paths: list[str])` and `search_files_batch(pattern: str, glob_filters: list[str], max_results: int)` to `hive-mcp/tools/context.py`, each wrapping the existing synchronous `get_file_content`/`search_files` in `asyncio.to_thread` + `asyncio.gather` so multiple files/globs are read concurrently under the hood. The model still sees exactly one tool call per batch — compatible with every existing "≤N reads per call" prompt-shaping rule in client projects.

**Tech Stack:** Python 3.12 stdlib `asyncio` only — no new dependency. FastMCP tool registration (existing `_tool()` helper in `hive-mcp/main.py`, which already correctly detects and wraps `async def` tools via `inspect.iscoroutinefunction`).

## Global Constraints

- `get_file_content` and `search_files` (`hive-mcp/tools/context.py:186`, `:323`) are both plain **synchronous** functions that never raise on ordinary error conditions (file not found, read failure) — they return a descriptive error *string* instead. The batch wrappers must not change or duplicate that error-formatting logic; they call the existing functions unmodified via `asyncio.to_thread` and only add their own handling for the rare case of a genuine exception escaping (`return_exceptions=True` in `asyncio.gather`).
- Output must go through the existing `_cap()` truncation helper (`hive-mcp/tools/context.py:26-34`) exactly once, on the combined multi-file output — not once per file (which could still produce a combined result far over `_MAX_OUTPUT_CHARS`).
- New tools must be registered through the existing `_tool()` tracing wrapper in `hive-mcp/main.py`, not a bare `mcp.tool()(...)` call.
- This plan does not touch `swarm/team.py`, `api/server.py`, or `cli/hive` — it is scoped entirely to `hive-mcp`.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `hive-mcp/tools/context.py` | Modify | `get_files_batch`, `search_files_batch` |
| `hive-mcp/tests/test_context_batch_tools.py` | Create | Unit tests, real tmp_path files (matching `test_context_get_file_content.py`'s existing style) |
| `hive-mcp/main.py` | Modify | Register the 2 new tools |

---

## Task 1: `get_files_batch`

**Files:**
- Modify: `hive-mcp/tools/context.py` (add after `get_file_content`, which ends at line 243)
- Test: `hive-mcp/tests/test_context_batch_tools.py`

**Interfaces:**
- Produces: `async def get_files_batch(paths: list[str]) -> str` — importable as `from tools.context import get_files_batch` (matching this test file's existing `from tools import context` import style — call as `context.get_files_batch(...)`).
- Consumes: `context.get_file_content` (existing, unmodified), `context._cap` (existing, unmodified).

- [ ] **Step 1: Write the failing tests**

  Create `hive-mcp/tests/test_context_batch_tools.py`:

  ```python
  """Unit tests for the batch concurrency tools -- real files under tmp_path,
  same style as test_context_get_file_content.py (PROJECT_ROOT monkeypatched,
  no mocking of get_file_content/search_files themselves)."""
  import asyncio

  import pytest

  from tools import context


  def test_get_files_batch_reads_multiple_files_and_labels_each_section(tmp_path, monkeypatch):
      monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
      (tmp_path / "a.py").write_text("print('a')", encoding="utf-8")
      (tmp_path / "b.py").write_text("print('b')", encoding="utf-8")

      result = asyncio.run(context.get_files_batch(["a.py", "b.py"]))

      assert "=== a.py ===" in result
      assert "print('a')" in result
      assert "=== b.py ===" in result
      assert "print('b')" in result


  def test_get_files_batch_includes_the_not_found_message_for_a_missing_file(tmp_path, monkeypatch):
      monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
      (tmp_path / "a.py").write_text("print('a')", encoding="utf-8")

      result = asyncio.run(context.get_files_batch(["a.py", "missing.py"]))

      assert "print('a')" in result
      assert "File not found: missing.py" in result


  def test_get_files_batch_preserves_input_order_in_output(tmp_path, monkeypatch):
      monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
      for name in ("z.py", "a.py", "m.py"):
          (tmp_path / name).write_text(f"# {name}", encoding="utf-8")

      result = asyncio.run(context.get_files_batch(["z.py", "a.py", "m.py"]))

      assert result.index("=== z.py ===") < result.index("=== a.py ===") < result.index("=== m.py ===")


  def test_get_files_batch_runs_reads_concurrently_not_sequentially(tmp_path, monkeypatch):
      """Real concurrency check: if 3 reads ran sequentially with a 0.1s delay each,
      total wall time would be >=0.3s. Concurrent (asyncio.gather + to_thread) keeps
      it well under that."""
      import time
      monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
      for name in ("a.py", "b.py", "c.py"):
          (tmp_path / name).write_text("x", encoding="utf-8")

      def _slow_get_file_content(relative_path, offset=0, limit=0):
          time.sleep(0.1)
          return f"content of {relative_path}"

      monkeypatch.setattr(context, "get_file_content", _slow_get_file_content)

      t0 = time.perf_counter()
      result = asyncio.run(context.get_files_batch(["a.py", "b.py", "c.py"]))
      elapsed = time.perf_counter() - t0

      assert elapsed < 0.25   # well under the 0.3s a sequential run would take
      assert "content of a.py" in result
      assert "content of c.py" in result


  def test_get_files_batch_output_is_capped_by_max_output_chars(tmp_path, monkeypatch):
      monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
      monkeypatch.setattr(context, "_MAX_OUTPUT_CHARS", 100)
      (tmp_path / "big.py").write_text("x" * 500, encoding="utf-8")

      result = asyncio.run(context.get_files_batch(["big.py"]))

      assert len(result) <= 200   # capped text + the "TRUNCATED" message, well under 500
      assert "TRUNCATED" in result
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run: `cd agno-hive/hive-mcp && python -m pytest tests/test_context_batch_tools.py -v`
  Expected: FAIL with `AttributeError: module 'tools.context' has no attribute 'get_files_batch'`

- [ ] **Step 3: Add `get_files_batch` to `hive-mcp/tools/context.py`**

  Add `import asyncio` to the top-level imports (`hive-mcp/tools/context.py:6-10`, alongside the existing `import ast`/`import os`/`import re`/`import sys`).

  Add this function after `get_file_content` (which ends at line 243, right before the `_GLOB_FALLBACK_PREFIXES` comment block at line 246):

  ```python
  async def get_files_batch(paths: list[str]) -> str:
      """
      Read multiple files in ONE tool call, in parallel -- use this instead of several
      separate get_file_content() calls when you already know which files you need
      (e.g. a pattern file + the file you're about to edit + a related test file).

      Each file is read with get_file_content()'s exact same behavior (line numbers,
      skeleton-on-oversized, not-found message) -- this only parallelizes the I/O,
      it does not change what comes back for any individual file.

      Args:
          paths: relative paths, e.g. ['src/api/routes.py', 'src/api/models.py']
      """
      async def _read_one(p: str) -> str:
          return await asyncio.to_thread(get_file_content, p)

      outcomes = await asyncio.gather(*(_read_one(p) for p in paths), return_exceptions=True)
      sections = []
      for p, outcome in zip(paths, outcomes):
          if isinstance(outcome, BaseException):
              sections.append(f"=== {p} ===\nERROR: {outcome}")
          else:
              sections.append(f"=== {p} ===\n{outcome}")
      return _cap("\n\n".join(sections))
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run: `python -m pytest tests/test_context_batch_tools.py -v -k get_files_batch`
  Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

  ```bash
  git add hive-mcp/tools/context.py hive-mcp/tests/test_context_batch_tools.py
  git commit -m "feat(hive-mcp): add get_files_batch -- parallel reads in one tool call

  Wraps the existing synchronous get_file_content in asyncio.to_thread +
  asyncio.gather. The model still issues exactly one tool call; the I/O
  underneath is real concurrent thread-offloaded reads, not sequential."
  ```

---

## Task 2: `search_files_batch`

**Files:**
- Modify: `hive-mcp/tools/context.py` (add after `search_files`)
- Test: `hive-mcp/tests/test_context_batch_tools.py`

**Interfaces:**
- Produces: `async def search_files_batch(pattern: str, glob_filters: list[str], max_results: int = 80) -> str`.
- Consumes: `context.search_files` (existing, unmodified).

- [ ] **Step 1: Write the failing tests**

  Add to `hive-mcp/tests/test_context_batch_tools.py`:

  ```python
  def test_search_files_batch_searches_each_glob_and_labels_sections(tmp_path, monkeypatch):
      monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
      (tmp_path / "a.py").write_text("def handle_x(): pass", encoding="utf-8")
      (tmp_path / "b.ts").write_text("function handle_x() {}", encoding="utf-8")

      result = asyncio.run(context.search_files_batch("handle_x", ["**/*.py", "**/*.ts"]))

      assert "=== glob: **/*.py ===" in result
      assert "=== glob: **/*.ts ===" in result


  def test_search_files_batch_passes_pattern_and_max_results_through(tmp_path, monkeypatch):
      monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
      calls = []

      def _fake_search_files(pattern, glob_filter="**/*", max_results=80):
          calls.append((pattern, glob_filter, max_results))
          return f"searched {pattern} in {glob_filter}"

      monkeypatch.setattr(context, "search_files", _fake_search_files)

      asyncio.run(context.search_files_batch("voucher", ["**/*.py", "**/*.tsx"], max_results=20))

      assert ("voucher", "**/*.py", 20) in calls
      assert ("voucher", "**/*.tsx", 20) in calls


  def test_search_files_batch_runs_concurrently(tmp_path, monkeypatch):
      import time
      monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)

      def _slow_search(pattern, glob_filter="**/*", max_results=80):
          time.sleep(0.1)
          return f"result for {glob_filter}"

      monkeypatch.setattr(context, "search_files", _slow_search)

      t0 = time.perf_counter()
      result = asyncio.run(context.search_files_batch("x", ["**/*.py", "**/*.ts", "**/*.tsx"]))
      elapsed = time.perf_counter() - t0

      assert elapsed < 0.25
      assert "result for **/*.py" in result
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run: `python -m pytest tests/test_context_batch_tools.py -v -k search_files_batch`
  Expected: FAIL with `AttributeError: module 'tools.context' has no attribute 'search_files_batch'`

- [ ] **Step 3: Add `search_files_batch`**

  Add directly after `get_files_batch` (added in Task 1):

  ```python
  async def search_files_batch(pattern: str, glob_filters: list[str], max_results: int = 80) -> str:
      """
      Search the SAME pattern across multiple glob scopes in ONE tool call, in parallel --
      use this instead of several separate search_files() calls when checking a term
      across file types (e.g. '**/*.py' and '**/*.ts') or across multiple directories.

      Args:
          pattern:      Regex or literal string to search for (same as search_files)
          glob_filters: e.g. ['**/*.py', '**/*.ts'] -- one search per glob, in parallel
          max_results:  Max matching lines PER GLOB (default 80, same as search_files)
      """
      async def _search_one(g: str) -> str:
          return await asyncio.to_thread(search_files, pattern, g, max_results)

      outcomes = await asyncio.gather(*(_search_one(g) for g in glob_filters), return_exceptions=True)
      sections = []
      for g, outcome in zip(glob_filters, outcomes):
          if isinstance(outcome, BaseException):
              sections.append(f"=== glob: {g} ===\nERROR: {outcome}")
          else:
              sections.append(f"=== glob: {g} ===\n{outcome}")
      return _cap("\n\n".join(sections))
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run: `python -m pytest tests/test_context_batch_tools.py -v`
  Expected: PASS (8 tests total — 5 from Task 1 + 3 from this task)

- [ ] **Step 5: Commit**

  ```bash
  git add hive-mcp/tools/context.py hive-mcp/tests/test_context_batch_tools.py
  git commit -m "feat(hive-mcp): add search_files_batch -- one pattern, multiple globs, parallel"
  ```

---

## Task 3: Register both tools in `hive-mcp/main.py`

**Files:**
- Modify: `hive-mcp/main.py:26-28` (import block), `:169-172` (registration block)

**Interfaces:**
- Consumes: `get_files_batch`, `search_files_batch` (Tasks 1–2), `hive-mcp/main.py`'s existing `_tool()` helper (`main.py:164-166`, unchanged).

- [ ] **Step 1: Update the import block**

  `hive-mcp/main.py:26-28` currently imports `get_file_content` (line 26) and `search_files` (line 28) among others from `tools.context`. Extend that same `from .tools.context import (...)` (or equivalent existing import statement) to also pull in the two new names:

  ```python
      get_file_content,
      get_files_batch,
      find_files,
      search_files,
      search_files_batch,
  ```

  (Insert `get_files_batch` immediately after `get_file_content` and `search_files_batch` immediately after `search_files`, preserving the existing list's ordering convention of "read tool, its batch variant.")

- [ ] **Step 2: Register both tools**

  In the "Context + file reading" registration block (`hive-mcp/main.py:169-178`), add the two new calls immediately after their singular counterparts:

  ```python
  _tool(get_project_context)
  _tool(get_file_content)
  _tool(get_files_batch)
  _tool(find_files)
  _tool(search_files)
  _tool(search_files_batch)
  _tool(count_matches)
  _tool(verify_claims)
  _tool(list_skills)
  _tool(load_skill)
  _tool(list_directory)
  _tool(list_directory_tree)
  ```

- [ ] **Step 3: Manual smoke check (no automated test — this is FastMCP registration wiring, not logic)**

  Run: `cd hive-mcp && python -c "import main; print([t for t in ['get_files_batch', 'search_files_batch'] if hasattr(main, t) or t in dir(main.mcp._tool_manager._tools if hasattr(main.mcp, '_tool_manager') else main.mcp)])"`

  If that introspection path doesn't match this FastMCP version's internal attribute names, the simpler equivalent check is: start the server locally (`python main.py`) and confirm both new tool names appear in its startup tool-count log line, then stop it — there is no dedicated test file for "does main.py register N tools," matching this repo's existing convention (no other tool registration in `main.py` has its own test either).

- [ ] **Step 4: Commit**

  ```bash
  git add hive-mcp/main.py
  git commit -m "feat(hive-mcp): register get_files_batch and search_files_batch as MCP tools"
  ```

---

## Verification (deploy + empirical dispatch-mode check)

1. Deploy (this is hive-mcp, so the Docker path, not the ZGX `systemctl` path): push to `agno-hive` `main`, wait for the GHCR CI build, then from the EkamApp directory: `docker compose -f docker-compose.hive.yml pull hive-mcp && docker rm -f hive-mcp && docker compose -f docker-compose.hive.yml up -d hive-mcp`.
2. Live check the new tools work: `agno_run("Read config/config.py and swarm/team.py using get_files_batch in one call")` (or via `hive` CLI) — confirm the answer references content from both files, and check `docker logs hive-mcp --tail 20` for a `[tool] get_files_batch(...)` trace line.
3. **Empirical dispatch-mode check (informational, no code change — Notion feasibility row 4's gate):** on that same live run, inspect the `docker logs hive-mcp` timestamps for any case where two *different* tool names' `[tool] ... -> ... in Xs` lines start within a few milliseconds of each other (as opposed to one starting only after the previous one's line already printed) — that pattern is evidence the coordinator requested 2+ tool calls in one model turn and agno's `asyncio.gather`-based async dispatch (confirmed in the Phase 0 spike, `agno/models/base.py:2646-2648`) already parallelized them, with zero new code needed beyond what this plan already ships. If every tool call's trace line only starts after the prior one's fully printed, the model is issuing one call at a time and the batch tools above are the only concurrency lever actually in play for now.
