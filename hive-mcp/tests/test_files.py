from tools import files


def _setup(tmp_path, monkeypatch, initial_text):
    # files.py does `from config import PROJECT_ROOT, WRITE_REVIEW` (direct name
    # import) — monkeypatching config.PROJECT_ROOT has NO effect here. Patch the
    # module-local bound names directly.
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "WRITE_REVIEW", False)
    f = tmp_path / "sample.py"
    f.write_text(initial_text, encoding="utf-8")
    files._last_failed_call.clear()
    return f


def test_apply_diff_failure_includes_near_match_hint(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    status_filter = 'active'\n    return status_filter\n")

    result = files.apply_diff("sample.py", "status_filter = 'inactive'", "status_filter = 'archived'")

    assert "old_string not found" in result
    assert "status_filter = 'active'" in result  # the near-match hint shows the real line


def test_apply_diff_no_hint_when_nothing_close(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    result = files.apply_diff("sample.py", "completely unrelated text with no resemblance", "x")

    assert "old_string not found" in result
    assert "Closest existing text" not in result


def test_apply_diff_second_identical_failure_hard_stops(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    first = files.apply_diff("sample.py", "does not exist", "replacement")
    second = files.apply_diff("sample.py", "does not exist", "replacement")

    assert "old_string not found" in first
    assert "STOPPED" in second


def test_apply_diff_success_clears_failure_tracking(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    files.apply_diff("sample.py", "does not exist", "replacement")       # fails, tracked
    files.apply_diff("sample.py", "return 1", "return 2")                # succeeds, clears tracking
    third = files.apply_diff("sample.py", "does not exist", "replacement")  # same failure again

    assert "STOPPED" not in third


def test_apply_diff_different_failure_after_first_is_not_treated_as_repeat(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    first = files.apply_diff("sample.py", "does not exist", "replacement")
    second = files.apply_diff("sample.py", "also does not exist", "other replacement")

    assert "STOPPED" not in second
