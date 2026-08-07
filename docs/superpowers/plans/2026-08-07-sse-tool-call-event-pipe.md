# SSE Tool-Call Event Pipe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop discarding tool-call events. Today `swarm/team.py`'s `run_task_stream` filters agno's event stream down to `TeamRunContent` only — every `ToolCallStarted`/`ToolCallCompleted` event is dropped before it reaches the client. Add new, additive SSE event types (`tool_start`, `tool_end`) end-to-end (team → server → CLI) so the CLI can print one line per tool call as it happens.

**Architecture:** A new pure function `_stream_event_to_chunk` in `swarm/team.py` classifies each raw agno event into either a text delta (existing `str` behavior, unchanged) or a new tool-event `dict` sentinel (distinguished from the existing `{"__done__": True, ...}` sentinel by a `"__tool_event__"` key). `api/server.py`'s `stream_endpoint` gains a matching pure function `_tool_event_to_sse` that turns that dict into a new SSE `data:` line. `cli/hive`'s `run_task` prints a one-line `[tool] name(args)` on `tool_start`. This is Phase 1 + Phase 2 of the AGNOHive 2.3.1 Pi-migration Notion page, and is a hard dependency for the later Mid-Flight Steering and Rich TUI plans (both need this event pipe to exist first).

**Tech Stack:** Python 3.12, agno 2.5.17 (`agno.run.team.TeamRunEvent`/`ToolCallStartedEvent`/`ToolCallCompletedEvent`, `agno.models.response.ToolExecution`), FastAPI `StreamingResponse`, pytest + pytest-asyncio, stdlib `urllib`/`json` (CLI — zero new dependencies).

## Global Constraints

- Existing SSE event shapes (`chunk`, `done`, `error`) and their exact JSON keys must not change — this is purely additive.
- `run_task_stream`'s existing `full_content` accumulation (used for the final combined answer, count-marker guard, and handoff summary) must only ever include `TeamRunContent` text — tool-event dicts must never be appended to it.
- No new third-party dependency in this plan (Rich is a later, separate plan).
- Agno's real event-type string values (verified against the installed package, not assumed): `"TeamRunContent"` (existing), `"TeamToolCallStarted"`, `"TeamToolCallCompleted"` (`agno/run/team.py:147-148` in the installed `agno==2.5.17` package at `EkamApp/.venv/Lib/site-packages/agno`). `ToolCallStartedEvent.tool` / `ToolCallCompletedEvent.tool` are `agno.models.response.ToolExecution` objects with fields `tool_name: str`, `tool_args: dict`, `result: str` (`agno/models/response.py:28-36`).

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `swarm/team.py` | Modify | Extract `_stream_event_to_chunk`; wire into `run_task_stream`'s loop |
| `tests/test_team_stream_events.py` | Create | Unit tests for `_stream_event_to_chunk` |
| `api/server.py` | Modify | Add `_tool_event_to_sse`; wire into `stream_endpoint`'s `generate()` |
| `tests/test_server_stream_events.py` | Create | Unit tests for `_tool_event_to_sse` |
| `cli/hive` | Modify | Handle `tool_start`/`tool_end` SSE events in `run_task`; add `_short_repr` helper |
| `tests/conftest.py` | Modify | Add a `load_cli_hive` fixture — `cli/hive` has no extension, so no test file has ever imported it before this plan; every CLI test in this and later plans depends on this fixture |
| `tests/test_cli_hive_tool_events.py` | Create | Unit tests for the new CLI tool-event rendering |

---

## Task 1: Extract `_stream_event_to_chunk` in `swarm/team.py`

**Files:**
- Modify: `swarm/team.py:1206-1212` (inside `run_task_stream`)
- Test: `tests/test_team_stream_events.py`

**Interfaces:**
- Produces: `_stream_event_to_chunk(event) -> str | dict | None` — importable as `from swarm.team import _stream_event_to_chunk`. Returns a non-empty `str` for a `TeamRunContent` text delta (unchanged existing behavior), a `dict` shaped `{"__tool_event__": "start", "name": str, "args": dict}` or `{"__tool_event__": "end", "name": str, "result_preview": str | None}` for a tool-call event, or `None` for every other/unrecognized event type (dropped, same as today).
- Consumes: nothing new — operates on whatever object agno's `team.arun(task, stream=True)` yields, duck-typed via `getattr`.

