"""T36 Track D -- range-shaped citations false-MISMATCH when only the range's END
is checked for quoted content.

Root cause, confirmed against the live T10 answer (session 39ea188d-5714-4e2e-9377-
3235c38f6e6c, 2026-10-19 battery, recovered verbatim from this conversation's own
transcript):

    "- `authUtil/authHelper.py`: Line 132-168 defines the `check_login_rate_limit`
    function."

`authHelper.py:132` is the REAL declaration line. `_LABELED_LINE_RE`'s range match
registers BOTH endpoints (132 and 168) as independent `file_lines` citations, and
both get the SAME quoted span `check_login_rate_limit` as their `content_claims`
entry. The window check at 132 passes (the declaration is right there); the window
check at 168 -- the function's own CLOSING line, which does not repeat its own name
-- fails, and the live report flagged it:

    MISMATCH   .../authHelper.py:168 <-- quoted 'check_login_rate_limit' not found
    within 5 lines; citation and quoted content do not point at the same place

on an answer that was entirely correct. The same shape produced a second live
MISMATCH the same run, on UserLoginService.py:177 (real declaration at 139).

Fix: when the quote-window check at `num` fails AND `num` is the END of a range the
answer itself declared (`_range_containing`, already used by the sibling declared-
symbol-range check), also try the window around the range's START before reporting
MISMATCH. A symbol genuinely absent from both ends, present only away from either
endpoint, or fabricated outright, still reports MISMATCH exactly as before.
"""
from tools import verify


def _mk_auth_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})
    real_file = tmp_path / "authHelper.py"
    lines = [f"# filler line {i}" for i in range(1, 200)]
    lines[131] = "def check_login_rate_limit(user_id):"   # real line 132
    real_file.write_text("\n".join(lines), encoding="utf-8")
    return real_file


# ── Case A: the exact live T10 shape -- symbol only at the range's START ───────

def test_t10_real_shape_symbol_at_range_start_not_flagged(tmp_path, monkeypatch):
    _mk_auth_helper(tmp_path, monkeypatch)
    answer = (
        "`authHelper.py`: Line 132-168 defines the `check_login_rate_limit` function."
    )
    report = verify.verify_claims(answer)
    assert "MISMATCH" not in report
    assert "verified within 5 lines of the cited range's start" in report
    assert "132" in report


# ── Case B: symbol genuinely at the range's END -- already worked, must still ──

def test_symbol_at_range_end_still_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})
    real_file = tmp_path / "authHelper.py"
    lines = [f"# filler line {i}" for i in range(1, 200)]
    lines[167] = "    return check_login_rate_limit_done"   # real line 168
    real_file.write_text("\n".join(lines), encoding="utf-8")
    answer = (
        "`authHelper.py`: Line 132-168 defines the `check_login_rate_limit` function."
    )
    report = verify.verify_claims(answer)
    assert "MISMATCH" not in report


# ── Case C: symbol absent from BOTH endpoints -- genuinely wrong, must still fire ─

def test_symbol_absent_from_both_endpoints_still_flagged(tmp_path, monkeypatch):
    _mk_auth_helper(tmp_path, monkeypatch)  # symbol only at 132, not near 168 -- N/A here
    answer = (
        "`authHelper.py`: Line 50-60 defines the `check_login_rate_limit` function."
    )
    report = verify.verify_claims(answer)
    assert "MISMATCH" in report
    assert "check_login_rate_limit" in report


# ── Case D: a fabricated symbol is not accepted just because a range was given ──

def test_fabricated_symbol_in_range_still_flagged(tmp_path, monkeypatch):
    _mk_auth_helper(tmp_path, monkeypatch)
    answer = (
        "`authHelper.py`: Line 132-168 defines the `totally_fabricated_rate_limiter` "
        "function."
    )
    report = verify.verify_claims(answer)
    assert "MISMATCH" in report
    assert "totally_fabricated_rate_limiter" in report


# ── Case E: the second live citation from the same T10 run ─────────────────────

def test_t10_second_live_citation_user_login_service(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})
    real_file = tmp_path / "UserLoginService.py"
    lines = [f"# filler line {i}" for i in range(1, 200)]
    lines[138] = "def count_recent_failed_attempts(user_id):"   # real line 139
    real_file.write_text("\n".join(lines), encoding="utf-8")
    answer = (
        "`UserLoginService.py`: Line 139-177 defines the "
        "`count_recent_failed_attempts` method used by the rate limiter."
    )
    report = verify.verify_claims(answer)
    assert "MISMATCH" not in report
