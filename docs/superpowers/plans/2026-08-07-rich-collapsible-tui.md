# Rich Collapsible TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render tool-call activity as a collapsed, live-updating panel (name + one-line arg summary + a status icon) instead of Plan 1's plain `[tool] name(args)` print lines — the pi `Ctrl+O`-equivalent "collapsed by default, expandable" experience — while keeping `cli/hive` runnable with zero installed dependencies for anyone who hasn't opted in.

**Architecture:** `rich` becomes an *optional* dependency, imported defensively (`try/except ImportError`) — when unavailable, `cli/hive` behaves exactly as Plan 1 left it (plain `[tool] name(args)` lines), no crash, no missing-dependency error. When available and `sys.stdout.isatty()` (mirroring the existing `_COLOUR` gate), `run_task` renders tool-call activity in a `rich.live.Live` region — collapsed one-liners, a `✓`/`✗` status icon on completion — for as long as the run is in the "tool calls, no answer text yet" phase. The Live region is finalized as static output the moment the first text `chunk` arrives (matching how a run naturally moves from "gathering" to "answering"); any tool call that starts *after* text has already begun streaming falls back to Plan 1's plain one-line print rather than reopening a second `Live` region mid-stream.

**Tech Stack:** `rich>=13.0.0` (new, optional dependency — `cli/requirements.txt`, new file), Python 3.12 stdlib otherwise.

## Global Constraints

