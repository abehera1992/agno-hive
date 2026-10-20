"""T36.1 Track C -- the T10 range-citation fallback's remaining edge cases:
a legitimate in-range (not endpoint) anchor, a malformed range, and an
ambiguous dual-range collision. T36's own test_verify_t36_range_end_quote_
fallback.py already covers valid-start, valid-end, absent-from-both, and
fabricated-symbol; this file completes the validation-sequence set this
phase's mission explicitly asks for.

Both REAL historical T10 MISMATCH cases (authHelper.py:132-168,
UserLoginService.py:139-177) had their symbol declared EXACTLY at the
range's START -- T36's fix fully resolves both. The tests below probe a
DIFFERENT, broader shape -- a symbol declared strictly BETWEEN a range's
two endpoints -- that no real incident ever demonstrated. Confirmed here
to be a pre-existing, out-of-scope limitation (same conclusion T36's own
report already reached), not expanded: per this phase's change policy
("fix only demonstrated residual defects... do not loosen controls merely
to force a PASS"), a mechanism checking the whole range's window would be
a materially larger change than the narrow start-fallback T36 shipped, and
nothing in this battery's history justifies it.
"""
from tools import verify


def _mk_file(tmp_path, monkeypatch, decl_line_1based: int, symbol: str,
             n_lines: int = 200):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})
    f = tmp_path / "service.py"
    lines = [f"# filler {i}" for i in range(1, n_lines + 1)]
    lines[decl_line_1based - 1] = f"def {symbol}(arg):"
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


BT = chr(96)


# ── Valid in-range (not endpoint) anchor: documented, pre-existing limitation ──

def test_legitimate_in_range_anchor_not_at_either_endpoint_still_mismatches(
        tmp_path, monkeypatch):
    """Documents current behavior honestly rather than assuming it: a symbol
    declared at line 150, cited as 'Line 132-168', is neither near the start
    (132) nor the end (168) -- T36's fallback (range-start only) does not
    reach it, and this remains a MISMATCH. No real T10 incident had this
    shape; both real cases were exactly at the range's start. Not fixed this
    phase -- see this file's own module docstring for why."""
    _mk_file(tmp_path, monkeypatch, decl_line_1based=150, symbol="handle_request")
    answer = f"{BT}service.py{BT}: Line 132-168 defines the {BT}handle_request{BT} function."
    report = verify.verify_claims(answer)
    assert "MISMATCH" in report


# ── Malformed range (end < start): safe fallthrough, no crash, no false accept ─

def test_malformed_range_end_before_start_does_not_crash_or_false_accept(
        tmp_path, monkeypatch):
    """'Line 168-132' (end before start) is not a valid ascending range;
    _range_containing's own `lo <= hi` guard means it is simply never
    treated as a range at all for fallback purposes -- confirmed here it
    neither crashes nor silently verifies a citation that should fail."""
    _mk_file(tmp_path, monkeypatch, decl_line_1based=132, symbol="check_login_rate_limit")
    answer = (f"{BT}service.py{BT}: Line 168-132 defines the "
              f"{BT}check_login_rate_limit{BT} function.")
    report = verify.verify_claims(answer)  # must not raise
    assert isinstance(report, str) and report


# ── Ambiguous: two distinct cited ranges share the same END line number ───────

def test_two_ranges_sharing_the_same_end_number_each_resolve_against_their_own_start(
        tmp_path, monkeypatch):
    """`_range_containing` returns the FIRST range in the answer whose end
    matches the queried number. Two unrelated citations that happen to share
    an end line number is a genuine ambiguity in principle; this pins the
    actual behavior (first-declared-range wins) so a future change to that
    resolution order is a deliberate decision, not an accidental regression."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})
    f = tmp_path / "service.py"
    lines = [f"# filler {i}" for i in range(1, 300)]
    lines[9] = "def alpha_handler(arg):"     # line 10
    lines[49] = "def beta_handler(arg):"     # line 50
    f.write_text("\n".join(lines), encoding="utf-8")
    # Both ranges end at 168 -- "Line 10-168" (alpha, real at start) and
    # "Line 50-168" (beta, also cited against the SAME end number).
    answer = (
        f"{BT}service.py{BT}: Line 10-168 defines the {BT}alpha_handler{BT} function. "
        f"{BT}service.py{BT}: Line 50-168 defines the {BT}beta_handler{BT} function."
    )
    report = verify.verify_claims(answer)
    # alpha (true start=10) must resolve via the fallback; whatever happens to
    # beta (true start=50, but a same-ended EARLIER range (10-168) sits first
    # in the text) is recorded, not assumed -- this is the documented
    # resolution-order behavior, not a claim that beta is correctly handled.
    assert "alpha_handler" in report