- [ ] **Step 1: Write the failing tests**

  Create `tests/test_team_stream_events.py`:

  ```python
  """Unit tests for _stream_event_to_chunk — the pure classifier that decides what
  run_task_stream yields for each raw agno event. No agno Team/MCP dependency:
  built from bare objects with just the attributes the function reads."""
  from types import SimpleNamespace

  from swarm.team import _stream_event_to_chunk


  def _content_event(content):
      return SimpleNamespace(event="TeamRunContent", content=content)


  def _tool_started(name, args):
      tool = SimpleNamespace(tool_name=name, tool_args=args, result=None)
      return SimpleNamespace(event="TeamToolCallStarted", tool=tool)


  def _tool_completed(name, result):
      tool = SimpleNamespace(tool_name=name, tool_args=None, result=result)
      return SimpleNamespace(event="TeamToolCallCompleted", tool=tool)


  def test_content_event_returns_the_text_chunk():
      assert _stream_event_to_chunk(_content_event("hello")) == "hello"


  def test_content_event_with_empty_string_returns_none():
      assert _stream_event_to_chunk(_content_event("")) is None


  def test_content_event_with_non_string_content_returns_none():
      assert _stream_event_to_chunk(_content_event(None)) is None


  def test_tool_started_returns_start_sentinel():
      event = _tool_started("search_files", {"pattern": "voucher"})
      out = _stream_event_to_chunk(event)
      assert out == {"__tool_event__": "start", "name": "search_files", "args": {"pattern": "voucher"}}


  def test_tool_started_with_no_args_defaults_to_empty_dict():
      event = _tool_started("list_directory", None)
      out = _stream_event_to_chunk(event)
      assert out["args"] == {}


  def test_tool_completed_returns_end_sentinel_with_truncated_preview():
      event = _tool_completed("get_file_content", "x" * 500)
      out = _stream_event_to_chunk(event)
      assert out["__tool_event__"] == "end"
      assert out["name"] == "get_file_content"
      assert len(out["result_preview"]) == 200


  def test_tool_completed_with_non_string_result_has_no_preview():
      event = _tool_completed("run_command", {"exit_code": 0})
      out = _stream_event_to_chunk(event)
      assert out["result_preview"] is None


  def test_tool_event_with_no_tool_attribute_returns_none():
      event = SimpleNamespace(event="TeamToolCallStarted", tool=None)
      assert _stream_event_to_chunk(event) is None


  def test_unrecognized_event_type_returns_none():
      event = SimpleNamespace(event="TeamRunStarted", content=None)
      assert _stream_event_to_chunk(event) is None


  def test_event_missing_event_attribute_returns_none():
      event = SimpleNamespace(content="stray text")  # no .event at all
      assert _stream_event_to_chunk(event) is None
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run: `cd agno-hive && python -m pytest tests/test_team_stream_events.py -v`
  Expected: FAIL with `ImportError: cannot import name '_stream_event_to_chunk' from 'swarm.team'`

- [ ] **Step 3: Add `_stream_event_to_chunk` and wire it into `run_task_stream`**

  In `swarm/team.py`, add this function directly above `run_task_stream` (which starts at line 1093):

  ```python
  def _stream_event_to_chunk(event) -> str | dict | None:
      """Classify one raw agno team.arun(stream=True) event into what run_task_stream
      yields downstream. Duck-typed via getattr since agno event objects vary by type.

      Returns:
        str  — a TeamRunContent text delta (existing behavior, unchanged)
        dict — a tool-call sentinel: {"__tool_event__": "start", "name": str, "args": dict}
               or {"__tool_event__": "end", "name": str, "result_preview": str | None}
        None — every other event type (dropped, same as the previous hard filter)
      """
      event_type = getattr(event, "event", "")
      if event_type == "TeamRunContent":
          chunk = getattr(event, "content", None)
          return chunk if isinstance(chunk, str) and chunk else None
      if event_type == "TeamToolCallStarted":
          tool = getattr(event, "tool", None)
          if tool is None:
              return None
          return {
              "__tool_event__": "start",
              "name": tool.tool_name,
              "args": tool.tool_args or {},
          }
      if event_type == "TeamToolCallCompleted":
          tool = getattr(event, "tool", None)
          if tool is None:
              return None
          result = tool.result
          return {
              "__tool_event__": "end",
              "name": tool.tool_name,
              "result_preview": result[:200] if isinstance(result, str) else None,
          }
      return None
  ```

  Then replace the loop body at `swarm/team.py:1206-1212` (currently):

  ```python
              try:
                  async for event in team.arun(task, stream=True):
                      last_event = event
                      event_type = getattr(event, "event", "")
                      chunk = getattr(event, "content", None)
                      if isinstance(chunk, str) and chunk and event_type == "TeamRunContent":
                          full_content.append(chunk)
                          yield chunk
  ```

  with:

  ```python
              try:
                  async for event in team.arun(task, stream=True):
                      last_event = event
                      out = _stream_event_to_chunk(event)
                      if isinstance(out, str):
                          full_content.append(out)
                          yield out
                      elif isinstance(out, dict):
                          yield out
  ```

  Update `run_task_stream`'s docstring (currently `swarm/team.py:1105-1109`) to add the new yield shape:

  ```python
      """Same setup as run_task_async but yields text chunks as the coordinator generates them.

      Yields:
        str  — content chunks from the coordinator as they arrive
        dict — a tool-call sentinel {"__tool_event__": "start"|"end", ...} (see
               _stream_event_to_chunk), or the final sentinel
               {"__done__": True, "content": str, "tokens": dict}
      """
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run: `python -m pytest tests/test_team_stream_events.py -v`
  Expected: PASS (11 tests)

