# Mid-Flight Steering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user type a follow-up message while a `hive` run is still streaming, without it being lost or forcing them to wait — queued, then automatically delivered as the next turn the moment the current run's `done` event arrives. This is pi's `Alt+Enter` tier ("deliver after the current run completes"), the tier confirmed buildable without any agno-internals dependency. It also registers a real `tool_hooks` audit-log callable on the coordinator (Phase 0's confirmed mechanism), and is explicit about what it does **not** deliver — see Global Constraints.

**Architecture:** `cli/hive`'s existing `_start_escape_watcher()` background thread (Windows `msvcrt`-based, already proven safe alongside the SSE streaming loop) is extended — not duplicated — to also buffer typed characters and queue a full line on Enter, while preserving its exact existing Esc-with-no-buffered-text-cancels-the-run behavior. A new `_drain_steering_queue` helper, called from both the REPL loop and the one-shot CLI path, fires each queued message as a fresh chained `run_task` call once the current one returns. Separately and independently, `swarm/team.py`'s `_build_team` registers a `tool_hooks` audit-log callable on the coordinator `Team`, using the mechanism Phase 0 confirmed is a real, public `Agent`/`Team` constructor kwarg.

**Tech Stack:** Python 3.12 stdlib `threading`/`queue`/`msvcrt` (CLI, Windows only — see Global Constraints), agno 2.5.17's `tool_hooks` (server), pytest + pytest-asyncio.

## Global Constraints

