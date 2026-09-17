"""Phase AD -- durable /run answer-integrity regression harness.

Phases Y, Z, AA, and AC each found and closed one concrete verification
bypass (a specific function shipping content that had never crossed
verify_claims, or was checked against a stale snapshot of itself). This
file is not another bypass hunt -- it converts that accumulated forensic
knowledge into ONE reusable invariant assertion plus a small set of
composition tests that chain the REAL production functions together, in
their REAL pipeline order, so a future regression in any of them (or in
how they interact) fails a test instead of requiring another live-battery
forensic phase to rediscover.

The core invariant under test, matching this phase's own framing:

    Every substantive answer mutation must either be deterministic/
    additive and incapable of invalidating prior verification, or the
    resulting content must pass verify_claims() before it can be shipped.

Every test here is SIMULATED (monkeypatched _stream_team_run/
_verify_claims/_computed_comparison standing in for a live model and
hive-mcp), matching this whole file family's established convention.
"""
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import (
    _evidence_integrity_check,
    _hoist_denied_premise,
    _verified_answer,
)


# ── 1. The reusable invariant assertion --------------------------------------

def _assert_content_is_covered(shipped: str, verified_snapshots: set[str]) -> None:
    """The Phase AD invariant, as one reusable assertion every composition
    test below calls on the TRULY FINAL text a pipeline chain produced.

    `verified_snapshots` is every distinct string _verify_claims was ever
    actually invoked with during the chain (collected by a test's own
    monkeypatched fake). `shipped` passes when it is:

      * byte-identical to something already verified, or
      * a deterministic/additive wrapping of something already verified --
        checked by containment, matching how every real safe transformation
        in this codebase actually works (_hoist_denied_premise prepends a
        banner, _force_uncertainty_answer/_flagged_draft_note prepend a
        banner and keep the wrapped text verbatim, _summarize_actual_writes
        and the coordinator-alone/tool-budget notes append one) -- never by
        a fuzzy similarity score, which would hide a genuine substitution.

    Fails, by design, on exactly the shape every closed bypass had: content
    that is neither in the verified set nor built by wrapping something
    that is.
    """
    if shipped in verified_snapshots:
        return
    for v in verified_snapshots:
        if v and v in shipped:
            return
    raise AssertionError(
        f"shipped content was never verified and is not a deterministic/"
        f"additive wrapping of anything that was -- shipped={shipped!r}, "
        f"verified_snapshots={verified_snapshots!r}")


def test_the_invariant_helper_itself_catches_an_unverified_swap():
    """Self-test: the helper must fail on the exact shape every closed
    bypass had (verify A, ship a wholly different B) and pass on both safe
    shapes (unchanged, or wrapped)."""
    verified = {"The table has 0 rows."}
    with pytest.raises(AssertionError):
        _assert_content_is_covered("Something completely different.", verified)
    _assert_content_is_covered("The table has 0 rows.", verified)  # unchanged
    _assert_content_is_covered(
        "**BANNER**\n\n---\nThe table has 0 rows.", verified)  # wrapped


# ── Shared fixture plumbing (matches test_unfinished_intent.py /
#    test_evidence_integrity.py's own established conventions) -------------

def _msgs(*items):
    return SimpleNamespace(messages=list(items))


def _tool_msg(name: str, content: str):
    return SimpleNamespace(role="tool", tool_name=name, content=content)


class _FakeTeam:
    def __init__(self, retry_result):
        self._retry_result = retry_result
        self.prompts = []

    def arun(self, prompt, stream=False, yield_run_output=False):
        self.prompts.append(prompt)
        if stream:
            return self._stream()
        return self._direct()

    async def _direct(self):
        return self._retry_result

    async def _stream(self):
        if self._retry_result is not None:
            yield self._retry_result


def _verify_recorder(monkeypatch, fabricated_marker: str):
    """Installs a fake _verify_claims that records every distinct text it is
    called with, and flags `fabricated_marker` as bad wherever it appears.
    Returns the set of recorded texts for the test to assert against."""
    seen: set[str] = set()

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        seen.add(content)
        if fabricated_marker in content:
            return (f"verify_claims — deterministic grep of the claims in this answer\n\n"
                     f"SYMBOLS (1 checked):\n"
                     f"  NOT FOUND  {fabricated_marker}              "
                     f"<-- does not exist in the project\n\n"
                     f"VERDICT: 1 claim(s) could NOT be found in the project."
                     ), True, False
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)
    return seen


# ── 2. Composition A: _adopt_retry fires, THEN a separate guard also fires,
#      chained through the REAL production functions in REAL order --------

