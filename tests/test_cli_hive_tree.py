"""Unit tests for cli/hive's /tree, /branch, --fork commands (Phase 5/6)."""


def test_cmd_branch_calls_the_branch_endpoint_and_returns_editable_content(load_cli_hive, monkeypatch):
    hive = load_cli_hive()
    calls = []

    def _fake_call_api(endpoint, payload, timeout=300):
        calls.append((endpoint, payload))
        return {"new_leaf_id": 1, "editable_content": "earlier message text"}

    monkeypatch.setattr(hive, "call_api", _fake_call_api)
    result = hive._cmd_branch("session-uuid", 5)

    assert calls == [("/sessions/session-uuid/branch", {"message_id": 5})]
    assert result == "earlier message text"


def test_cmd_branch_with_no_active_session_returns_none(load_cli_hive):
    hive = load_cli_hive()
    result = hive._cmd_branch(None, 5)
    assert result is None


def test_cmd_branch_prints_a_warning_on_404_and_returns_none(load_cli_hive, monkeypatch, capsys):
    import urllib.error
    hive = load_cli_hive()

    def _fake_call_api(endpoint, payload, timeout=300):
        raise urllib.error.HTTPError(endpoint, 404, "not found", None, None)

    monkeypatch.setattr(hive, "call_api", _fake_call_api)
    result = hive._cmd_branch("session-uuid", 999)

    assert result is None
    assert "not found" in capsys.readouterr().out.lower()


def test_cmd_tree_lists_messages_and_lets_user_pick(load_cli_hive, monkeypatch):
    hive = load_cli_hive()
    fake_tree = {
        "messages": [
            {"id": 1, "parent_message_id": None, "role": "user", "content": "root message here", "created_at": None, "depth": 0},
            {"id": 2, "parent_message_id": 1, "role": "assistant", "content": "reply message here", "created_at": None, "depth": 1},
        ]
    }
    monkeypatch.setattr(hive, "call_get", lambda endpoint, params=None, timeout=30: fake_tree)
    monkeypatch.setattr(hive, "_arrow_select", lambda options, hints=None: 0)  # picks the first entry
    monkeypatch.setattr(hive, "call_api", lambda endpoint, payload, timeout=300: {"new_leaf_id": None, "editable_content": "root message here"})

    result = hive._cmd_tree("session-uuid")

    assert result == "root message here"


def test_cmd_tree_indents_by_depth_in_the_picker_options(load_cli_hive, monkeypatch):
    hive = load_cli_hive()
    fake_tree = {
        "messages": [
            {"id": 1, "parent_message_id": None, "role": "user", "content": "root", "created_at": None, "depth": 0},
            {"id": 2, "parent_message_id": 1, "role": "assistant", "content": "child", "created_at": None, "depth": 1},
        ]
    }
    captured = {}

    def _fake_arrow_select(options, hints=None):
        captured["options"] = options
        return -1  # cancel, we only care about what was rendered

    monkeypatch.setattr(hive, "call_get", lambda endpoint, params=None, timeout=30: fake_tree)
    monkeypatch.setattr(hive, "_arrow_select", _fake_arrow_select)

    hive._cmd_tree("session-uuid")

    assert captured["options"][0].startswith("[1]")       # depth 0 -- no indent
    assert captured["options"][1].startswith("  [2]")     # depth 1 -- one indent level


def test_cmd_tree_returns_none_when_cancelled(load_cli_hive, monkeypatch):
    hive = load_cli_hive()
    fake_tree = {"messages": [{"id": 1, "parent_message_id": None, "role": "user", "content": "x", "created_at": None, "depth": 0}]}
    monkeypatch.setattr(hive, "call_get", lambda endpoint, params=None, timeout=30: fake_tree)
    monkeypatch.setattr(hive, "_arrow_select", lambda options, hints=None: -1)  # Esc/cancel

    result = hive._cmd_tree("session-uuid")

    assert result is None


def test_cmd_tree_with_no_active_session_returns_none(load_cli_hive):
    hive = load_cli_hive()
    result = hive._cmd_tree(None)
    assert result is None


def test_cmd_tree_with_no_messages_returns_none(load_cli_hive, monkeypatch):
    hive = load_cli_hive()
    monkeypatch.setattr(hive, "call_get", lambda endpoint, params=None, timeout=30: {"messages": []})
    result = hive._cmd_tree("session-uuid")
    assert result is None


def test_cmd_fork_oneshot_prints_new_session_id(load_cli_hive, monkeypatch, capsys):
    hive = load_cli_hive()
    calls = []

    def _fake_call_api(endpoint, payload, timeout=300):
        calls.append((endpoint, payload))
        return {"session_id": "new-uuid-123"}

    monkeypatch.setattr(hive, "call_api", _fake_call_api)

    hive._cmd_fork_oneshot("session-uuid", "forked title", "ekam")

    assert calls == [("/sessions/session-uuid/fork", {"title": "forked title", "project_id": "ekam"})]
    assert "new-uuid-123" in capsys.readouterr().out


def test_cmd_fork_oneshot_warns_on_404(load_cli_hive, monkeypatch, capsys):
    import urllib.error
    hive = load_cli_hive()

    def _fake_call_api(endpoint, payload, timeout=300):
        raise urllib.error.HTTPError(endpoint, 404, "not found", None, None)

    monkeypatch.setattr(hive, "call_api", _fake_call_api)

    hive._cmd_fork_oneshot("session-uuid", "title", "ekam")

    assert "no messages to fork" in capsys.readouterr().out.lower()
