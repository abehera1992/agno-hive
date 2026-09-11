"""Experiment 4 (H7): the providesTags/tagTypes invariant injection. Mechanism only.

These assert that the mechanism does exactly one thing and touches nothing else.
They say nothing about RTK Query correctness, TypeScript, apply_diff, newline
escaping, ContextPack, routing or the Reviewer -- those are separate hypotheses and
testing them here would blur what this experiment measures.

Driven through the REAL interception hook with a real agent-shaped caller and the
production predicate, never a re-implementation of it: a test that duplicated the
Coder predicate could pass while production targeted the wrong member. Two fixes
earlier in this series shipped green-in-tests and inert in production for exactly
that kind of convenience.

The flag-off case is asserted hardest. The control arm of the battery IS production,
so if a single character leaks in with the flag unset, the comparison is void.
"""
import asyncio

import pytest

from swarm.team import (_TAG_INVARIANT_TEXT, _make_tool_interception_hook,
                        _tag_invariant_enabled)

FROZEN = (
    "When adding an RTK Query endpoint that introduces a new literal tag in "
    "providesTags, verify that the same tag is declared in the API's existing "
    "tagTypes. Treat the endpoint and its tag configuration as one coupled change."
)


class _Agent:
    def __init__(self, name="Coordinator"):
        self.name = name


def _call(hook, member, task, sentinel=None):
    """Drive the real hook and return (mutated args, what the inner fn received)."""
    seen = {}

    async def fake_delegate(**kwargs):
        seen.update(kwargs)
        return sentinel if sentinel is not None else "ok"

    args = {"member_id": member, "task": task}
    out = asyncio.run(hook("delegate_task_to_member", fake_delegate, args,
                           agent=_Agent(), team=None))
    return args, seen, out


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("TAGINVARIANT_ENABLED", "1")


@pytest.fixture(autouse=True)
def off_by_default(monkeypatch):
    monkeypatch.delenv("TAGINVARIANT_ENABLED", raising=False)


# ── the treatment text itself ───────────────────────────────────────────────────────

def test_frozen_text_is_byte_exact():
    """Immutable for the whole battery. A reworded treatment is a different
    experiment, so this pins the string rather than merely checking it is non-empty."""
    assert _TAG_INVARIANT_TEXT == FROZEN


def test_treatment_carries_no_self_inspection_or_tooling_guidance():
    """The pre-registered design excluded these explicitly; each would test a
    different hypothesis (self-review, syntax, tooling, style-mirroring)."""
    low = _TAG_INVARIANT_TEXT.lower()
    for banned in ("before finishing", "inspect", "run the compiler", "check your work",
                   "make sure the file parses", "double quotes", "apply_diff",
                   "typescript", "\\n", "existing pattern", "review"):
        assert banned not in low, banned
    assert _TAG_INVARIANT_TEXT.count(".") == 2, "exactly two sentences"


# ── control inertness ───────────────────────────────────────────────────────────────

def test_flag_defaults_to_off():
    assert _tag_invariant_enabled() is False


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_only_recognised_truthy_values_enable_it(monkeypatch, value):
    monkeypatch.setenv("TAGINVARIANT_ENABLED", value)
    assert _tag_invariant_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_recognised_truthy_values(monkeypatch, value):
    monkeypatch.setenv("TAGINVARIANT_ENABLED", value)
    assert _tag_invariant_enabled() is True


def test_flag_off_leaves_a_coder_delegation_byte_identical():
    original = "Implement getPayments in inventoryApi.ts."
    args, seen, _ = _call(_make_tool_interception_hook(), "coder", original)
    assert args["task"] == original
    assert seen["task"] == original


# ── targeting ───────────────────────────────────────────────────────────────────────

def test_injected_into_a_coder_delegation(on):
    args, seen, _ = _call(_make_tool_interception_hook(), "coder", "do the thing")
    assert args["task"] == f"do the thing\n\n{FROZEN}"
    # the forwarded call must carry it too -- injection happens before the real call
    assert FROZEN in seen["task"]


def test_coordinator_framing_is_preserved_not_replaced(on):
    args, _, _ = _call(_make_tool_interception_hook(), "coder", "ORIGINAL TEXT")
    assert args["task"].startswith("ORIGINAL TEXT")


@pytest.mark.parametrize("member", ["researcher", "reviewer", "executor",
                                    "contextrouter", "planner"])
def test_other_members_are_untouched(on, member):
    args, _, _ = _call(_make_tool_interception_hook(), member, "do the thing")
    assert args["task"] == "do the thing"


def test_other_tool_calls_are_untouched(on):
    """Only delegate_task_to_member is eligible; a read must not be rewritten."""
    hook = _make_tool_interception_hook()
    seen = {}

    async def fake_read(**kwargs):
        seen.update(kwargs)
        return "bytes"

    args = {"relative_path": "a.ts"}
    asyncio.run(hook("get_file_content", fake_read, args, agent=_Agent()))
    assert args == {"relative_path": "a.ts"}
    assert "task" not in seen


def test_plural_broadcast_delegation_is_untouched(on):
    """delegate_task_to_members (plural) is a different function name and carries no
    member_id; the predicate must not catch it."""
    hook = _make_tool_interception_hook()

    async def fake(**kwargs):
        return "ok"

    args = {"task": "review everything"}
    asyncio.run(hook("delegate_task_to_members", fake, args, agent=_Agent()))
    assert args["task"] == "review everything"


# ── idempotence and transparency ────────────────────────────────────────────────────

def test_not_appended_twice_on_a_redelegation(on):
    hook = _make_tool_interception_hook()
    args, _, _ = _call(hook, "coder", "do the thing")
    once = args["task"]
    args2, _, _ = _call(hook, "coder", once)
    assert args2["task"].count(FROZEN) == 1


def test_hook_returns_the_inner_result_object_unchanged(on):
    """An observer that altered the return value would be a treatment on the tool
    path, not on the delegation text."""
    sentinel = object()
    _, _, out = _call(_make_tool_interception_hook(), "coder", "x", sentinel=sentinel)
    assert out is sentinel


def test_marker_is_treatment_only(on, capsys):
    hook = _make_tool_interception_hook()
    _call(hook, "coder", "do the thing")
    assert "[taginvariant] injected" in capsys.readouterr().out


def test_no_marker_when_flag_is_off(capsys):
    _call(_make_tool_interception_hook(), "coder", "do the thing")
    assert "[taginvariant]" not in capsys.readouterr().out


def test_no_marker_for_a_non_coder_member(on, capsys):
    _call(_make_tool_interception_hook(), "reviewer", "do the thing")
    assert "[taginvariant]" not in capsys.readouterr().out
