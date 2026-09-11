"""Experiment 5 Phase 3: mechanical verification integrated into the Coder ->
Reviewer workflow.

Driven through the REAL interception hook (_make_tool_interception_hook /
_tool_interception_hook), the same discipline test_team_tag_invariant.py and
test_phase0_write_action_telemetry.py already established for this file -- a test
that re-implemented the Coder/apply_diff/review_pending predicate could pass while
production targeted the wrong call. `_call_verify_project` (the one function that
actually reaches hive-mcp over MCP) is monkeypatched at the swarm.team module
level; everything else -- the hook, the gating predicate, the repair-counter dict,
the result rewriting, the telemetry call -- is real production code.

The flag-off case is asserted hardest, same reasoning as Experiment 4: the control
arm of any future comparison IS production, so a single leaked character with the
flag unset voids it.
"""
import asyncio

import pytest
from agno.tools.function import ToolResult

from swarm import phase0
from swarm.phase0 import Phase0Run
from swarm.team import (_MECHVERIFY_MAX_REPAIRS, _make_tool_interception_hook,
                        _mechverify_enabled)

APPLY = "apply_diff"
PATH = "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts"


class _Agent:
    def __init__(self, name="Coder"):
        self.name = name


class _Team:
    """Stand-in for the real agno Team object. Production sets _hive_mcp_url and
    _phase0 on the team AFTER _build_team() returns (the established late-binding
    pattern) -- this mirrors exactly that shape, nothing more."""

    def __init__(self, hive_mcp_url="http://fake-hive-mcp/mcp", phase0_run=None):
        self._hive_mcp_url = hive_mcp_url
        self._phase0 = phase0_run


def _vr(status, checks=None, verified_targets=None):
    return {
        "status": status,
        "checks": checks or [],
        "verified_targets": verified_targets or [],
        "rejected_targets": [],
    }


def _check(id_, status, exit_code=None, diagnostics=None, raw_output="", reason=""):
    return {
        "id": id_, "status": status, "exit_code": exit_code,
        "diagnostics": diagnostics or [], "raw_output": raw_output, "reason": reason,
    }


def _diag(file, message, line=None, column=None, code=None, severity="error"):
    return {"file": file, "severity": severity, "message": message,
            "line": line, "column": column, "code": code}


@pytest.fixture(autouse=True)
def _clean_phase0_actions():
    phase0._reset_actions()
    yield
    phase0._reset_actions()


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("MECHVERIFY_ENABLED", "1")


@pytest.fixture(autouse=True)
def off_by_default(monkeypatch):
    monkeypatch.delenv("MECHVERIFY_ENABLED", raising=False)


def _mock_verify(monkeypatch, result_or_sequence):
    """Replaces _call_verify_project with a stub returning the given dict, or the
    next dict off a list for successive calls (repair-loop tests)."""
    calls = []
    if isinstance(result_or_sequence, list):
        seq = list(result_or_sequence)

        async def fake(hive_mcp_url, checks, targets):
            calls.append({"hive_mcp_url": hive_mcp_url, "checks": list(checks),
                         "targets": list(targets)})
            return seq.pop(0) if seq else None
    else:
        async def fake(hive_mcp_url, checks, targets):
            calls.append({"hive_mcp_url": hive_mcp_url, "checks": list(checks),
                         "targets": list(targets)})
            return result_or_sequence

    monkeypatch.setattr("swarm.team._call_verify_project", fake)
    return calls


async def _apply_diff_ok(**kw):
    return ToolResult(content=f"review_pending: {PATH}")


# ── 1. Enablement ────────────────────────────────────────────────────────────────

def test_flag_defaults_to_off():
    assert _mechverify_enabled() is False


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_only_recognised_truthy_values_enable_it(monkeypatch, value):
    monkeypatch.setenv("MECHVERIFY_ENABLED", value)
    assert _mechverify_enabled() is False


@pytest.mark.asyncio
async def test_flag_off_leaves_the_result_byte_identical(monkeypatch):
    """No verify_project call at all when the flag is unset -- not merely a PASS-shaped
    no-op. A monkeypatch that raises proves the gate short-circuits before ever calling
    _call_verify_project."""
    async def boom(*a, **kw):
        raise AssertionError("_call_verify_project must not be called with the flag off")
    monkeypatch.setattr("swarm.team._call_verify_project", boom)

    hook = _make_tool_interception_hook()
    sentinel = ToolResult(content=f"review_pending: {PATH}")

    async def ok(**kw):
        return sentinel

    out = await hook(APPLY, ok, {"relative_path": PATH}, agent=_Agent("Coder"),
                     team=_Team())
    assert out is sentinel