- [ ] **Step 5: Run the full existing suite to confirm no regression**

  Run: `python -m pytest tests/ -q --deselect tests/test_bootstrap.py::test_load_from_session_discovers_patterns --deselect tests/test_bootstrap.py::test_load_from_session_skips_failed_file_reads -k "not test_sessions"`
  Expected: PASS, same count as before this change plus 11

- [ ] **Step 6: Commit**

  ```bash
  git add swarm/team.py tests/test_team_stream_events.py
  git commit -m "feat(team): extract _stream_event_to_chunk, classify tool-call events

  Stop silently dropping ToolCallStarted/ToolCallCompleted events inside
  run_task_stream's TeamRunContent-only filter. Yields new dict sentinels
  (__tool_event__: start/end) alongside the existing str/text-delta and
  __done__ shapes -- additive, TeamRunContent behavior unchanged."
  ```

---

## Task 2: Forward tool events over SSE in `api/server.py`

**Files:**
- Modify: `api/server.py:1-14` (imports), `api/server.py:335-428` (`stream_endpoint`)
- Test: `tests/test_server_stream_events.py`

**Interfaces:**
- Consumes: `swarm.team._stream_event_to_chunk`'s dict shapes (Task 1) — `{"__tool_event__": "start", "name": str, "args": dict}` / `{"__tool_event__": "end", "name": str, "result_preview": str | None}`.
- Produces: `_tool_event_to_sse(chunk: dict) -> str | None` — importable as `from api.server import _tool_event_to_sse`. Returns a full `"data: {...}\n\n"` SSE line for a recognized tool-event dict, or `None` for anything else (so the caller knows to fall through to other chunk-shape checks).