@pytest.mark.asyncio
async def test_composition_A_adopt_retry_then_hoist_denied_premise(monkeypatch):
    """_verified_answer's own no-evidence guard adopts a retry (a real
    _adopt_retry site), and the ADOPTED text is what the rest of the real
    pipeline (_hoist_denied_premise) sees next -- not the pre-retry draft.
    Proves the composition uses the truly final _verified_answer output,
    not a stale reference to the original draft."""
    verified = _verify_recorder(monkeypatch, "FooBarNonExistentClass")

    async def fake_stream(*a, **k):
        # A genuinely clean, more-grounded retry (real file read).
        return "The sku_prefix column exists in models.py at line 129.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    # A genuine zero-read result: _count_read_calls treats a truly empty
    # `messages` list as -1 (undeterminable, never triggers the guard) --
    # one recognisable non-read tool message is what actually reads as a
    # real, determinable zero (see test_retry_adjudication.py's own
    # _reads(0) fixture, the established convention this reuses).
    original_result = _msgs(_tool_msg("verify_claims", "ok"))
    # _CLAIMY_RE requires a backticked symbol, a path:line citation, or a
    # number+rows/count phrase -- a bare prose sentence never trips the
    # no-evidence guard at all.
    content = "The `sku_prefix` column is present, based on prior knowledge."
    fake_team = _FakeTeam(SimpleNamespace(
        content="The sku_prefix column exists in models.py at line 129.",
        messages=[_tool_msg("get_file_content", "sku_prefix = Column(...)")]))

    stage1 = await _verified_answer(
        content, "describe sku_prefix", fake_team, "http://fake-hive-mcp",
        result=original_result, hive_mcp_tools=object())
    stage2 = await _hoist_denied_premise(stage1, "http://fake-hive-mcp", hive_mcp_tools=object())

    # The final text is the ADOPTED retry (or a deterministic wrapping of it),
    # not the original zero-evidence draft.
    _assert_content_is_covered(stage2, verified)
    assert "sku_prefix column exists" in stage2


# ── 3. Composition B: reconciliation candidate fails verification, gets
#      wrapped in uncertainty -- the exact Phase AC shape, run through the
#      real _evidence_integrity_check ---------------------------------------

