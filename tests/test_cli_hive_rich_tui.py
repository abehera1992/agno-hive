"""Unit tests for _ToolActivityPanel (Phase 8 -- Rich collapsible TUI). Rich-mode
tests are skipped entirely if rich isn't installed in the test environment --
the whole point of this feature is that cli/hive works either way, so these
tests verify the Rich-mode behavior specifically and need rich present to run."""


def _require_rich(load_cli_hive):
    hive = load_cli_hive()
    if not hive._RICH_AVAILABLE:
        import pytest
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

    assert "✓" in rendered  # checkmark
    assert "get_file_content" in rendered
    panel.close()


def test_panel_finish_marks_failure_with_an_x(load_cli_hive):
    hive = _require_rich(load_cli_hive)
    panel = hive._ToolActivityPanel(verbose=False)
    panel.start("run_command", {"cmd": "pytest"})
    panel.finish("run_command", ok=False)

    rendered = panel.render_text()

    assert "✗" in rendered  # x mark
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


def test_run_task_uses_the_panel_for_tool_events_before_text_starts(load_cli_hive, monkeypatch):
    hive = _require_rich(load_cli_hive)
    monkeypatch.setattr(hive, "_COLOUR", True)

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
    monkeypatch.setattr(hive, "_COLOUR", True)

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


def test_run_task_falls_back_to_plain_text_when_rich_is_unavailable(load_cli_hive, monkeypatch, capsys):
    hive = load_cli_hive()
    monkeypatch.setattr(hive, "_RICH_AVAILABLE", False)
    monkeypatch.setattr(hive, "_COLOUR", True)

    def _fake_stream(endpoint, payload, timeout=600):
        yield {"type": "tool_start", "name": "search_files", "args": {"pattern": "voucher"}}
        yield {"type": "tool_end", "name": "search_files", "result_preview": "3 matches"}
        yield {"type": "chunk", "content": "Found 3 vouchers."}
        yield {"type": "done", "session": {"session_id": "abc123"}}

    monkeypatch.setattr(hive, "_stream_api", _fake_stream)
    monkeypatch.setattr(hive, "_start_escape_watcher", lambda: False)
    monkeypatch.setattr(hive, "_find_pending_diffs", lambda: [])
    monkeypatch.setattr(hive, "_find_pending_actions", lambda root: [])

    hive.run_task("research vouchers", "ekam")

    out = capsys.readouterr().out
    assert "[tool] search_files(pattern=voucher)" in out