- [ ] **Step 1: Write the failing tests**

  Create `tests/test_server_stream_events.py`:

  ```python
  """Unit tests for _tool_event_to_sse — the pure formatter that turns a
  run_task_stream tool-event dict into an SSE data line. No FastAPI app
  dependency: this is a plain string-in, string-out function."""
  import json

  from api.server import _tool_event_to_sse


  def test_start_event_produces_tool_start_sse_line():
      chunk = {"__tool_event__": "start", "name": "search_files", "args": {"pattern": "x"}}
      line = _tool_event_to_sse(chunk)
      assert line.startswith("data: ")
      assert line.endswith("\n\n")
      payload = json.loads(line[len("data: "):].strip())
      assert payload == {"type": "tool_start", "name": "search_files", "args": {"pattern": "x"}}


  def test_end_event_produces_tool_end_sse_line():
      chunk = {"__tool_event__": "end", "name": "get_file_content", "result_preview": "abc"}
      line = _tool_event_to_sse(chunk)
      payload = json.loads(line[len("data: "):].strip())
      assert payload == {"type": "tool_end", "name": "get_file_content", "result_preview": "abc"}


  def test_unrecognized_dict_shape_returns_none():
      assert _tool_event_to_sse({"__done__": True, "content": "x"}) is None


  def test_dict_with_no_tool_event_key_returns_none():
      assert _tool_event_to_sse({"name": "x"}) is None
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run: `python -m pytest tests/test_server_stream_events.py -v`
  Expected: FAIL with `ImportError: cannot import name '_tool_event_to_sse' from 'api.server'`

- [ ] **Step 3: Add `import json` and `_tool_event_to_sse` to `api/server.py`**

  Add to the top-level imports (`api/server.py:1-4`, after `import time`):

  ```python
  import json
  ```

  Add this function above `stream_endpoint` (which starts at `api/server.py:335`):

  ```python
  def _tool_event_to_sse(chunk: dict) -> str | None:
      """Turn a run_task_stream tool-event dict (see swarm.team._stream_event_to_chunk)
      into an SSE data line, or None if `chunk` isn't a recognized tool-event shape."""
      kind = chunk.get("__tool_event__")
      if kind == "start":
          payload = {"type": "tool_start", "name": chunk["name"], "args": chunk["args"]}
      elif kind == "end":
          payload = {"type": "tool_end", "name": chunk["name"], "result_preview": chunk["result_preview"]}
      else:
          return None
      return f"data: {json.dumps(payload)}\n\n"
  ```

  Update `stream_endpoint`'s docstring (`api/server.py:339-342`) to document the new events:

  ```python
      Events:
        data: {"type": "chunk",      "content": "<text>"}
        data: {"type": "tool_start", "name": "<tool>", "args": {...}}
        data: {"type": "tool_end",   "name": "<tool>", "result_preview": "<text>" | null}
        data: {"type": "done",       "session": {...}, "input_tokens": N, ...}
        data: {"type": "error",      "content": "<message>"}
  ```

  In `generate()` (`api/server.py:378-426`), add a branch to the existing `if isinstance(chunk, str): ... elif isinstance(chunk, dict) and chunk.get("__done__"): ...` chain — insert BEFORE the `__done__` check, since both are dict but `__tool_event__` must be checked first:

  ```python
              async for chunk in run_task_stream(
                  task=request.task,
                  agent_specs=agent_specs,
                  coordinator_model=coordinator_model,
                  coordinator_tools=coordinator_tools,
                  mcp_url=mcp_url,
                  mcp_urls=_resolve_mcp_urls(request.mcp_urls, config.lightrag_mcp_url),
                  project_id=request.project_id,
                  session_id=session_id,
                  mode=stream_mode,
              ):
                  if isinstance(chunk, str):
                      yield f"data: {_json.dumps({'type': 'chunk', 'content': chunk})}\n\n"

                  elif isinstance(chunk, dict) and "__tool_event__" in chunk:
                      sse_line = _tool_event_to_sse(chunk)
                      if sse_line:
                          yield sse_line

                  elif isinstance(chunk, dict) and chunk.get("__done__"):
  ```

  (The rest of the `__done__` branch body is unchanged — only the `elif` condition ordering gained one new branch above it.)

- [ ] **Step 4: Run tests to verify they pass**

  Run: `python -m pytest tests/test_server_stream_events.py -v`
  Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite**

  Run: `python -m pytest tests/ -q --deselect tests/test_bootstrap.py::test_load_from_session_discovers_patterns --deselect tests/test_bootstrap.py::test_load_from_session_skips_failed_file_reads -k "not test_sessions"`
  Expected: PASS, previous count + 4

- [ ] **Step 6: Commit**

  ```bash
  git add api/server.py tests/test_server_stream_events.py
  git commit -m "feat(server): forward tool_start/tool_end SSE events

  Additive new SSE event types alongside the existing chunk/done/error --
  no existing event shape changes. _tool_event_to_sse is a pure function,
  unit-tested without a FastAPI TestClient."
  ```

---

## Task 3: Render tool-call lines in `cli/hive`

**Files:**
- Modify: `cli/hive:837-862` (inside `run_task`'s SSE consumption loop)
- Modify: `tests/conftest.py` (new `load_cli_hive` fixture)
- Test: `tests/test_cli_hive_tool_events.py`

**Interfaces:**
- Consumes: SSE `tool_start`/`tool_end` events from Task 2, shaped `{"type": "tool_start", "name": str, "args": dict}` / `{"type": "tool_end", "name": str, "result_preview": str | None}`.
- Produces: `_short_repr(value, limit: int = 40) -> str` — a small formatting helper other CLI plans (steering, TUI) can also reuse for arg previews.

- [ ] **Step 1: Add the `load_cli_hive` fixture to `tests/conftest.py`**

  `cli/hive` has no `.py` extension and `cli/` has no `__init__.py`, so no test in this repo has ever imported it before this plan — every future CLI-touching plan needs this same loader. Add to `tests/conftest.py`:

  ```python
  import importlib.util
  import sys
  from pathlib import Path


  @pytest.fixture
  def load_cli_hive():
      """Dynamically load cli/hive (no .py extension, not a package) as a fresh
      module object each time it's called, so tests can inspect/monkeypatch its
      module-level functions without polluting sys.modules across tests."""
      def _load():
          path = Path(__file__).resolve().parent.parent / "cli" / "hive"
          spec = importlib.util.spec_from_file_location("cli_hive_under_test", path)
          module = importlib.util.module_from_spec(spec)
          sys.modules["cli_hive_under_test"] = module
          spec.loader.exec_module(module)
          return module
      return _load
  ```

  (`import pytest` is already at the top of `tests/conftest.py:1` — the two new imports above go alongside it.)

- [ ] **Step 2: Write the failing tests**

  Create `tests/test_cli_hive_tool_events.py`:

  ```python
  """Unit tests for cli/hive's tool-call rendering, added in the SSE Tool-Call
  Event Pipe plan. Loaded dynamically via the load_cli_hive fixture since
  cli/hive has no .py extension."""


  def test_short_repr_passes_short_strings_through(load_cli_hive):
      hive = load_cli_hive()
      assert hive._short_repr("voucher") == "voucher"


  def test_short_repr_truncates_long_strings(load_cli_hive):
      hive = load_cli_hive()
      out = hive._short_repr("x" * 100, limit=40)
      assert len(out) <= 44   # 40 chars + "..."
      assert out.endswith("...")


  def test_short_repr_handles_non_string_values(load_cli_hive):
      hive = load_cli_hive()
      assert hive._short_repr(42) == "42"
      assert hive._short_repr({"a": 1}) == "{'a': 1}"


  def test_run_task_prints_tool_start_line(load_cli_hive, monkeypatch, capsys):
      hive = load_cli_hive()

      def _fake_stream(endpoint, payload, timeout=600):
          yield {"type": "tool_start", "name": "search_files", "args": {"pattern": "voucher"}}
          yield {"type": "chunk", "content": "found it"}
          yield {"type": "done", "session": {"session_id": "abc123"}}

      monkeypatch.setattr(hive, "_stream_api", _fake_stream)
      monkeypatch.setattr(hive, "_start_escape_watcher", lambda: False)
      monkeypatch.setattr(hive, "_find_pending_diffs", lambda: [])
      monkeypatch.setattr(hive, "_find_pending_actions", lambda root: [])

      hive.run_task("research vouchers", "ekam")

      out = capsys.readouterr().out
      assert "[tool] search_files(pattern=voucher)" in out
      assert "found it" in out


  def test_run_task_tool_end_is_a_noop_in_plain_mode(load_cli_hive, monkeypatch, capsys):
      """Plan 1 only prints on tool_start; a plain-text tool_end must not error
      or print anything extra (the Rich TUI plan later upgrades this)."""
      hive = load_cli_hive()

      def _fake_stream(endpoint, payload, timeout=600):
          yield {"type": "tool_start", "name": "get_file_content", "args": {"relative_path": "x.py"}}
          yield {"type": "tool_end", "name": "get_file_content", "result_preview": "..."}
          yield {"type": "chunk", "content": "done"}
          yield {"type": "done", "session": {"session_id": "abc123"}}

      monkeypatch.setattr(hive, "_stream_api", _fake_stream)
      monkeypatch.setattr(hive, "_start_escape_watcher", lambda: False)
      monkeypatch.setattr(hive, "_find_pending_diffs", lambda: [])
      monkeypatch.setattr(hive, "_find_pending_actions", lambda root: [])

      hive.run_task("read a file", "ekam")  # must not raise

      out = capsys.readouterr().out
      assert "[tool] get_file_content" in out
  ```

- [ ] **Step 3: Run tests to verify they fail**

  Run: `python -m pytest tests/test_cli_hive_tool_events.py -v`
  Expected: FAIL with `AttributeError: module 'cli_hive_under_test' has no attribute '_short_repr'`

- [ ] **Step 4: Add `_short_repr` and wire tool-event handling into `run_task`**

  Add above `run_task` (which starts at `cli/hive:802`):

  ```python
  def _short_repr(value, limit: int = 40) -> str:
      """One-line, length-capped representation of a tool-call argument value
      for the [tool] name(args) preview line."""
      s = value if isinstance(value, str) else repr(value)
      return s if len(s) <= limit else s[:limit] + "..."
  ```

  In `run_task`'s SSE loop (currently `cli/hive:837-862`), add two new `elif` branches between the existing `chunk` branch and the `done` branch:

  ```python
      try:
          for event in _stream_api("/stream", payload, timeout=600):
              if _cancel_event.is_set():
                  print(f"\n{yellow('  interrupted')}")
                  _cancel_event.clear()
                  return None

              etype = event.get("type")

              if etype == "chunk":
                  chunk = event.get("content", "")
                  if chunk:
                      if first_chunk:
                          if _COLOUR:
                              print(" " * 54, end="\r")  # clear "thinking..." line
                          print()
                          first_chunk = False
                      print(chunk, end="", flush=True)

              elif etype == "tool_start":
                  if first_chunk:
                      if _COLOUR:
                          print(" " * 54, end="\r")
                      print()
                      first_chunk = False
                  name = event.get("name", "?")
                  args = event.get("args", {}) or {}
                  args_str = ", ".join(f"{k}={_short_repr(v)}" for k, v in args.items())
                  print(dim(f"  [tool] {name}({args_str})"))

              elif etype == "tool_end":
                  pass  # plain-text mode has nothing more to show here; see the Rich TUI plan

              elif etype == "done":
                  done_data = event
                  returned_session_id = event.get("session", {}).get("session_id")
                  break

              elif etype == "error":
                  print(f"\n{red('  error: ' + event.get('content', 'unknown'))}")
                  return None
  ```

- [ ] **Step 5: Run tests to verify they pass**

  Run: `python -m pytest tests/test_cli_hive_tool_events.py -v`
  Expected: PASS (5 tests)

- [ ] **Step 6: Run the full suite**

  Run: `python -m pytest tests/ -q --deselect tests/test_bootstrap.py::test_load_from_session_discovers_patterns --deselect tests/test_bootstrap.py::test_load_from_session_skips_failed_file_reads -k "not test_sessions"`
  Expected: PASS, previous count + 5

- [ ] **Step 7: Commit**

  ```bash
  git add cli/hive tests/conftest.py tests/test_cli_hive_tool_events.py
  git commit -m "feat(cli): print [tool] name(args) lines from the new SSE tool events

  Adds _short_repr helper and a load_cli_hive test fixture (cli/hive has no
  .py extension, so it was never test-importable before this). tool_end is
  a deliberate no-op in plain-text mode -- the Rich TUI plan upgrades it to
  a checkmark/collapse update."
  ```

---

## Verification (end to end, manual — no automated integration test in this plan)

This plan's automated tests cover each layer's pure-function boundary in isolation (Task 1/2/3). There is deliberately no full FastAPI-`TestClient`-through-`cli/hive` integration test in this plan, since `run_task_stream` requires a live or heavily-mocked `Team`/`MCPTools`/ZGX connection that the existing test suite doesn't set up anywhere (confirmed: zero existing tests reference `TestClient`, `/stream`, or `run_task_stream`). After all three tasks are committed and deployed (`git push`, then on ZGX: `git pull && systemctl --user restart agno-api.service`), do one live manual check:

```bash
hive "read config/config.py"
```

Expected: one or more `[tool] get_file_content(...)` lines print before the answer text, and the run completes normally with no change to the existing footer.