# ── 2. Coder gating (apply_diff + Coder-only + review_pending) ─────────────────────

@pytest.mark.asyncio
async def test_gate_fires_for_a_successful_coder_apply_diff(on, monkeypatch):
    _mock_verify(monkeypatch, _vr("PASS", [_check("typecheck", "PASS", exit_code=0)]))
    hook = _make_tool_interception_hook()
    out = await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
                     team=_Team())
    assert isinstance(out, ToolResult)
    assert out.content == f"review_pending: {PATH}"  # PASS => untouched


@pytest.mark.parametrize("member", ["researcher", "reviewer", "executor", "planner"])
@pytest.mark.asyncio
async def test_other_members_are_never_gated(on, monkeypatch, member):
    calls = _mock_verify(monkeypatch, _vr("FAIL"))
    hook = _make_tool_interception_hook()
    out = await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent(member),
                     team=_Team())
    assert calls == [], "verify_project must not run for a non-Coder apply_diff"
    assert out.content == f"review_pending: {PATH}"


@pytest.mark.asyncio
async def test_other_tool_calls_are_never_gated(on, monkeypatch):
    """Only apply_diff is eligible; a read must not trigger verification."""
    calls = _mock_verify(monkeypatch, _vr("FAIL"))
    hook = _make_tool_interception_hook()

    async def read(**kw):
        return ToolResult(content="…file bytes…")

    await hook("get_file_content", read, {"relative_path": PATH}, agent=_Agent("Coder"),
              team=_Team())
    assert calls == []


@pytest.mark.asyncio
async def test_a_failed_apply_diff_is_never_gated(on, monkeypatch):
    """No review_pending in the result => nothing was staged => nothing to verify."""
    calls = _mock_verify(monkeypatch, _vr("FAIL"))
    hook = _make_tool_interception_hook()

    async def failing(**kw):
        return ToolResult(content=f"apply_diff failed: old_string not found in {PATH}")

    out = await hook(APPLY, failing, {"relative_path": PATH}, agent=_Agent("Coder"),
                     team=_Team())
    assert calls == []
    assert "apply_diff failed" in out.content


# ── 3. Verification outcomes ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pass_preserves_the_original_result_object_identity(on, monkeypatch):
    """PASS must not fabricate a new result -- the existing Reviewer flow reads
    exactly what apply_diff itself returned."""
    _mock_verify(monkeypatch, _vr("PASS", [_check("typecheck", "PASS")]))
    hook = _make_tool_interception_hook()
    sentinel = ToolResult(content=f"review_pending: {PATH}")

    async def ok(**kw):
        return sentinel

    out = await hook(APPLY, ok, {"relative_path": PATH}, agent=_Agent("Coder"), team=_Team())
    assert out is sentinel


@pytest.mark.asyncio
async def test_fail_rewrites_the_result_into_a_repair_request(on, monkeypatch):
    diag = _diag(PATH, "Type 'string' is not assignable to type 'number'.", line=42, column=7,
                code="TS2322")
    _mock_verify(monkeypatch, _vr("FAIL", [_check("typecheck", "FAIL", exit_code=2,
                                                  diagnostics=[diag])]))
    hook = _make_tool_interception_hook()
    out = await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
                     team=_Team())
    assert isinstance(out, str)
    assert "review_pending" not in out
    assert PATH in out
    assert "42" in out and "TS2322" in out
    assert "not assignable" in out


@pytest.mark.asyncio
async def test_error_status_passes_the_original_result_through_unchanged(on, monkeypatch):
    """Infrastructure/config failure -- not a code defect, never treated as one."""
    _mock_verify(monkeypatch, _vr("ERROR", [_check("typecheck", "ERROR",
                                                    reason="manifest command not found")]))
    hook = _make_tool_interception_hook()
    sentinel = ToolResult(content=f"review_pending: {PATH}")

    async def ok(**kw):
        return sentinel

    out = await hook(APPLY, ok, {"relative_path": PATH}, agent=_Agent("Coder"), team=_Team())
    assert out is sentinel


