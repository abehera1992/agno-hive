"""Phase AA -- repair_unguarded_draft must also cross verify_claims.

Phase AA's end-to-end audit traced the final-answer pipeline beyond
swarm/team.py into api/server.py's process-boundary rescue paths
(liveness auto-kill, worker crash, unparseable worker output -- all three
funnel through `_salvage()` -> `repair_unguarded_draft()`). That function's
own docstring already named the gap: "_verified_answer lives in the
worker... an auto-killed run reaches the caller with NO guard having
run -- not the scope check, not the integration check, not
verify_claims." Only the first two ever got a parent-side repair
(`_run_repo_derived_guards`, seven guards, none of them verify_claims) --
verify_claims itself was never wired into this rescue path, even though it
needs nothing repair_unguarded_draft doesn't already have (the answer text
and a hive-mcp URL).

Net effect before this fix: ANY run that got auto-killed, crashed, or
produced unparseable worker output shipped its salvaged draft to the
client having NEVER crossed the codebase's primary fabrication-detection
check, regardless of what the draft actually claimed.

The fix adds one bounded, fail-open verify_claims call inside
repair_unguarded_draft, reusing the SAME `_flagged_draft_note` disclosure
_verified_answer's own "retry produced nothing" paths already use for "a
known-bad draft with nothing left to correct it."
"""
import pytest

from swarm import team


@pytest.mark.asyncio
async def test_a_fabricated_killed_run_draft_is_flagged_not_shipped_clean(monkeypatch):
    async def fake_run_repo_derived_guards(*a, **k):
        return []  # isolate this test from the other 7 guards' own behavior
    monkeypatch.setattr(team, "_run_repo_derived_guards", fake_run_repo_derived_guards)

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        assert hive_mcp_url == "http://fake-hive-mcp"
        assert hive_mcp_tools is None  # no live session in the parent process
        return (
            "verify_claims — deterministic grep of the claims in this answer\n\n"
            "SYMBOLS (1 checked):\n"
            "  NOT FOUND  FooBarNonExistentClass              "
            "<-- does not exist in the project\n\n"
            "VERDICT: 1 claim(s) could NOT be found in the project."
        ), True, False
    monkeypatch.setattr(team, "_verify_claims", fake_verify_claims)

    # _pick_hive_mcp_url excludes whatever `mcp_url` resolves to (the project
    # MCP, by convention) -- the real hive-mcp URL has to come through
    # `mcp_urls` instead, matching how api/server.py actually calls this.
    draft = "The `FooBarNonExistentClass` handles this. Everything checks out."
    out = await team.repair_unguarded_draft(
        "describe the module", draft,
        mcp_url="http://project-mcp", mcp_urls=["http://fake-hive-mcp"])

    assert "FooBarNonExistentClass" in out
    assert "could NOT be found" in out


@pytest.mark.asyncio
async def test_a_genuinely_clean_killed_run_draft_adds_no_false_disclaimer(monkeypatch):
    async def fake_run_repo_derived_guards(*a, **k):
        return []
    monkeypatch.setattr(team, "_run_repo_derived_guards", fake_run_repo_derived_guards)

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team, "_verify_claims", fake_verify_claims)

    calls = {"n": 0}
    _orig = fake_verify_claims

    async def counting_verify_claims(*a, **k):
        calls["n"] += 1
        return await _orig(*a, **k)
    monkeypatch.setattr(team, "_verify_claims", counting_verify_claims)

    draft = "The vouchers table has these real columns: voucher_id, status."
    out = await team.repair_unguarded_draft(
        "describe the module", draft,
        mcp_url="http://project-mcp", mcp_urls=["http://fake-hive-mcp"])

    assert calls["n"] == 1  # actually ran, not skipped
    assert out == ""  # nothing fired -- no false positive


@pytest.mark.asyncio
async def test_verify_claims_failure_is_fail_open_not_fatal(monkeypatch):
    """The whole point of a rescue path: a failing check must cost the caller
    nothing more than the note it would have added, never an exception that
    loses the draft entirely."""
    async def fake_run_repo_derived_guards(*a, **k):
        return []
    monkeypatch.setattr(team, "_run_repo_derived_guards", fake_run_repo_derived_guards)

    async def raising_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        raise RuntimeError("hive-mcp unreachable")
    monkeypatch.setattr(team, "_verify_claims", raising_verify_claims)

    draft = "Some draft text."
    out = await team.repair_unguarded_draft(
        "describe the module", draft,
        mcp_url="http://project-mcp", mcp_urls=["http://fake-hive-mcp"])

    assert out == ""  # no exception propagates, no crash


@pytest.mark.asyncio
async def test_no_hive_mcp_url_skips_everything_as_before(monkeypatch):
    """Existing behavior, unchanged: with no hive-mcp URL at all, the function
    returns "" immediately, before either guard family runs."""
    calls = {"n": 0}

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        calls["n"] += 1
        return "", False, False
    monkeypatch.setattr(team, "_verify_claims", fake_verify_claims)

    out = await team.repair_unguarded_draft("task", "some draft", mcp_url=None, mcp_urls=None)

    assert out == ""
    assert calls["n"] == 0
