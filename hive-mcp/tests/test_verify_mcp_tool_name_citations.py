"""Regression test: verify_claims must not check a backticked hive-mcp tool name
against the TARGET project's repo as if it were a code-symbol existence claim.

Confirmed live 2026-08-14: an answer wrote "verified using `search_files_batch`"
-- naming which real hive-mcp tool it called (hive-mcp/tools/context.py:401,
registered in main.py). `search_files_batch` has no reason to appear anywhere in
EkamApp's own source -- it is hive-mcp's tool, not the project's. verify_claims
grepped the project repo for the literal string, found nothing, and reported it
NOT FOUND / fabrication, even though the tool name is completely real and the
model's self-citation of its own methodology was accurate.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_tracking():
    verify._last_checked_answer = None
    verify._repeat_count = 0


def test_known_hive_mcp_tool_name_backtick_is_excluded_like_noise(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "Verified using `search_files_batch`."

    report = verify.verify_claims(answer)

    assert "no checkable claims found" in report


def test_multiple_known_tool_names_are_all_excluded(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "Used `get_file_content` then `apply_diff` to make the change."

    report = verify.verify_claims(answer)

    assert "no checkable claims found" in report


def test_tool_name_exclusion_does_not_swallow_a_real_adjacent_claim(monkeypatch):
    """The exclusion is per-token, not per-answer -- a genuine fabricated symbol
    named alongside a real tool name must still be caught."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "Used `get_file_content` to confirm `totallyMadeUpSymbol` exists."

    report = verify.verify_claims(answer)

    assert "get_file_content" not in report
    assert "NOT FOUND" in report
    assert "totallyMadeUpSymbol" in report


def test_non_tool_name_symbol_is_still_checked_normally(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "The function `doTheThing` handles this."

    report = verify.verify_claims(answer)

    assert "NOT FOUND" in report
    assert "doTheThing" in report