@pytest.mark.asyncio
async def test_unsupported_status_passes_the_original_result_through_unchanged(on, monkeypatch):
    """No manifest / no declared checks -- must not be silently reported as success,
    and must not be treated as a code failure either. The Coder's own apply_diff
    result reaches the Reviewer exactly as it would with mechverify off."""
    _mock_verify(monkeypatch, _vr("UNSUPPORTED", [_check("typecheck", "UNSUPPORTED",
                                                          reason="no .hive-verify.json")]))
    hook = _make_tool_interception_hook()
    sentinel = ToolResult(content=f"review_pending: {PATH}")

    async def ok(**kw):
        return sentinel

    out = await hook(APPLY, ok, {"relative_path": PATH}, agent=_Agent("Coder"), team=_Team())
    assert out is sentinel


# ── 4. Repair loop (hard limit, server-side state) ──────────────────────────────────

@pytest.mark.asyncio
async def test_repair_limit_is_two_by_construction():
    assert _MECHVERIFY_MAX_REPAIRS == 2


@pytest.mark.asyncio
async def test_second_consecutive_fail_on_the_same_target_produces_a_second_repair_request(
        on, monkeypatch):
    diag = _diag(PATH, "boom", line=1)
    _mock_verify(monkeypatch, [
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
    ])
    hook = _make_tool_interception_hook()   # ONE hook instance -> ONE repair-counter dict
    team = _Team()

    out1 = await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
                      team=team)
    assert "repair attempt 1/2" in out1

    out2 = await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
                      team=team)
    assert "repair attempt 2/2" in out2
    assert "final repair attempt" in out2


@pytest.mark.asyncio
async def test_third_consecutive_fail_stops_repair_and_surfaces_the_original_result(
        on, monkeypatch):
    diag = _diag(PATH, "boom", line=1)
    _mock_verify(monkeypatch, [
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
    ])
    hook = _make_tool_interception_hook()
    team = _Team()

    sentinel = ToolResult(content=f"review_pending: {PATH}")

    async def ok(**kw):
        return sentinel

    await hook(APPLY, ok, {"relative_path": PATH}, agent=_Agent("Coder"), team=team)
    await hook(APPLY, ok, {"relative_path": PATH}, agent=_Agent("Coder"), team=team)
    out3 = await hook(APPLY, ok, {"relative_path": PATH}, agent=_Agent("Coder"), team=team)

    assert out3 is sentinel, ("after the limit, the ORIGINAL (failing) result must pass "
                              "through -- never a 3rd repair request, never a fabricated "
                              "success")


@pytest.mark.asyncio
async def test_a_pass_after_a_fail_resets_the_counter_for_that_target(on, monkeypatch):
    diag = _diag(PATH, "boom", line=1)
    _mock_verify(monkeypatch, [
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
        _vr("PASS", [_check("typecheck", "PASS")]),
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
    ])
    hook = _make_tool_interception_hook()
    team = _Team()

    out1 = await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
                      team=team)
    assert "repair attempt 1/2" in out1

    sentinel = ToolResult(content=f"review_pending: {PATH}")

    async def ok(**kw):
        return sentinel

    out2 = await hook(APPLY, ok, {"relative_path": PATH}, agent=_Agent("Coder"), team=team)
    assert out2 is sentinel  # PASS

    out3 = await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
                      team=team)
    assert "repair attempt 1/2" in out3, "counter must restart at 1, not continue at 3"


@pytest.mark.asyncio
async def test_repair_counter_is_scoped_per_target_not_global(on, monkeypatch):
    """A repair-limited target must not block verification on a different target."""
    other_path = "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/other.ts"
    diag = _diag(PATH, "boom", line=1)
    _mock_verify(monkeypatch, [
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),  # PATH exhausts limit
        _vr("PASS", [_check("typecheck", "PASS")]),                      # other_path, fresh
    ])
    hook = _make_tool_interception_hook()
    team = _Team()

    for _ in range(3):
        await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
                  team=team)

    sentinel = ToolResult(content=f"review_pending: {other_path}")

    async def ok(**kw):
        return sentinel

    out = await hook(APPLY, ok, {"relative_path": other_path}, agent=_Agent("Coder"), team=team)
    assert out is sentinel


