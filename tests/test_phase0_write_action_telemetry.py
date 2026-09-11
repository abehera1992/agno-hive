"""Phase 0 write-action telemetry (2026-09-11).

These exist to validate ONE thing: that the record can separate "the Coder never
emitted a write call" from "the Coder emitted one and hive removed it". If that
distinction is not measurable, the observation battery cannot answer its question.

Written against PRODUCTION SHAPES on purpose. Two fixes in this series were green in
tests and inert in production, both because the test used a convenient shape: one
returned a bare str where production returns agno's ToolResult wrapper, the other
passed agent=None where production passes a real agent. So the hook tests below drive
the REAL interception hook with a real agent and a real ToolResult, and the sanitizer
test drives the REAL _sanitize_forced_text rather than re-implementing its regex.

The flag-off / no-activity cases are asserted too: an observer that invents activity
where there was none would corrupt exactly the runs this battery is meant to explain.
"""
import pytest

from agno.tools.function import ToolResult

from swarm import phase0
from swarm.team import _make_tool_interception_hook

APPLY = "apply_diff"
PATH = "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts"


class _Agent:
    def __init__(self, name="Coder"):
        self.name = name


@pytest.fixture(autouse=True)
def _clean():
    phase0._reset_actions()
    yield
    phase0._reset_actions()


# ── accounting primitives ───────────────────────────────────────────────────────────

def test_write_outcomes_are_decided_on_positive_evidence_not_exclusion():
    """Success is "review_pending:" and nothing else. The first battery produced 4
    distinct apply_diff result shapes; treating "not a failure" as success recorded a
    guard refusal as an execution while nothing was staged."""
    phase0.note_tool_call("coder", APPLY, f"review_pending: {PATH}")
    phase0.note_tool_call("coder", APPLY, f"apply_diff failed: old_string not found in {PATH}")
    phase0.note_tool_call("coder", APPLY, f"File not found: {PATH}")
    phase0.note_tool_call("coder", APPLY,
                          "apply_diff STOPPED: this exact old_string/new_string was just retried")
    s = phase0.action_snapshot("coder")
    assert s["write_tool_calls_reached_hook"] == 4
    assert s["write_tool_calls_executed"] == 1, "only the staging receipt counts"
    assert s["write_tool_calls_failed"] == 2, "tool failures incl. File not found"
    assert s["write_tool_calls_blocked"] == 1, "a hive guard refusal is its own bucket"
    assert s["tool_calls_made"] == 4


def test_reads_count_as_tool_calls_but_never_as_writes():
    for _ in range(5):
        phase0.note_tool_call("coder", "get_file_content", "…file bytes…")
    s = phase0.action_snapshot("coder")
    assert s["tool_calls_made"] == 5
    assert s["write_tool_calls_reached_hook"] == 0


def test_members_are_counted_separately():
    phase0.note_tool_call("coder", APPLY, "review_pending: x")
    phase0.note_tool_call("researcher", "search_files", "hits")
    assert phase0.action_snapshot("coder")["write_tool_calls_reached_hook"] == 1
    assert phase0.action_snapshot("researcher")["write_tool_calls_reached_hook"] == 0


def test_reset_isolates_runs():
    phase0.note_tool_call("coder", APPLY, "review_pending: x")
    phase0._reset_actions()
    assert phase0.action_snapshot("coder")["tool_calls_made"] == 0


# ── the distinction the battery depends on ──────────────────────────────────────────

def test_no_write_call_is_recorded_as_zero_not_as_missing_data():
    """Category A must be positively observable: a Coder that read and finished with
    prose leaves tool calls > 0 and write calls == 0, not an absent record."""
    for _ in range(10):
        phase0.note_tool_call("coder", "get_file_content", "bytes")
    s = phase0.action_snapshot("coder")
    assert s["tool_calls_made"] == 10 and s["write_tool_calls_reached_hook"] == 0
    assert s["tool_choice_escalations"] == 0
    assert phase0.discard_snapshot()["content_discarded_chars"] == 0


def test_tool_choice_escalation_is_recorded_against_the_member():
    phase0.note_tool_choice_forced("coder", raw="tool_choice=none")
    s = phase0.action_snapshot("coder")
    assert s["tool_choice_escalations"] == 1
    assert "tool_choice=none" in s["tool_choice_raw"]


def test_discarded_write_call_is_distinguishable_from_discarded_prose():
    """The whole point of category B/C: a strip that destroyed a write call must be
    separable from a strip that destroyed ordinary text."""
    phase0.note_discarded_content(
        chars=80, reason="forced_text_only_tag_strip",
        text='<tool_call>{"name": "apply_diff", "arguments": {}}</tool_call>')
    phase0.note_discarded_content(chars=20, reason="forced_text_only_tag_strip",
                                  text="<tool_call>{\"name\": \"search_files\"}</tool_call>")
    d = phase0.discard_snapshot()
    assert d["content_discarded_chars"] == 100
    assert d["discarded_write_calls"] == 1
    assert d["discard_reasons"] == ["forced_text_only_tag_strip"]


