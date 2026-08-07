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