# ── 5. Diagnostics content ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_diagnostics_preserve_file_line_column_code_message_verbatim(on, monkeypatch):
    diag = _diag("src/foo.ts", "Cannot find name 'bar'.", line=17, column=3, code="TS2304")
    _mock_verify(monkeypatch, _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]))
    hook = _make_tool_interception_hook()
    out = await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
                     team=_Team())
    assert "src/foo.ts" in out
    assert "17" in out and "3" in out
    assert "TS2304" in out
    assert "Cannot find name 'bar'." in out


@pytest.mark.asyncio
async def test_unstructured_failure_falls_back_to_raw_output_not_silently_dropped(on, monkeypatch):
    """A check that FAILed with no parsed diagnostics must still surface something
    actionable -- never a blank repair request."""
    _mock_verify(monkeypatch, _vr("FAIL", [_check(
        "test", "FAIL", exit_code=1, raw_output="FAIL tests/foo.test.ts\n3 failing")]))
    hook = _make_tool_interception_hook()
    out = await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
                     team=_Team())
    assert "3 failing" in out


# ── 6. Targets ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_apply_diff_relative_path_is_the_target_passed_to_verify_project(
        on, monkeypatch):
    calls = _mock_verify(monkeypatch, _vr("PASS", [_check("typecheck", "PASS")]))
    hook = _make_tool_interception_hook()
    await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
              team=_Team())
    assert calls[0]["targets"] == [PATH]


@pytest.mark.asyncio
async def test_no_target_is_ever_invented_from_task_prose(on, monkeypatch):
    """Only args["relative_path"] may ever appear in targets -- nothing from any
    surrounding task text, which this test deliberately never provides."""
    calls = _mock_verify(monkeypatch, _vr("PASS", [_check("typecheck", "PASS")]))
    hook = _make_tool_interception_hook()
    await hook(APPLY, _apply_diff_ok, {"relative_path": PATH, "old_string": "a",
                                       "new_string": "b"},
              agent=_Agent("Coder"), team=_Team())
    assert calls[0]["targets"] == [PATH]
    assert len(calls[0]["targets"]) == 1


# ── 7. Telemetry (extends Phase 0, does not replace or parallel it) ────────────────

@pytest.mark.asyncio
async def test_mechverify_event_is_recorded_on_the_runs_phase0(on, monkeypatch):
    _mock_verify(monkeypatch, _vr("FAIL", [_check("typecheck", "FAIL",
                                                   diagnostics=[_diag(PATH, "x", line=1)])]))
    run = Phase0Run("ekam", None, "engineering", False)
    hook = _make_tool_interception_hook()
    await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
              team=_Team(phase0_run=run))

    assert len(run.mechverify) == 1
    ev = run.mechverify[0]
    assert ev["status"] == "FAIL"
    assert ev["target"] == PATH
    assert ev["member"] == "coder"
    assert ev["checks"]
    assert ev["repair_attempt"] == 0
    assert ev["repair_requested"] is True
    assert ev["repair_limit_reached"] is False
    assert ev["proceeded_to_reviewer"] is False
    assert isinstance(ev["duration_ms"], int)


@pytest.mark.asyncio
async def test_telemetry_reports_repair_limit_reached_and_proceeded_to_reviewer(
        on, monkeypatch):
    diag = _diag(PATH, "boom", line=1)
    _mock_verify(monkeypatch, [
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
        _vr("FAIL", [_check("typecheck", "FAIL", diagnostics=[diag])]),
    ])
    run = Phase0Run("ekam", None, "engineering", False)
    hook = _make_tool_interception_hook()
    team = _Team(phase0_run=run)

    for _ in range(3):
        await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
                  team=team)

    assert len(run.mechverify) == 3
    last = run.mechverify[-1]
    assert last["repair_limit_reached"] is True
    assert last["proceeded_to_reviewer"] is True
    assert last["repair_requested"] is False


@pytest.mark.asyncio
async def test_no_mechverify_telemetry_when_the_flag_is_off(monkeypatch):
    """Verifies the telemetry extension is additive: with the treatment off, the
    existing Phase 0 observer (note_tool_call) still runs, but no mechverify event
    is ever recorded."""
    run = Phase0Run("ekam", None, "engineering", False)
    hook = _make_tool_interception_hook()
    await hook(APPLY, _apply_diff_ok, {"relative_path": PATH}, agent=_Agent("Coder"),
              team=_Team(phase0_run=run))

    assert run.mechverify == []
    assert phase0.action_snapshot("coder")["write_tool_calls_executed"] == 1
