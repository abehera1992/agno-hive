"""Unit tests for cli/hive's steering-message queue (Phase 7). The real
_watch() thread loop reads msvcrt keystrokes directly and isn't practical
to drive from a test, so these tests exercise the queue and drain helper
directly -- the same boundary the _watch loop pushes into and
_drain_steering_queue reads from, which is where the actual logic worth
testing lives."""


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


def test_drain_steering_queue_keeps_prior_session_id_when_run_task_returns_none(load_cli_hive, monkeypatch):
    """If a queued run_task call fails (returns None), the session_id must not be
    silently wiped -- the next queued message (if any) should still chain off the
    last KNOWN-GOOD session."""
    hive = load_cli_hive()
    hive._steering_queue.put("this one fails")
    monkeypatch.setattr(hive, "run_task", lambda *a, **k: None)

    result = hive._drain_steering_queue("ekam", "session-1", False)

    assert result == "session-1"
