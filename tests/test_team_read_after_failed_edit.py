"""A read that follows a FAILED apply_diff on the same file must return fresh content.

Demonstrated live, Phase-2 control v2, I3 (2026-09-10). The Coder read the whole target
file, built an apply_diff anchor, and the anchor did not match. It then did exactly what
the Coder instructions ask for -- re-read the file to get exact text -- and the
duplicate-read cache answered with a 347-character stub instead:

    14:58:34  get_file_content  35,624  (whole file)
    14:59:07  apply_diff           634  apply_diff failed: old_string not found
    14:59:09  get_file_content     347  "Already returned this exact ... unchanged"
    14:59:31  apply_diff           635  apply_diff failed: old_string not found
    14:59:34  get_file_content     374  "Already returned this exact ..."
    15:00:13  get_file_content     622  FORCED STOP

The stub's own wording is the contract it breaks: "the result has not changed and will
not change". After apply_diff stages a .hive_proposed, hive-mcp serves the STAGED file
for that path, so the result demonstrably can change -- and the one moment the model
most needs the current bytes is immediately after an anchor failed against them.

The cache is otherwise doing its job and is left alone: an ordinary repeated read, with
no failed edit in between, must still be stubbed. Both directions are asserted here,
because the value of the exemption depends entirely on how narrow it is.
"""
import pytest

from swarm.team import _make_read_cache_tool_hook

PATH = "API/inventory-service/router/vouchers_api.py"
OTHER = "API/inventory-service/models.py"


def _reader(box):
    async def fake_get_file_content(**kwargs):
        box.append(kwargs)
        return f"content #{len(box)} of {kwargs['relative_path']}"
    return fake_get_file_content


def _failing_apply_diff():
    async def fake(**kwargs):
        return ("apply_diff failed: old_string not found in "
                f"{kwargs.get('relative_path')}")
    return fake


def _succeeding_apply_diff():
    async def fake(**kwargs):
        return f"review_pending: {kwargs.get('relative_path')}"
    return fake


@pytest.mark.asyncio
async def test_read_after_failed_apply_diff_returns_fresh_content():
    """The demonstrated I3 sequence: read -> failed edit -> read again."""
    hook = _make_read_cache_tool_hook()
    calls = []

    first = await hook("get_file_content", _reader(calls), {"relative_path": PATH})
    await hook("apply_diff", _failing_apply_diff(),
               {"relative_path": PATH, "old_string": "nope", "new_string": "x"})
    second = await hook("get_file_content", _reader(calls), {"relative_path": PATH})

    assert first == "content #1 of " + PATH
    # Re-fetched, not stubbed: the underlying function ran a second time.
    assert len(calls) == 2, f"recovery read was not executed: {calls}"
    assert "Already returned this exact" not in second
    assert second == "content #2 of " + PATH


@pytest.mark.asyncio
async def test_ordinary_repeated_read_is_still_suppressed():
    """The negative case. Without a failed edit, duplicate suppression is unchanged."""
    hook = _make_read_cache_tool_hook()
    calls = []

    await hook("get_file_content", _reader(calls), {"relative_path": PATH})
    second = await hook("get_file_content", _reader(calls), {"relative_path": PATH})

    assert len(calls) == 1, "an ordinary repeat must not re-fetch"
    assert "Already returned this exact" in second


@pytest.mark.asyncio
async def test_failed_edit_on_one_file_does_not_unlock_another():
    """The exemption is per-path. A failed edit on PATH says nothing about OTHER."""
    hook = _make_read_cache_tool_hook()
    calls = []

    await hook("get_file_content", _reader(calls), {"relative_path": OTHER})
    await hook("apply_diff", _failing_apply_diff(),
               {"relative_path": PATH, "old_string": "nope", "new_string": "x"})
    second = await hook("get_file_content", _reader(calls), {"relative_path": OTHER})

    assert len(calls) == 1, "an unrelated file must stay suppressed"
    assert "Already returned this exact" in second


@pytest.mark.asyncio
async def test_exemption_is_consumed_once():
    """One failed edit buys one fresh read, not a permanently open door."""
    hook = _make_read_cache_tool_hook()
    calls = []

    await hook("get_file_content", _reader(calls), {"relative_path": PATH})
    await hook("apply_diff", _failing_apply_diff(),
               {"relative_path": PATH, "old_string": "nope", "new_string": "x"})
    await hook("get_file_content", _reader(calls), {"relative_path": PATH})   # allowed
    third = await hook("get_file_content", _reader(calls), {"relative_path": PATH})

    assert len(calls) == 2, "the exemption must not persist past one read"
    assert "Already returned this exact" in third


@pytest.mark.asyncio
async def test_successful_apply_diff_does_not_grant_the_exemption():
    """Scope guard. This change is about FAILED edits only.

    A successful apply_diff also changes what the path serves, and I3's fourth call
    hit ambiguity created by its own successful third. That is a real and separate
    question; deliberately NOT addressed here, and asserted so the scope cannot drift
    without a test failing.
    """
    hook = _make_read_cache_tool_hook()
    calls = []

    await hook("get_file_content", _reader(calls), {"relative_path": PATH})
    await hook("apply_diff", _succeeding_apply_diff(),
               {"relative_path": PATH, "old_string": "a", "new_string": "b"})
    second = await hook("get_file_content", _reader(calls), {"relative_path": PATH})

    assert len(calls) == 1
    assert "Already returned this exact" in second