- **`rich` is optional, not required.** `cli/hive` must still run with zero installed third-party packages, exactly as documented in its own docstring today (`cp cli/hive ~/.local/bin/hive; chmod +x ...`, no `pip install` step mentioned). Every Rich import is wrapped in `try/except ImportError`, and every Rich-mode code path has a plain-text fallback that already exists from Plan 1 — this plan adds a *better* rendering when available, never a *required* one.
- **Depends on Plan 1** (SSE Tool-Call Event Pipe) — this plan only changes how `tool_start`/`tool_end` events already delivered by that plan are rendered client-side. If Plan 1 has not been executed yet, this plan cannot proceed (its events don't exist).
- **Deliberately scoped down from a live `Ctrl+O` keystroke toggle to a pre-run env var/flag**, for a concrete reason: `_arrow_select`'s raw-keystroke-read primitives are the only precedent for capturing a keypress in this codebase, and the Mid-Flight Steering plan already established that raw `msvcrt` keystroke reading must live in exactly ONE background thread to avoid two threads racing for the same keyboard buffer. Adding a THIRD concern (live expand/collapse toggling) to that same thread, while a `rich.live.Live` region is simultaneously redrawing the terminal, is real additional complexity this plan does not take on. Instead, expand/collapse is a **pre-run** choice: `HIVE_VERBOSE_TOOLS=1` env var or `--verbose-tools` CLI flag shows full args/results always-expanded; the default stays collapsed. A live in-run toggle is a documented follow-up, not delivered here.
- No change to the SSE event shapes or server-side code from Plan 1 — this plan is entirely client-side (`cli/hive`, `cli/requirements.txt`).

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `cli/requirements.txt` | Create | Documents the optional `rich` dependency |
| `cli/hive` | Modify | Defensive Rich import; `_ToolActivityPanel` renderer class; wire into `run_task`; `--verbose-tools` flag / `HIVE_VERBOSE_TOOLS` env var; installation docstring update |
| `tests/conftest.py` | Modify (if not already done) | `load_cli_hive` fixture |
| `tests/test_cli_hive_rich_tui.py` | Create | Unit tests for `_ToolActivityPanel`, run both with and without Rich installed |

---

## Task 1: `cli/requirements.txt` and defensive import

**Files:**
- Create: `cli/requirements.txt`
- Modify: `cli/hive:1-24` (docstring), `:25-40` (imports)

- [ ] **Step 1: Create `cli/requirements.txt`**

  ```
  # Optional. cli/hive runs with ZERO installed dependencies without this --
  # see the "TUI" section of cli/hive's own docstring. Installing rich upgrades
  # tool-call rendering from plain [tool] name(args) lines to a collapsed,
  # live-updating panel. `pip install -r cli/requirements.txt` to opt in.
  rich>=13.0.0
  ```

- [ ] **Step 2: Add the defensive import and doc update to `cli/hive`**

  Add to `cli/hive`'s existing stdlib import block (`cli/hive:25-40`), right after the last stdlib import (`from urllib.parse import urlencode`):

  ```python
  # Optional TUI upgrade -- see cli/requirements.txt. Every code path below that
  # uses these falls back to plain-text rendering (Plan 1's [tool] name(args)
  # lines) when this import fails, so cli/hive keeps running with zero
  # installed dependencies for anyone who hasn't opted in.
  try:
      from rich.console import Console
      from rich.live import Live
      from rich.text import Text
      _RICH_AVAILABLE = True
  except ImportError:
      _RICH_AVAILABLE = False
  ```

  Update the module docstring's `Usage:` section (`cli/hive:15-24`) to add one line documenting the optional upgrade:

  ```
  Optional TUI upgrade (pip install -r cli/requirements.txt):
      Renders tool-call activity as a collapsed, live-updating panel instead
      of plain [tool] name(args) lines. Falls back to plain text automatically
      if rich isn't installed -- no flag needed to opt out.
  ```

- [ ] **Step 3: Manual check — confirm the fallback actually works**

  No automated test for "does the bare import succeed/fail correctly" (that's testing Python's own import machinery, not this repo's logic) — instead:

  Run: `python -c "import sys; sys.path.insert(0, 'cli'); exec(open('cli/hive').read().split('if __name__')[0]); print('_RICH_AVAILABLE =', _RICH_AVAILABLE)"`

  Expected: prints `_RICH_AVAILABLE = True` if `rich` is installed in the current environment, `False` otherwise — either way, no `ImportError` propagates out.

- [ ] **Step 4: Commit**

  ```bash
  git add cli/requirements.txt cli/hive
  git commit -m "feat(cli): add optional rich dependency with defensive import + fallback"
  ```

---

## Task 2: `_ToolActivityPanel` renderer

**Files:**
- Modify: `cli/hive` (add near `_short_repr`, which Plan 1 places above `run_task`)
- Modify: `tests/conftest.py` (add `load_cli_hive` fixture — **skip if an earlier plan already added it**)
- Test: `tests/test_cli_hive_rich_tui.py`

**Interfaces:**
- Consumes: `_short_repr` (Plan 1, Task 3) for arg-value truncation; `_RICH_AVAILABLE`, `Console`, `Live`, `Text` (Task 1).
- Produces: `_ToolActivityPanel` — a small class wrapping a `Live` region. `start(name, args)` adds a collapsed pending line; `finish(name, ok)` marks the most recent matching entry with a `✓`/`✗`; `close() -> None` stops the `Live` and leaves its last frame as static terminal output. `verbose: bool = False` constructor flag controls collapsed-vs-expanded rendering.

- [ ] **Step 1 (conditional): Add `load_cli_hive` fixture to `tests/conftest.py`**

  Skip if already present (added by an earlier plan). Otherwise:

  ```python
  import importlib.util
  import sys
  from importlib.machinery import SourceFileLoader
  from pathlib import Path


  @pytest.fixture
  def load_cli_hive():
      """Dynamically load cli/hive (no .py extension, not a package) as a fresh
      module object each time it's called, so tests can inspect/monkeypatch its
      module-level functions without polluting sys.modules across tests."""
      def _load():
          path = Path(__file__).resolve().parent.parent / "cli" / "hive"
          # cli/hive has no .py suffix, so spec_from_file_location can't infer a
          # loader on its own (it returns None) -- pass SourceFileLoader explicitly.
          # Confirmed live 2026-08-07 (SSE Tool-Call Event Pipe plan, Task 3): the
          # literal version without an explicit loader crashes at module_from_spec
          # with spec=None -- standard CPython importlib behavior (PEP 451) for an
          # extensionless filename, not platform-specific.
          loader = SourceFileLoader("cli_hive_under_test", str(path))
          spec = importlib.util.spec_from_file_location("cli_hive_under_test", path, loader=loader)
          module = importlib.util.module_from_spec(spec)
          sys.modules["cli_hive_under_test"] = module
          spec.loader.exec_module(module)
          return module
      return _load
  ```

- [ ] **Step 2: Write the failing tests**

  Create `tests/test_cli_hive_rich_tui.py`:

  ```python
  """Unit tests for _ToolActivityPanel. Skipped entirely if rich isn't
  installed in the test environment (the whole point of this plan is that
  cli/hive works either way -- these tests verify the Rich-mode behavior
  specifically, so they need rich present to run)."""
  import pytest


  def _require_rich(load_cli_hive):
      hive = load_cli_hive()
      if not hive._RICH_AVAILABLE:
          pytest.skip("rich not installed in this environment")
      return hive


  def test_panel_start_adds_a_pending_collapsed_line(load_cli_hive):
      hive = _require_rich(load_cli_hive)
      panel = hive._ToolActivityPanel(verbose=False)
      panel.start("search_files", {"pattern": "voucher"})

      rendered = panel.render_text()

      assert "search_files(pattern=voucher)" in rendered
      panel.close()


  def test_panel_finish_marks_success_with_a_checkmark(load_cli_hive):
      hive = _require_rich(load_cli_hive)
      panel = hive._ToolActivityPanel(verbose=False)
      panel.start("get_file_content", {"relative_path": "x.py"})
      panel.finish("get_file_content", ok=True)

      rendered = panel.render_text()

      assert "✓" in rendered   # ✓
      assert "get_file_content" in rendered
      panel.close()


  def test_panel_finish_marks_failure_with_an_x(load_cli_hive):
      hive = _require_rich(load_cli_hive)
      panel = hive._ToolActivityPanel(verbose=False)
      panel.start("run_command", {"cmd": "pytest"})
      panel.finish("run_command", ok=False)

      rendered = panel.render_text()

      assert "✗" in rendered   # ✗
      panel.close()


  def test_panel_verbose_mode_shows_all_args_not_truncated(load_cli_hive):
      hive = _require_rich(load_cli_hive)
      long_arg = "x" * 100
      panel_collapsed = hive._ToolActivityPanel(verbose=False)
      panel_collapsed.start("search_files", {"pattern": long_arg})
      collapsed_text = panel_collapsed.render_text()
      panel_collapsed.close()

      panel_verbose = hive._ToolActivityPanel(verbose=True)
      panel_verbose.start("search_files", {"pattern": long_arg})
      verbose_text = panel_verbose.render_text()
      panel_verbose.close()

      assert len(verbose_text) > len(collapsed_text)
      assert long_arg in verbose_text
      assert long_arg not in collapsed_text


  def test_panel_finish_for_unstarted_tool_does_not_raise(load_cli_hive):
      hive = _require_rich(load_cli_hive)
      panel = hive._ToolActivityPanel(verbose=False)
      panel.finish("never_started", ok=True)   # must not raise
      panel.close()


  def test_verbose_tools_env_var_enables_verbose_mode(load_cli_hive, monkeypatch):
      monkeypatch.setenv("HIVE_VERBOSE_TOOLS", "1")
      hive = load_cli_hive()   # re-load AFTER setting the env var so module-level read picks it up
      assert hive._VERBOSE_TOOLS is True


  def test_verbose_tools_defaults_to_false(load_cli_hive, monkeypatch):
      monkeypatch.delenv("HIVE_VERBOSE_TOOLS", raising=False)
      hive = load_cli_hive()
      assert hive._VERBOSE_TOOLS is False
  ```

- [ ] **Step 3: Run tests to verify they fail**

  Run: `python -m pytest tests/test_cli_hive_rich_tui.py -v`
  Expected: FAIL with `AttributeError: module 'cli_hive_under_test' has no attribute '_ToolActivityPanel'` (or `_VERBOSE_TOOLS`)

- [ ] **Step 4: Add `_VERBOSE_TOOLS` config and `_ToolActivityPanel`**

  Add near the other env-driven config at the top of `cli/hive` (`cli/hive:53-62`, alongside `AGNO_HOST` etc.):

  ```python
  HIVE_VERBOSE_TOOLS  = os.getenv("HIVE_VERBOSE_TOOLS", "") in ("1", "true", "True")
  _VERBOSE_TOOLS       = HIVE_VERBOSE_TOOLS   # overridable by --verbose-tools in main()
  ```

  Add `_ToolActivityPanel` directly after `_short_repr` (added by Plan 1, Task 3, right above `run_task`):

  ```python
  class _ToolActivityPanel:
      """Collapsed, live-updating tool-call panel for the 'gathering' phase of
      a run (before any answer text has started streaming). Falls back to
      Plan 1's plain [tool] name(args) print lines everywhere Rich isn't
      available or a tool call starts AFTER text has already begun streaming
      -- see this plan's Global Constraints for why a second concurrent Live
      region isn't attempted mid-stream."""

      def __init__(self, verbose: bool = False):
          self.verbose = verbose
          self._entries: list[dict] = []   # [{"name": str, "args": dict, "status": "pending"|"ok"|"error"}]
          self._live = Live(self._render(), refresh_per_second=8, transient=False) if _RICH_AVAILABLE else None
          if self._live:
              self._live.start()

      def _render(self):
          lines = []
          for e in self._entries:
              icon = {"pending": "…", "ok": "✓", "error": "✗"}[e["status"]]
              if self.verbose:
                  args_str = ", ".join(f"{k}={v!r}" for k, v in e["args"].items())
              else:
                  args_str = ", ".join(f"{k}={_short_repr(v)}" for k, v in e["args"].items())
              lines.append(f"  [dim][tool][/dim] {icon} {e['name']}({args_str})")
          return Text.from_markup("\n".join(lines)) if lines else Text("")

      def render_text(self) -> str:
          """Plain-string snapshot of the current render, for tests (Rich's own
          Text object doesn't compare usefully with a plain 'in' check)."""
          return self._render().plain

      def start(self, name: str, args: dict) -> None:
          self._entries.append({"name": name, "args": args or {}, "status": "pending"})
          if self._live:
              self._live.update(self._render())

      def finish(self, name: str, ok: bool) -> None:
          for e in reversed(self._entries):
              if e["name"] == name and e["status"] == "pending":
                  e["status"] = "ok" if ok else "error"
                  break
          if self._live:
              self._live.update(self._render())

      def close(self) -> None:
          if self._live:
              self._live.stop()
  ```

- [ ] **Step 5: Run tests to verify they pass**

  Run: `python -m pytest tests/test_cli_hive_rich_tui.py -v`
  Expected: PASS (7 tests) if `rich` is installed in the dev environment; the Rich-dependent tests SKIP (not fail) if it isn't — `test_verbose_tools_defaults_to_false` and `test_verbose_tools_env_var_enables_verbose_mode` still run either way since they don't touch `_ToolActivityPanel`.

- [ ] **Step 6: Commit**

  ```bash
  git add cli/hive tests/test_cli_hive_rich_tui.py tests/conftest.py
  git commit -m "feat(cli): add _ToolActivityPanel -- collapsed live tool-call rendering

  verbose vs collapsed controlled by HIVE_VERBOSE_TOOLS env var / --verbose-tools
  flag (Task 3), not a live in-run keystroke toggle -- see this plan's Global
  Constraints for why a live Ctrl+O-equivalent isn't attempted here."
  ```

---

## Task 3: Wire `_ToolActivityPanel` into `run_task` and add `--verbose-tools`

**Files:**
- Modify: `cli/hive` (`run_task`'s SSE loop — the `tool_start`/`tool_end`/`chunk` branches added by Plan 1, Task 3), `main()` (new `--verbose-tools` flag)
- Test: `tests/test_cli_hive_rich_tui.py` (extend)

**Interfaces:**
- Consumes: Plan 1's SSE `tool_start`/`tool_end` events; `_ToolActivityPanel` (Task 2).

- [ ] **Step 1: Write the failing test**

  Add to `tests/test_cli_hive_rich_tui.py`:

  ```python
  def test_run_task_uses_the_panel_for_tool_events_before_text_starts(load_cli_hive, monkeypatch):
      hive = _require_rich(load_cli_hive)

      def _fake_stream(endpoint, payload, timeout=600):
          yield {"type": "tool_start", "name": "search_files", "args": {"pattern": "voucher"}}
          yield {"type": "tool_end", "name": "search_files", "result_preview": "3 matches"}
          yield {"type": "chunk", "content": "Found 3 vouchers."}
          yield {"type": "done", "session": {"session_id": "abc123"}}

      closed = []
      monkeypatch.setattr(hive, "_stream_api", _fake_stream)
      monkeypatch.setattr(hive, "_start_escape_watcher", lambda: False)
      monkeypatch.setattr(hive, "_find_pending_diffs", lambda: [])
      monkeypatch.setattr(hive, "_find_pending_actions", lambda root: [])
      real_close = hive._ToolActivityPanel.close
      monkeypatch.setattr(hive._ToolActivityPanel, "close", lambda self: (closed.append(True), real_close(self)))

      hive.run_task("research vouchers", "ekam")

      assert closed   # the panel was closed (finalized as static output) before the answer streamed


  def test_run_task_falls_back_to_plain_print_for_a_tool_call_after_text_has_started(load_cli_hive, monkeypatch, capsys):
      hive = _require_rich(load_cli_hive)

      def _fake_stream(endpoint, payload, timeout=600):
          yield {"type": "chunk", "content": "Checking further... "}
          yield {"type": "tool_start", "name": "search_files", "args": {"pattern": "grn"}}
          yield {"type": "chunk", "content": "Found it."}
          yield {"type": "done", "session": {"session_id": "abc123"}}

      monkeypatch.setattr(hive, "_stream_api", _fake_stream)
      monkeypatch.setattr(hive, "_start_escape_watcher", lambda: False)
      monkeypatch.setattr(hive, "_find_pending_diffs", lambda: [])
      monkeypatch.setattr(hive, "_find_pending_actions", lambda root: [])

      hive.run_task("research vouchers", "ekam")

      out = capsys.readouterr().out
      assert "[tool] search_files(pattern=grn)" in out   # plain-text fallback line, not a Live panel
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run: `python -m pytest tests/test_cli_hive_rich_tui.py -v -k "run_task_uses_the_panel or falls_back_to_plain"`
  Expected: FAIL — `run_task` doesn't use `_ToolActivityPanel` at all yet (still Plan 1's plain-print-only behavior for every tool event)

- [ ] **Step 3: Rewire `run_task`'s SSE loop**

  Replace `run_task`'s `chunk`/`tool_start`/`tool_end` branches (added by Plan 1, Task 3) with:

  ```python
      done_data: dict = {}
      first_chunk = True
      returned_session_id: str | None = None
      use_rich = _RICH_AVAILABLE and _COLOUR
      panel = _ToolActivityPanel(verbose=_VERBOSE_TOOLS) if use_rich else None
      panel_active = panel is not None

      try:
          for event in _stream_api("/stream", payload, timeout=600):
              if _cancel_event.is_set():
                  if panel:
                      panel.close()
                  print(f"\n{yellow('  interrupted')}")
                  _cancel_event.clear()
                  return None

              etype = event.get("type")

              if etype == "chunk":
                  chunk = event.get("content", "")
                  if chunk:
                      if panel_active and panel:
                          panel.close()          # finalize the tool-activity panel as static output
                          panel_active = False
                      if first_chunk:
                          if _COLOUR:
                              print(" " * 54, end="\r")  # clear "thinking..." line
                          print()
                          first_chunk = False
                      print(chunk, end="", flush=True)

              elif etype == "tool_start":
                  name = event.get("name", "?")
                  args = event.get("args", {}) or {}
                  if panel_active and panel:
                      panel.start(name, args)
                  else:
                      if first_chunk:
                          if _COLOUR:
                              print(" " * 54, end="\r")
                          print()
                          first_chunk = False
                      args_str = ", ".join(f"{k}={_short_repr(v)}" for k, v in args.items())
                      print(dim(f"  [tool] {name}({args_str})"))

              elif etype == "tool_end":
                  name = event.get("name", "?")
                  if panel_active and panel:
                      panel.finish(name, ok=True)
                  # plain-text fallback mode: tool_end stays a no-op, matching Plan 1

              elif etype == "done":
                  done_data = event
                  returned_session_id = event.get("session", {}).get("session_id")
                  break

              elif etype == "error":
                  if panel:
                      panel.close()
                  print(f"\n{red('  error: ' + event.get('content', 'unknown'))}")
                  return None

      except KeyboardInterrupt:
          if panel:
              panel.close()
          print(f"\n{yellow('  interrupted')}")
          _cancel_event.set()
          return None
      except urllib.error.HTTPError as e:
          if panel:
              panel.close()
          body = e.read().decode(errors="replace")
          try:
              msg = json.loads(body).get("detail", body)
          except Exception:
              msg = body
          print(red(f"\n  server error {e.code}: {msg}"))
          return None
      except urllib.error.URLError as e:
          if panel:
              panel.close()
          print(red(f"\n  connection error: {e.reason}"))
          print(dim(f"  is AGNOHive running at {AGNO_HOST}?"))
          return None
      except Exception as e:
          if panel:
              panel.close()
          print(red(f"\n  error: {e}"))
          return None

      if panel_active and panel:
          panel.close()   # the run ended with tool calls but no answer text -- still finalize
  ```

  (This replaces the entire `try:`/`except:` block of `run_task` that Plan 1, Task 3 last modified — every existing exception branch is unchanged except for the added `if panel: panel.close()` line, so a raised or cancelled run never leaves a `Live` region hanging open on the terminal.)

- [ ] **Step 4: Add `--verbose-tools` flag to `main()`**

  Add next to `--persist` in the argparse block (`cli/hive:1645-1646`):

  ```python
      parser.add_argument("--verbose-tools",    action="store_true",
                          help="always show full tool-call args/results, not collapsed (default: HIVE_VERBOSE_TOOLS env var, or collapsed)")
  ```

  In `main()`, right after `args = parser.parse_args()` (`cli/hive:1668`), add:

  ```python
      if args.verbose_tools:
          global _VERBOSE_TOOLS
          _VERBOSE_TOOLS = True
  ```

  (`global AGNO_HOST, AGNO_TEAM, AGNO_MCP_URL, AGNO_SYSTEM_MCP_URL` already exists at `cli/hive:1636` for the same reason — add `_VERBOSE_TOOLS` as its own separate `global` statement right where shown above, since it's set conditionally rather than unconditionally like the existing block.)

- [ ] **Step 5: Run tests to verify they pass**

  Run: `python -m pytest tests/test_cli_hive_rich_tui.py -v`
  Expected: PASS (9 tests total; Rich-dependent ones skip if `rich` isn't installed)

- [ ] **Step 6: Run the full suite**

  Run: `python -m pytest tests/ -q --deselect tests/test_bootstrap.py::test_load_from_session_discovers_patterns --deselect tests/test_bootstrap.py::test_load_from_session_skips_failed_file_reads`
  Expected: PASS

- [ ] **Step 7: Commit**

  ```bash
  git add cli/hive tests/test_cli_hive_rich_tui.py
  git commit -m "feat(cli): wire _ToolActivityPanel into run_task; add --verbose-tools

  Panel is used for tool calls before any answer text starts streaming;
  finalized as static output the moment text begins. A tool call that
  starts AFTER text has already streamed falls back to a plain [tool]
  name(args) line rather than opening a second concurrent Live region."
  ```

---

## Verification (manual, after deployment; requires `rich` installed locally to see the upgrade)

1. `pip install -r cli/requirements.txt` on the machine running `hive`.
2. Deploy the server-side pieces this plan depends on (Plan 1) if not already deployed.
3. `hive "read config/config.py and swarm/team.py"` — confirm a collapsed, live-updating panel shows `[tool] get_file_content(...)` lines with a spinner/pending icon, each flipping to `✓` as it completes, then the panel finalizes as static text right as the answer starts streaming below it.
4. `hive --verbose-tools "read config/config.py"` — confirm the same run shows full untruncated args instead of the collapsed one-liner.
5. `pip uninstall rich` (or run in a venv without it) → `hive "read config/config.py"` → confirm it still works, falling back to Plan 1's plain `[tool] name(args)` lines with no error.