# ── claimed completion: deterministic, under-reporting by design ────────────────────

@pytest.mark.parametrize("text,writes,expected", [
    ("The `getPayments` query endpoint has been added to inventoryApi.ts.", 0, True),
    ("The endpoint has been added.", 1, False),          # it really did write
    ("I will add the getPayments endpoint next.", 0, False),   # a plan, not a claim
    ("getPayments", 0, False),                            # a bare symbol name
    ("", 0, None),                                        # no content => unknown
])
def test_claimed_completion_is_literal_and_conservative(text, writes, expected):
    assert phase0.claimed_completion(text, writes) is expected


# ── production shapes: the real hook, a real agent, a real ToolResult ───────────────

@pytest.mark.asyncio
async def test_real_interception_hook_records_a_wrapped_failed_write():
    """Production delivers agno's ToolResult, whose str() is a pydantic repr. A guard
    reading str() instead of unwrapping is how Experiment 1's fix shipped inert."""
    hook = _make_tool_interception_hook()

    async def failing_apply_diff(**kw):
        return ToolResult(content=f"apply_diff failed: old_string not found in {PATH}")

    await hook(APPLY, failing_apply_diff,
               {"relative_path": PATH, "old_string": "a", "new_string": "b"},
               agent=_Agent("Coder"))

    s = phase0.action_snapshot("coder")
    assert s["write_tool_calls_reached_hook"] == 1, "write never reached the observer"
    assert s["write_tool_calls_failed"] == 1
    assert s["write_tool_calls_executed"] == 0


@pytest.mark.asyncio
async def test_real_interception_hook_records_a_wrapped_successful_write():
    hook = _make_tool_interception_hook()

    async def ok_apply_diff(**kw):
        return ToolResult(content=f"review_pending: {PATH}")

    await hook(APPLY, ok_apply_diff,
               {"relative_path": PATH, "old_string": "a", "new_string": "b"},
               agent=_Agent("Coder"))

    s = phase0.action_snapshot("coder")
    assert s["write_tool_calls_executed"] == 1
    assert s["write_tool_calls_failed"] == 0
    assert "apply_diff" in s["write_tools_seen"]


@pytest.mark.asyncio
async def test_hook_observer_does_not_alter_the_tool_result():
    """An observer that changed a return value would be a treatment, not telemetry."""
    hook = _make_tool_interception_hook()
    sentinel = ToolResult(content=f"review_pending: {PATH}")

    async def ok(**kw):
        return sentinel

    got = await hook(APPLY, ok, {"relative_path": PATH}, agent=_Agent("Coder"))
    assert got is sentinel


@pytest.mark.asyncio
async def test_coordinator_calls_arrive_with_no_agent_and_are_still_counted():
    """agno passes function._agent, which is unset for Team-owned functions, so the
    coordinator's own calls arrive as agent=None."""
    hook = _make_tool_interception_hook()

    async def ok(**kw):
        return ToolResult(content="fine")

    await hook("search_files", ok, {"pattern": "x"}, agent=None)
    assert phase0.action_snapshot("coordinator")["tool_calls_made"] == 1


# ── the real sanitizer is the only place an emitted write is visibly destroyed ──────

def test_real_sanitizer_records_the_write_call_it_strips():
    from swarm.tool_fix import _ToolCallRecoveryMixin

    class _M(_ToolCallRecoveryMixin):
        _tool_choice = "none"          # what _sanitize_forced_text gates on

    class _Resp:
        content = ('Here is my plan.\n<tool_call>\n{"name": "apply_diff", '
                   '"arguments": {"relative_path": "x.ts"}}</tool_call>')

    resp = _Resp()
    handled = _M()._sanitize_forced_text(resp)

    assert handled is True
    assert "<tool_call>" not in resp.content, "sanitizer behaviour must be unchanged"
    d = phase0.discard_snapshot()
    assert d["content_discarded_chars"] > 0
    assert d["discarded_write_calls"] == 1, "a destroyed apply_diff must be visible"
    assert d["events"][0]["write_tool"] == "apply_diff"


def test_sanitizer_records_nothing_when_it_does_not_fire():
    """Not forced, so nothing is stripped and nothing may be recorded."""
    from swarm.tool_fix import _ToolCallRecoveryMixin

    class _M(_ToolCallRecoveryMixin):
        _tool_choice = None

    class _Resp:
        content = '<tool_call>{"name": "apply_diff"}</tool_call>'

    assert _M()._sanitize_forced_text(_Resp()) is False
    assert phase0.discard_snapshot()["content_discarded_chars"] == 0
