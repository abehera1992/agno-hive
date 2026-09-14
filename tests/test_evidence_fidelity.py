"""Phase N -- evidence fidelity & model I/O observability.

Forensics (this phase, see docs/guide/deferred-limitations-ledger.md's
Phase N entry and this project's own extensive existing groundedness-guard
comments) traced the T2/T3/T8/T9/T11/T12/T13a/T13b failure family to their
already-documented first-divergence points and current fix status. None of
those live-tuned guards were modified this phase -- this project has no
live model available to validate a change against a real battery, and
"prove the required boundary first" extends to not touching machinery
tuned through dozens of documented live incidents without that proof.

What WAS built: swarm.team._evidence_fidelity_report, a deterministic,
token-level trace of tool evidence -> member relay -> final answer,
reusing _salient_tokens (the SAME extraction _answer_supported_by_evidence
already uses) rather than any new heuristic. Raw model request/completion
capture was evaluated and judged NOT required -- every input this function
needs is already durable (RunContext.evidence, Phase A) or already
in-memory for the run's own ordinary purposes (team._member_results).

Deliberately NOT wired into the live answer-generation pipeline (verified
below by a source-level test) -- standalone, tested, awaiting a proven
wiring need, matching this project's own established pattern for
primitives built ahead of live validation.

Every test here is SIMULATED (constructed RunContext/text, not a live
model) -- clearly distinguished from live-model validation, which this
environment cannot perform.
"""
import types

from swarm.execution_context import RunContext
from swarm.team import _evidence_fidelity_report, _fidelity_agent_name


def _rc_with_evidence(content, tool_name="get_file_content", agent_name="Coordinator"):
    rc = RunContext("session-1", "run-1")
    root = rc.start_execution(agent_name, "coordinator", parent_execution_id=None)
    tc = rc.start_tool_call(tool_name, {"path": "a.py"})
    rc.finish_tool_call(tc, content=content, success=True, error=None)
    return rc


# ── 1-3. Retention ratios reflect actual token survival --------------------

def test_full_retention_when_relay_and_answer_contain_everything():
    rc = _rc_with_evidence("the vouchers_api.py router has 6 endpoints and status 200")

    report = _evidence_fidelity_report(
        rc,
        {"coordinator": "the vouchers_api.py router has 6 endpoints and status 200"},
        "the vouchers_api.py router has 6 endpoints and status 200")

    item = report["items"][0]
    assert item["relay_retention"] == 1.0
    assert item["answer_retention"] == 1.0
    assert report["overall_relay_retention"] == 1.0
    assert report["overall_answer_retention"] == 1.0


def test_partial_retention_when_relay_drops_some_evidence():
    rc = _rc_with_evidence("vouchers_api.py has 6 endpoints and 3 hooks and status_code 200")

    report = _evidence_fidelity_report(
        rc,
        {"coordinator": "vouchers_api.py has 6 endpoints"},  # dropped "3 hooks", "status_code", "200"
        "vouchers_api.py has 6 endpoints")

    item = report["items"][0]
    assert 0.0 < item["relay_retention"] < 1.0
    assert item["relay_retention"] == item["answer_retention"]  # same text this time


def test_zero_retention_is_a_real_zero_not_none():
    rc = _rc_with_evidence("vouchers_api.py has 6 endpoints")

    report = _evidence_fidelity_report(
        rc,
        {"coordinator": "completely unrelated text about something else entirely"},
        "still nothing to do with the evidence at all")

    item = report["items"][0]
    assert item["relay_retention"] == 0.0
    assert item["answer_retention"] == 0.0


# ── 4. No salient tokens -> None, never a fabricated 0.0 --------------------

def test_no_salient_tokens_in_evidence_gives_none_not_zero():
    rc = _rc_with_evidence("ok")  # no digits/paths/identifiers -- nothing citable

    report = _evidence_fidelity_report(rc, {"coordinator": "ok"}, "ok")

    item = report["items"][0]
    assert item["evidence_tokens"] == 0
    assert item["relay_retention"] is None
    assert item["answer_retention"] is None


# ── 5. MCP-wrapper-shaped evidence content is unwrapped correctly ----------

def test_mcp_wrapper_shaped_evidence_content_is_unwrapped():
    wrapper = types.SimpleNamespace(content="vouchers_api.py has 6 endpoints")
    rc = _rc_with_evidence(wrapper)

    report = _evidence_fidelity_report(
        rc, {"coordinator": "vouchers_api.py has 6 endpoints"},
        "vouchers_api.py has 6 endpoints")

    assert report["items"][0]["evidence_tokens"] > 0
    assert report["items"][0]["relay_retention"] == 1.0


# ── 6. Multiple agents: each evidence item checked against ITS OWN relay ----