@pytest.mark.asyncio
async def test_composition_B_failed_reconciliation_wrapped_in_uncertainty(monkeypatch):
    verified = _verify_recorder(monkeypatch, "FooBarNonExistentClass")

    async def fake_stream(*a, **k):
        return ("The table has 500 rows. See `FooBarNonExistentClass` for "
                "the schema."), object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = SimpleNamespace(_read_state={}, _tool_evidence=[
        {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
    content = "The table has 12,473 rows."
    final = await _evidence_integrity_check(
        content, "task", team, "http://fake-hive-mcp", object())

    _assert_content_is_covered(final, verified)
    assert "UNRESOLVED" in final
    assert "could NOT be found" in final  # the fabrication is disclosed, not hidden


# ── 4. Composition C: content that never reaches _verified_answer at all --
#      the liveness/crash rescue path, its own single check --------------

@pytest.mark.asyncio
async def test_composition_C_rescue_path_is_the_only_check_this_content_gets(monkeypatch):
    """A liveness-killed run's draft never reaches _verified_answer (the
    worker holding it was SIGKILLed) -- repair_unguarded_draft is the FIRST
    and ONLY verification boundary this content will ever cross. Proves the
    invariant holds even when the "initial verify" stage in the phase's own
    diagram never ran at all."""
    verified = _verify_recorder(monkeypatch, "FooBarNonExistentClass")

    async def fake_run_repo_derived_guards(*a, **k):
        return []
    monkeypatch.setattr(team_mod, "_run_repo_derived_guards", fake_run_repo_derived_guards)

    draft = "The `FooBarNonExistentClass` handles migration compliance."
    final = await team_mod.repair_unguarded_draft(
        "task", draft, mcp_url="http://project-mcp", mcp_urls=["http://fake-hive-mcp"])
    shipped = draft + final  # api/server.py concatenates draft + repair notes verbatim

    _assert_content_is_covered(shipped, verified)
    assert "could NOT be found" in final


# ── 5. Composition D / the adversarial finalization test -------------------
#      A -> B -> C -> D, each hop capable of introducing a fabrication,
#      chained through the REAL _verified_answer -> _hoist_denied_premise ->
#      _evidence_integrity_check pipeline in REAL order. Proves it is D --
#      the truly final text -- that determines shipping, not A, B, or C.

@pytest.mark.asyncio
async def test_adversarial_A_to_D_only_the_final_hop_determines_shipping(monkeypatch):
    verified = _verify_recorder(monkeypatch, "VoucherSeriesConfigHistory")

    # Hop A -> B: _verified_answer's own no-evidence guard adopts a retry.
    # B is clean -- this hop must NOT be what blocks shipping.
    async def fake_stream_verified_answer(*a, **k):
        return "All voucher tables are Voucher, VoucherSeries, VoucherVersion.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream_verified_answer)

    original_result = _msgs(_tool_msg("verify_claims", "ok"))  # a real, determinable zero
    a = "The `Voucher` table exists, as I recall from prior context."
    fake_team = _FakeTeam(SimpleNamespace(
        content="All voucher tables are Voucher, VoucherSeries, VoucherVersion.",
        messages=[_tool_msg("get_file_content", "class Voucher(Base): ...")]))
    b = await _verified_answer(
        a, "list voucher tables", fake_team, "http://fake-hive-mcp",
        result=original_result, hive_mcp_tools=object())
    assert "Voucher" in b  # hop A->B happened

    # Hop B -> C: the safe, deterministic, additive transformation --
    # _hoist_denied_premise. Must add nothing verify_claims would need to
    # separately check (it fires only on a specific retired-symbol pattern,
    # which this content does not match, so C == B here -- proving the
    # harness does not demand unnecessary verification for a pass-through).
    c = await _hoist_denied_premise(b, "http://fake-hive-mcp", hive_mcp_tools=object())
    assert c == b

    # Hop C -> D: _evidence_integrity_check's own reconciliation candidate
    # is where the fabrication is introduced -- the LAST possible hop, after
    # two prior stages already "verified" their own outputs. This is the
    # exact composition Phase AC closed: D must not ship clean just because
    # B and C were fine.
    async def fake_computed_comparison(task, enumerations, hive_mcp_url,
                                        hive_mcp_tools, content, team=None):
        return ("**THE COMPARISON, COMPUTED**\n```\n"
                "TOTALS: left 9, right 48, matched 3, left-only 6, right-only 45.\n```")
    monkeypatch.setattr(team_mod, "_computed_comparison", fake_computed_comparison)

    async def fake_stream_reconcile(*a, **k):
        return ("All 9 backend endpoints are covered. See "
                "`VoucherSeriesConfigHistory` for the schema."), object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream_reconcile)

    team = SimpleNamespace(_read_state={}, _tool_evidence=[])
    # This content must itself carry a completeness claim to trigger
    # reconciliation -- reuse the exact real-incident phrasing.
    c_with_claim = c + " All 9 backend endpoints are covered."
    d = await _evidence_integrity_check(
        c_with_claim, "task", team, "http://fake-hive-mcp", object())

    # THE central assertion: D, the truly final text, is what must be
    # covered by verification -- not merely that A, B, or C were fine.
    _assert_content_is_covered(d, verified)
    assert "VoucherSeriesConfigHistory" in d
    assert "could NOT be found" in d  # Phase V's own fabricated-declaration
    # shape (an invented class in a nested Voucher*Config*History* chain) is
    # still caught at whichever hop it is actually introduced at, not just
    # when it appears in the very first draft.


# ── 6. Historical-incident regression pins ------------------------------
#      Small, deterministic fixtures for the named incidents this phase
#      lists, without reproducing full live prompts.

@pytest.mark.asyncio
async def test_historical_phase_Y_incident_fd66e6422564_shape_stays_fixed(monkeypatch):
    """The exact run_id=fd66e6422564 shape: a zero-tool-call reconciliation
    resolves the SPECIFIC contradiction it was asked about while fabricating
    an entirely different, self-consistent answer."""
    verified = _verify_recorder(monkeypatch, "getVoucherById")

    async def fake_computed_comparison(task, enumerations, hive_mcp_url,
                                        hive_mcp_tools, content, team=None):
        return ("**THE COMPARISON, COMPUTED**\n```\n"
                "TOTALS: left 9, right 48, matched 3, left-only 6, right-only 45.\n```")
    monkeypatch.setattr(team_mod, "_computed_comparison", fake_computed_comparison)

    async def fake_stream(*a, **k):
        # Zero tool calls -- resolves the completeness claim by inventing a
        # self-consistent but fictional endpoint/hook set instead.
        return ("All backend endpoints have a corresponding frontend hook, "
                "including `getVoucherById` and `createVoucher`."), object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = SimpleNamespace(_read_state={}, _tool_evidence=[])
    content = "All 9 backend endpoints are covered by frontend hooks."
    result = await _evidence_integrity_check(
        content, "task", team, "http://fake-hive-mcp", object())

    _assert_content_is_covered(result, verified)


# Phase V's own incident (a nested, invented class hierarchy shipping
# because the old backtick tokenizer split "class X(Y)" at its first "("
# into the always-rejected two-word string "class X") is a durable
# EXTRACTION-gap regression, not a replacement-after-verification bypass --
# it belongs to, and is already pinned in, hive-mcp's own test suite
# (hive-mcp/tests/test_verify_phase_v_regression.py, added with the W1/W2
# fix, commit 83ba560). hive-mcp/tools/verify.py imports a `config` module
# of its own (distinct from this repo's root-level `config` package this
# same pytest session already has loaded), so importing it here would
# either collide on `sys.modules["config"]` or require a second, separate
# test process -- not worth doing just to re-assert what that file already
# proves. Not duplicated here; cross-referenced instead.
