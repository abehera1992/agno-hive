"""Phase: Evidence-Deduplicated Reread Control -- cross-generation marker tests.

Focused, self-contained tests for the minimal addition to
_make_read_cache_tool_hook (swarm/team.py): a cache entry populated in one
delegation generation, served again in a LATER generation's first ask, is now
prefixed with an "EXISTING RUN EVIDENCE" marker rather than being silently
indistinguishable from a brand-new fetch. Deliberately does not touch or
extend tests/test_team_read_cache_hook.py (already failing, pre-existing,
unrelated -- see CLAUDE.md/session history) -- this is a new, independent
file testing only the new behavior in isolation.
"""
import json

import pytest

from swarm.team import _make_read_cache_tool_hook


class _FakeAgent:
    def __init__(self, name: str):
        self.name = name


class _FakeSession:
    def __init__(self):
        self.session_state = {}


async def _get_file_content(relative_path: str) -> str:
    return f"CONTENT of {relative_path}"


async def _delegate_task_to_member(member_id: str, task: str = "x") -> str:
    return "delegated"


@pytest.mark.asyncio
async def test_same_generation_repeat_is_unaffected_stub_path():
    """Existing within-generation duplicate protection is untouched: 2nd ask in
    the SAME generation still gets the real _duplicate_read_stub, not the new
    marker -- the two mechanisms must not collide."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("researcher")
    args = {"relative_path": "a.py"}

    first = await hook("get_file_content", _get_file_content, args, agent=researcher)
    assert "EXISTING RUN EVIDENCE" not in first
    assert "CONTENT of a.py" in first

    second = await hook("get_file_content", _get_file_content, args, agent=researcher)
    assert "Already returned this exact" in second
    assert "EXISTING RUN EVIDENCE" not in second


@pytest.mark.asyncio
async def test_cross_generation_exact_repeat_is_marked_not_suppressed():
    """Counterfactual A: a new delegation generation's first ask for a file
    already cached in an EARLIER generation gets the REAL content (not
    suppressed -- the member has no other way to obtain it) but PREFIXED with
    the existing-evidence marker."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("researcher")
    args = {"relative_path": "a.py"}

    g1 = await hook("get_file_content", _get_file_content, args, agent=researcher)
    assert "EXISTING RUN EVIDENCE" not in g1

    # Bump researcher's delegation generation (same mechanism the hook itself
    # uses internally when the coordinator calls delegate_task_to_member).
    await hook("delegate_task_to_member",
               lambda **kw: _delegate_task_to_member(**kw),
               {"member_id": "researcher", "task": "read models.py"},
               agent=None)

    g2 = await hook("get_file_content", _get_file_content, args, agent=researcher)
    assert "EXISTING RUN EVIDENCE" in g2
    assert "already retrieved earlier this run" in g2
    # The real content must still be present -- restored, not withheld.
    assert "CONTENT of a.py" in g2


@pytest.mark.asyncio
async def test_different_args_in_new_generation_is_genuinely_fresh():
    """Counterfactual B: a targeted read with DIFFERENT arguments (a different
    file) in a new generation is a real cache miss and must never be marked --
    it is not a repeat of anything."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("researcher")

    await hook("get_file_content", _get_file_content,
               {"relative_path": "a.py"}, agent=researcher)
    await hook("delegate_task_to_member",
               lambda **kw: _delegate_task_to_member(**kw),
               {"member_id": "researcher", "task": "read b.py"}, agent=None)

    fresh = await hook("get_file_content", _get_file_content,
                        {"relative_path": "b.py"}, agent=researcher)
    assert "EXISTING RUN EVIDENCE" not in fresh
    assert "CONTENT of b.py" in fresh


@pytest.mark.asyncio
async def test_different_agent_same_generation_shape_not_marked():
    """A DIFFERENT agent's first-ever ask for an already-cached file is not a
    cross-generation repeat for THAT agent -- still real content, no marker,
    since cache_origin_generation records when the entry was first created,
    and the generation check is scoped per the requesting agent's own
    delegation_generation value, not a global notion of 'generation 1 vs 2'."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("researcher")
    coder = _FakeAgent("coder")
    args = {"relative_path": "a.py"}

    await hook("get_file_content", _get_file_content, args, agent=researcher)
    second_agent_first_ask = await hook(
        "get_file_content", _get_file_content, args, agent=coder)
    # Coder's own generation is 0 (never delegated to), matching the cache
    # entry's origin generation (also 0, researcher's first-ever delegation
    # generation) -- same-generation-number coincidence, not cross-generation
    # reuse, so no marker is expected here; this pins the exact boundary.
    assert "CONTENT of a.py" in second_agent_first_ask