- **This plan does NOT wire the client-side steering queue into the server-side `tool_hooks` checkpoint.** They are two independent deliverables in this plan. Doing so would require a new mid-run, bidirectional client↔server communication channel (e.g. a side-channel endpoint the hook polls, keyed by session/run id) — the existing `/stream` endpoint is server→client only. That channel does not exist anywhere in this codebase today and designing it is a separate, larger effort explicitly out of scope here. This is the same kind of explicit scoping decision recorded for Phase 9b (context injection) in the AGNOHive 2.3.1 Notion page — noted here rather than silently implied as solved.
- **Windows-only, matching the existing precedent.** `_start_escape_watcher()` already has no POSIX implementation (`except ImportError: return False # Unix — use Ctrl+C instead`, `cli/hive:515-516`) — this plan extends that same Windows-only thread rather than introducing new platform-specific code paths. POSIX users get the same experience they have today (no steering, Ctrl+C to interrupt); this plan does not regress or expand that.
- **Two features in the same physical background thread, not two competing threads.** A naive design would add a SECOND thread also calling `msvcrt.getch()` for line-buffering — that races with the EXISTING escape-watcher thread for the same keyboard buffer (whichever thread's `getch()` call wins consumes the keystroke, non-deterministically). The steering-line capture is added to the SAME thread/loop as the existing Esc-cancel watcher instead.
- `tool_hooks` audit logging must not change any existing coordinator behavior (no blocking, no argument mutation) — it must call the tool through unconditionally and return its exact result, matching Phase 0's confirmed middleware contract (`function(**args)` must be called for the tool call to actually happen).

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `cli/hive` | Modify | Extend `_start_escape_watcher` with steering-line capture; add `_drain_steering_queue`; wire into `repl()` and `main()`'s one-shot path |
| `tests/conftest.py` | Modify (if not already done) | `load_cli_hive` fixture |
| `tests/test_cli_hive_steering.py` | Create | Unit tests for the steering queue and drain helper |
| `swarm/team.py` | Modify | `_audit_tool_hook` + wire into `_build_team`'s `Team(...)` |
| `tests/test_team_tool_hooks.py` | Create | Unit tests for the audit hook and its wiring |

---

## Task 1: Steering-line capture in `cli/hive`

**Files:**
- Modify: `cli/hive:496-516` (`_cancel_event`, `_start_escape_watcher`)
- Modify: `tests/conftest.py` (add `load_cli_hive` fixture — **skip if an earlier plan already added it**)
- Test: `tests/test_cli_hive_steering.py`

**Interfaces:**
- Produces: `_steering_queue: queue.Queue[str]` (module-level, new), `_start_escape_watcher() -> bool` (same signature as today, extended behavior).
- Consumes: nothing new.

- [ ] **Step 1 (conditional): Add `load_cli_hive` fixture to `tests/conftest.py`**

  Skip if already present (added by the SSE Tool-Call Event Pipe or Session Tree Branching plans). Otherwise:

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

  Create `tests/test_cli_hive_steering.py`:

  ```python
  """Unit tests for cli/hive's steering-message queue. The real _watch() thread
  loop reads msvcrt keystrokes directly and isn't practical to drive from a
  test, so these tests exercise the queue and drain helper directly -- the
  same boundary the _watch loop pushes into and _drain_steering_queue reads
  from, which is where the actual logic worth testing lives."""


  def test_steering_queue_starts_empty(load_cli_hive):
      hive = load_cli_hive()
      assert hive._steering_queue.empty()


  def test_drain_steering_queue_does_nothing_when_empty(load_cli_hive, monkeypatch):
      hive = load_cli_hive()
      calls = []
      monkeypatch.setattr(hive, "run_task", lambda *a, **k: calls.append((a, k)) or "sid")

      result = hive._drain_steering_queue("ekam", "session-1", False)

      assert calls == []
      assert result == "session-1"


  def test_drain_steering_queue_fires_one_queued_message_as_a_chained_turn(load_cli_hive, monkeypatch):
      hive = load_cli_hive()
      hive._steering_queue.put("also check the parties module")
      calls = []

      def _fake_run_task(task, project_id, session_id=None, persist=False, show_resume=False):
          calls.append((task, project_id, session_id, persist))
          return "session-2"

      monkeypatch.setattr(hive, "run_task", _fake_run_task)

      result = hive._drain_steering_queue("ekam", "session-1", False)

      assert calls == [("also check the parties module", "ekam", "session-1", False)]
      assert result == "session-2"
      assert hive._steering_queue.empty()


  def test_drain_steering_queue_chains_session_id_across_multiple_queued_messages(load_cli_hive, monkeypatch):
      hive = load_cli_hive()
      hive._steering_queue.put("first follow-up")
      hive._steering_queue.put("second follow-up")
      seen_session_ids = []

      def _fake_run_task(task, project_id, session_id=None, persist=False, show_resume=False):
          seen_session_ids.append(session_id)
          return f"session-after-{task.split()[0]}"

      monkeypatch.setattr(hive, "run_task", _fake_run_task)

      result = hive._drain_steering_queue("ekam", "session-0", False)

      assert seen_session_ids == ["session-0", "session-after-first"]
      assert result == "session-after-second"
  ```

- [ ] **Step 3: Run tests to verify they fail**

  Run: `python -m pytest tests/test_cli_hive_steering.py -v`
  Expected: FAIL with `AttributeError: module 'cli_hive_under_test' has no attribute '_steering_queue'`

- [ ] **Step 4: Add `_steering_queue` and extend `_start_escape_watcher`**

  Add `import queue` to `cli/hive`'s existing stdlib imports (`cli/hive:33-40`, alongside `import threading`).

  Replace `cli/hive:496-516`:

  ```python
  # ── ESC interrupt ─────────────────────────────────────────────────────────────

  _cancel_event = threading.Event()

  def _start_escape_watcher() -> bool:
      """Start a daemon thread that sets _cancel_event when ESC is pressed.
      Returns True on Windows (msvcrt available), False otherwise."""
      _cancel_event.clear()
      try:
          import msvcrt
          def _watch():
              while not _cancel_event.is_set():
                  if msvcrt.kbhit():
                      if msvcrt.getch() == b'\x1b':
                          _cancel_event.set()
                          return
                  time.sleep(0.05)
          threading.Thread(target=_watch, daemon=True).start()
          return True
      except ImportError:
          return False  # Unix — use Ctrl+C instead
  ```

  with:

  ```python
  # ── ESC interrupt + mid-flight steering ─────────────────────────────────────────
  #
  # Both concerns share ONE background thread reading msvcrt keystrokes -- a second,
  # separate thread also calling msvcrt.getch() would race the existing ESC-cancel
  # thread for the same keyboard buffer. See the Mid-Flight Steering plan's Global
  # Constraints for why this is a single extended watcher, not two watchers.

  _cancel_event = threading.Event()
  _steering_queue: "queue.Queue[str]" = queue.Queue()

  def _start_escape_watcher() -> bool:
      """Start a daemon thread that watches keystrokes while a stream is active:
      - Esc with NO partially-typed steering text sets _cancel_event (unchanged
        existing behavior -- cancels the run).
      - Esc WITH partially-typed steering text clears just that in-progress text
        (does not cancel the run).
      - A full line of text terminated by Enter is queued to _steering_queue,
        delivered as the next chained turn once the current run's `done` event
        arrives (see _drain_steering_queue) -- pi's Alt+Enter tier, not the
        finer-grained mid-tool-batch Enter tier (see this plan's Global Constraints).
      Returns True on Windows (msvcrt available), False otherwise (POSIX --
      Ctrl+C only, same limitation this watcher already had before this plan)."""
      _cancel_event.clear()
      try:
          import msvcrt
          def _watch():
              buf: list[str] = []
              while not _cancel_event.is_set():
                  if msvcrt.kbhit():
                      ch = msvcrt.getch()
                      if ch == b'\x1b':
                          if buf:
                              buf.clear()
                          else:
                              _cancel_event.set()
                              return
                      elif ch in (b'\r', b'\n'):
                          if buf:
                              _steering_queue.put("".join(buf))
                              buf.clear()
                      elif ch not in (b'\x00', b'\xe0'):   # skip extended-key prefix bytes
                          try:
                              buf.append(ch.decode("utf-8", errors="ignore"))
                          except Exception:
                              pass
                  time.sleep(0.05)
          threading.Thread(target=_watch, daemon=True).start()
          return True
      except ImportError:
          return False  # Unix — use Ctrl+C instead
  ```

  Update `run_task`'s hint line (`cli/hive:815`):

  ```python
      esc_hint = dim("  Ctrl+C to interrupt") + (dim(" / ESC to cancel / type + Enter to queue") if has_esc else "")
  ```

- [ ] **Step 5: Run tests — still failing, `_drain_steering_queue` doesn't exist yet**

  Run: `python -m pytest tests/test_cli_hive_steering.py -v`
  Expected: FAIL with `AttributeError: module 'cli_hive_under_test' has no attribute '_drain_steering_queue'` (the queue-empty test from Step 2 now passes; the drain tests still fail)

- [ ] **Step 6: Add `_drain_steering_queue`**

  Add directly after `_run_in_thread` (which ends at `cli/hive:542`, right before the `# ── Helpers ──` section):

  ```python
  def _drain_steering_queue(project_id: str, session_id: str | None, persist: bool) -> str | None:
      """After a run completes, fire any messages queued during it (via the
      extended escape watcher above) as immediate chained follow-up turns.
      This is pi's Alt+Enter tier: delivered once the CURRENT run finishes,
      not mid-tool-batch. Returns the latest session_id."""
      while not _steering_queue.empty():
          try:
              queued_text = _steering_queue.get_nowait()
          except queue.Empty:
              break
          print(dim(f"  » delivering queued message: {queued_text[:60]}"))
          returned = run_task(queued_text, project_id, session_id=session_id, persist=persist)
          if returned:
              session_id = returned
      return session_id
  ```

- [ ] **Step 7: Run tests to verify they pass**

  Run: `python -m pytest tests/test_cli_hive_steering.py -v`
  Expected: PASS (4 tests)

- [ ] **Step 8: Wire `_drain_steering_queue` into `repl()` and `main()`'s one-shot path**

  In `repl()` (`cli/hive:1602-1614`), replace:

  ```python
              if skip_review or not review:
                  returned_id = run_task(
                      clean_task, project_id,
                      session_id=session_id, persist=persist,
                  )
              else:
                  returned_id = review_task(
                      clean_task, project_id,
                      session_id=session_id, persist=persist,
                  )

              if returned_id:
                  session_id = returned_id
  ```

  with:

  ```python
              if skip_review or not review:
                  returned_id = run_task(
                      clean_task, project_id,
                      session_id=session_id, persist=persist,
                  )
              else:
                  returned_id = review_task(
                      clean_task, project_id,
                      session_id=session_id, persist=persist,
                  )

              if returned_id:
                  session_id = returned_id
              session_id = _drain_steering_queue(project_id, session_id, persist)
  ```

  In `main()`'s one-shot task path (`cli/hive:1736-1745`), replace:

  ```python
      if args.task:
          task = " ".join(args.task)
          if args.review:
              review_task(task, project_id, session_id=args.session, persist=args.persist)
          else:
              run_task(
                  task, project_id,
                  session_id=args.session, persist=args.persist,
                  show_resume=True,
              )
  ```

  with:

  ```python
      if args.task:
          task = " ".join(args.task)
          if args.review:
              review_task(task, project_id, session_id=args.session, persist=args.persist)
          else:
              returned_id = run_task(
                  task, project_id,
                  session_id=args.session, persist=args.persist,
                  show_resume=True,
              )
              _drain_steering_queue(project_id, returned_id or args.session, args.persist)
  ```

- [ ] **Step 9: Run the full suite**

  Run: `python -m pytest tests/ -q --deselect tests/test_bootstrap.py::test_load_from_session_discovers_patterns --deselect tests/test_bootstrap.py::test_load_from_session_skips_failed_file_reads`
  Expected: PASS

- [ ] **Step 10: Commit**

  ```bash
  git add cli/hive tests/conftest.py tests/test_cli_hive_steering.py
  git commit -m "feat(cli): mid-flight steering -- queue a message while streaming, Alt+Enter tier

  Extends the existing ESC-cancel watcher thread (not a second thread -- see
  the plan's race-condition note) to also buffer typed text and queue a full
  line on Enter. _drain_steering_queue fires queued messages as chained
  turns once the current run's done event arrives; wired into both the REPL
  loop and the one-shot CLI path so a typed-but-unsent message during a
  one-shot run isn't silently lost."
  ```

---

## Task 2: `tool_hooks` audit-log callable on the coordinator

**Files:**
- Modify: `swarm/team.py:1076-1090` (`_build_team`'s `Team(...)` construction)
- Test: `tests/test_team_tool_hooks.py`

**Interfaces:**
- Produces: `_audit_tool_hook(function_name, function, args, agent)` — a plain function, registered via `tool_hooks=[_audit_tool_hook]` on the coordinator `Team`. Not currently connected to the client-side steering queue from Task 1 — see this plan's Global Constraints for why.
- Consumes: agno's `tool_hooks` mechanism (confirmed real and public in the Phase 0 spike: `agno/team/team.py:264,501,623`).

- [ ] **Step 1: Write the failing tests**

  Create `tests/test_team_tool_hooks.py`:

  ```python
  """Unit tests for the coordinator's tool_hooks audit-log callable.

  Confirmed real in the Phase 0 spike (2026-08-07): tool_hooks is a public,
  documented-in-source Agent/Team constructor kwarg (agno/team/team.py:264,
  501,623) -- middleware wrapping each tool call, not a simple before/after
  callback. A hook MUST call `function(**args)` itself to let the tool call
  proceed; not calling it blocks/short-circuits the call.
  """
  from swarm.team import _audit_tool_hook, _build_team


  def test_audit_tool_hook_calls_the_function_and_returns_its_result(capsys):
      def _fake_tool(x):
          return x * 2

      result = _audit_tool_hook(function_name="double", function=_fake_tool, args={"x": 21}, agent=None)

      assert result == 42


  def test_audit_tool_hook_prints_a_trace_line(capsys):
      def _fake_tool(x):
          return "ok"

      _audit_tool_hook(function_name="my_tool", function=_fake_tool, args={"x": 1}, agent=None)

      out = capsys.readouterr().out
      assert "my_tool" in out


  def test_audit_tool_hook_still_prints_and_reraises_on_a_failing_tool(capsys):
      def _failing_tool():
          raise RuntimeError("boom")

      try:
          _audit_tool_hook(function_name="bad_tool", function=_failing_tool, args={}, agent=None)
          assert False, "expected RuntimeError to propagate"
      except RuntimeError:
          pass

      out = capsys.readouterr().out
      assert "bad_tool" in out


  def test_build_team_registers_the_audit_tool_hook(monkeypatch):
      monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

      result = _build_team(
          agent_specs=None,
          coordinator_model="qwen2.5-coder:32b",
          coordinator_tools=None,
          mode="coordinate",
          mcp_list=[],
          instructions=[],
      )

      assert _audit_tool_hook in result.tool_hooks
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run: `python -m pytest tests/test_team_tool_hooks.py -v`
  Expected: FAIL with `ImportError: cannot import name '_audit_tool_hook' from 'swarm.team'`

- [ ] **Step 3: Add `_audit_tool_hook` and wire it into `_build_team`**

  Add directly above `_build_team` (`swarm/team.py:1051`):

  ```python
  def _audit_tool_hook(function_name, function, args, agent):
      """tool_hooks middleware: logs every coordinator tool call. Confirmed-safe,
      confirmed-real mechanism per the Phase 0 spike -- calls `function(**args)`
      unconditionally and returns its exact result, changing no behavior.

      NOT currently connected to the client-side steering queue (Task 1 of this
      plan) -- that would need a new mid-run client<->server channel this plan
      does not build. See this plan's Global Constraints.
      """
      import time as _time
      started = _time.monotonic()
      try:
          result = function(**args)
          elapsed = _time.monotonic() - started
          print(f"[team] tool_hook: {function_name}({args}) -> {elapsed:.2f}s")
          return result
      except Exception as exc:
          elapsed = _time.monotonic() - started
          print(f"[team] tool_hook: {function_name}({args}) RAISED {type(exc).__name__}: {exc} after {elapsed:.2f}s")
          raise
  ```

  In `_build_team`'s `Team(...)` construction (`swarm/team.py:1076-1090`), add one new kwarg:

  ```python
      return Team(
          name=name,
          description=description,
          mode=mode,
          model=get_model(coordinator_model, config.ollama_host),
          members=members,
          tools=_scope_coordinator_tools(coordinator_tools, mcp_list, read_only),
          instructions=instructions,
          show_members_responses=True,
          share_member_interactions=True,
          add_member_tools_to_context=True,
          markdown=True,
          max_iterations=config.max_iterations,
          tool_call_limit=config.tool_call_limit,
          tool_hooks=[_audit_tool_hook],
      )
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run: `python -m pytest tests/test_team_tool_hooks.py -v`
  Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite**

  Run: `python -m pytest tests/ -q --deselect tests/test_bootstrap.py::test_load_from_session_discovers_patterns --deselect tests/test_bootstrap.py::test_load_from_session_skips_failed_file_reads`
  Expected: PASS

- [ ] **Step 6: Commit**

  ```bash
  git add swarm/team.py tests/test_team_tool_hooks.py
  git commit -m "feat(team): register a tool_hooks audit-log callable on the coordinator

  Confirmed-real mechanism per the Phase 0 spike. Logs every tool call
  (name, args, duration, success/failure) without changing any behavior --
  calls function(**args) unconditionally. Deliberately NOT wired to the
  client-side steering queue (cli/hive) -- that needs a new mid-run
  client<->server channel this plan does not build; see Global Constraints."
  ```

---

## Verification (manual, after deployment)

1. Deploy: `git push`, then on ZGX: `cd ~/agno-hive && git pull && systemctl --user restart agno-api.service`.
2. Steering: `hive` (REPL) → send a longer-running task → while it's streaming, type a short follow-up and press Enter → confirm nothing garbles the streamed output → confirm that once the first run's footer prints, a `» delivering queued message: ...` line appears and a second run starts automatically using the SAME session.
3. Audit hook: after any run, check `journalctl --user -u agno-api.service` (or the equivalent ZGX log tail) for `[team] tool_hook: <name>(<args>) -> <N>s` lines — confirm they appear for every tool call and don't change the run's actual answer content.