def test_each_evidence_item_checked_against_its_owning_agents_own_relay():
    rc = RunContext("session-1", "run-1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    tc1 = rc.start_tool_call("get_file_content", {"path": "a.py"})
    rc.finish_tool_call(tc1, content="business_api.py has 13 endpoints", success=True, error=None)

    child = rc.start_execution("researcher", "delegation", parent_execution_id=rc.root_execution_id)
    tc2 = rc.start_tool_call("get_file_content", {"path": "b.ts"})
    rc.finish_tool_call(tc2, content="businessApi.ts has 16 hooks", success=True, error=None)

    member_results = {
        "coordinator": "business_api.py has 13 endpoints",     # full retention for tc1
        "researcher": "nothing at all about hooks",             # zero retention for tc2
    }
    report = _evidence_fidelity_report(rc, member_results, "13 endpoints, 16 hooks")

    by_tool_call = {i["tool_call_id"]: i for i in report["items"]}
    # agent_name reports the RAW ExecutionRecord spelling (informational);
    # the LOOKUP into member_results normalizes via _member_key internally
    # (see the function's own comment) -- member_results' keys here are
    # already lowercase, so this proves that normalization actually ran.
    assert by_tool_call[tc1.tool_call_id]["agent_name"] == "Coordinator"
    assert by_tool_call[tc1.tool_call_id]["relay_retention"] == 1.0
    assert by_tool_call[tc2.tool_call_id]["agent_name"] == "researcher"
    assert by_tool_call[tc2.tool_call_id]["relay_retention"] == 0.0


# ── 7-8. Defensive inputs --------------------------------------------------

def test_no_run_context_returns_empty_report_without_crashing():
    report = _evidence_fidelity_report(None, {"coordinator": "x"}, "x")
    assert report["items"] == []
    assert report["overall_relay_retention"] is None
    assert report["overall_answer_retention"] is None


def test_none_member_results_is_treated_as_empty_without_crashing():
    rc = _rc_with_evidence("vouchers_api.py has 6 endpoints")
    report = _evidence_fidelity_report(rc, None, "vouchers_api.py has 6 endpoints")
    assert report["items"][0]["relay_retention"] == 0.0  # nothing to compare against


# ── 9. Never mutates anything -----------------------------------------------

def test_never_mutates_evidence_member_results_or_answer():
    rc = _rc_with_evidence("vouchers_api.py has 6 endpoints")
    member_results = {"coordinator": "vouchers_api.py has 6 endpoints"}
    answer = "vouchers_api.py has 6 endpoints"
    ev_before = list(rc.evidence)
    mr_before = dict(member_results)

    _evidence_fidelity_report(rc, member_results, answer)

    assert rc.evidence == ev_before
    assert member_results == mr_before
    assert rc.evidence[0].content == "vouchers_api.py has 6 endpoints"  # byte-identical, untouched


def test_never_mutates_evidence_even_with_a_wrapper_object():
    wrapper = types.SimpleNamespace(content="vouchers_api.py has 6 endpoints")
    rc = _rc_with_evidence(wrapper)

    _evidence_fidelity_report(rc, {"coordinator": "x"}, "x")

    assert rc.evidence[0].content is wrapper  # the same object, never replaced or edited


# ── 10. Aggregate ratios are token-weighted, not a naive per-item average ---

def test_overall_retention_is_token_weighted_not_a_flat_average():
    rc = RunContext("session-1", "run-1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    tc1 = rc.start_tool_call("get_file_content", {"path": "a.py"})
    # 1 salient token, fully retained.
    rc.finish_tool_call(tc1, content="200", success=True, error=None)
    tc2 = rc.start_tool_call("get_file_content", {"path": "b.py"})
    # Many salient tokens, none retained.
    rc.finish_tool_call(
        tc2, content="business_api.py inventory_api.py voucher_api.py storage_api.py",
        success=True, error=None)

    report = _evidence_fidelity_report(rc, {"coordinator": "200"}, "200")

    # A flat average of [1.0, 0.0] would be 0.5; the token-weighted result
    # must be much lower, since tc2 contributed far more evidence tokens.
    assert report["overall_relay_retention"] < 0.3


# ── 11. Not wired into the live answer-generation pipeline -----------------

def test_not_called_from_verified_answer_or_any_live_pipeline_function():
    import inspect
    import swarm.team as team_mod
    for fn in (team_mod._verified_answer, team_mod.run_task_async, team_mod.run_task_stream):
        src = inspect.getsource(fn)
        assert "_evidence_fidelity_report(" not in src


def test_read_only_no_persistence_or_mutation_calls():
    import inspect
    from swarm.team import _evidence_fidelity_report as fn
    src = inspect.getsource(fn)
    for forbidden in ("execution_store.persist", "execution_store.promote",
                      ".insert(", ".update(", ".delete("):
        assert forbidden not in src


# ── 12. Simulated T9-style fidelity-loss scenario ---------------------------

def test_simulated_t9_style_relay_collapse_is_correctly_localized():
    """SIMULATED, not live-model-validated: reconstructs the shape of the
    documented T9 incident (get_env_info returns real OS/Python/path facts;
    the relay collapses them to a short precis; the final answer states
    DIFFERENT, fabricated values) and confirms the report correctly shows
    the loss occurring at the member->coordinator relay boundary, not at
    the tool->evidence boundary (evidence_tokens > 0, i.e. the tool DID
    return real facts) and not by claiming the final answer's fabricated
    values are somehow 'supported' (they score 0 answer_retention because
    they never appear in the real evidence at all)."""
    real_tool_output = (
        "OS: Linux 6.6.87.2-microsoft-standard-WSL2 (x86_64)\n"
        "Python: 3.12.14 at /usr/local/bin/python\n"
        "Project root: /project"
    )
    rc = _rc_with_evidence(real_tool_output, tool_name="get_env_info", agent_name="executor")
    lossy_relay = "Environment checked, all good."  # the real 27.6:1-style collapse
    fabricated_answer = "Ubuntu 22.04.4 LTS / Python 3.11.6 / /home/ubuntu/ekam-app"

    report = _evidence_fidelity_report(
        rc, {"executor": lossy_relay}, fabricated_answer)

    item = report["items"][0]
    assert item["evidence_tokens"] > 0        # the tool DID return real, citable facts
    assert item["relay_retention"] == 0.0     # none of them survived into the relay
    assert item["answer_retention"] == 0.0    # the shipped answer's values match NONE of them
