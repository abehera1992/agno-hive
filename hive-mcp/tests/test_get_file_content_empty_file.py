"""Regression tests: an existing-but-empty file must not read as "not found".

Live, 2026-08-21 (T1-T13 re-run). Asked to list the Python files in
API/business-service/router/, a run called get_file_content on that package's
__init__.py -- a real, normal, 0-byte file -- got back an empty string, and
answered:

    The file `API/business-service/router/__init__.py` does not exist in the
    codebase. This is confirmed by multiple attempts to read it, including with
    progressively larger line limits, all returning an empty result.

All six files were there. _numbered_lines([]) is "", byte-identical to nothing,
so the model had no way to tell "empty" from "missing" and reasoned to the only
explanation available. It then burned its entire tool_call_limit re-reading a
file whose content could not change with offset or limit.

The "File not found" branch has carried explicit anti-retry wording for a while.
This is the same class of failure with none of that guidance.
"""
from tools import context


def _empty(tmp_path, name="__init__.py"):
    p = tmp_path / name
    p.write_text("", encoding="utf-8")
    return p


def test_an_empty_file_does_not_return_an_empty_string(tmp_path, monkeypatch):
    """The whole bug in one assertion."""
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    _empty(tmp_path)

    out = context.get_file_content("__init__.py")

    assert out.strip(), "an empty file returned an empty result — indistinguishable from missing"


def test_it_says_the_file_exists(tmp_path, monkeypatch):
    """The exact wrong conclusion the live run reached must be contradicted
    explicitly, not merely left un-implied."""
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    _empty(tmp_path)

    out = context.get_file_content("__init__.py")

    assert "EXISTS" in out
    assert "EMPTY" in out
    assert "NOT 'file not found'" in out


def test_it_tells_the_caller_not_to_retry_with_offset_or_limit(tmp_path, monkeypatch):
    """The second half of the live failure: ~a dozen re-reads with growing limits,
    exhausting the run's tool budget. Naming the useless next action is what stops
    it -- the same reason the "File not found" branch carries this wording."""
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    _empty(tmp_path)

    out = context.get_file_content("__init__.py")

    assert "Do NOT retry" in out
    assert "offset" in out and "limit" in out


def test_the_empty_message_wins_over_a_ranged_read(tmp_path, monkeypatch):
    """Stated BEFORE the offset/limit branch on purpose: the live run's retries all
    passed a limit, so a fix that only covers the whole-file path would not have
    stopped the loop that actually happened."""
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    _empty(tmp_path)

    for kwargs in ({"limit": 10}, {"offset": 5}, {"offset": 0, "limit": 500}):
        out = context.get_file_content("__init__.py", **kwargs)
        assert "EXISTS" in out and "EMPTY" in out, kwargs


def test_a_genuinely_missing_file_still_says_not_found(tmp_path, monkeypatch):
    """The distinction has to cut both ways, or the fix just moves the confusion."""
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    _empty(tmp_path)   # so the dir is non-empty, but the requested name is absent

    out = context.get_file_content("no_such_file.py")

    assert "not found" in out.lower()
    assert "EXISTS and is EMPTY" not in out


def test_a_file_with_content_is_unaffected(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    (tmp_path / "real.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    out = context.get_file_content("real.py")

    assert "x = 1" in out
    assert "EMPTY" not in out


def test_a_whitespace_only_file_is_not_treated_as_empty(tmp_path, monkeypatch):
    """It has bytes, so it is not the 0-byte case — it should render as content
    (visibly blank lines), not as the empty-file notice."""
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    (tmp_path / "blank.py").write_text("\n\n\n", encoding="utf-8")

    out = context.get_file_content("blank.py")

    assert "EXISTS and is EMPTY" not in out
