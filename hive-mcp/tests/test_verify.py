from tools import verify


def test_extracts_dotted_identifier_from_fenced_code_block(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])  # nothing found anywhere
    answer = "Here is the code:\n```python\nx = item.stock_quantity\n```"

    report = verify.verify_claims(answer)

    assert "stock_quantity" in report
    assert "NOT FOUND" in report


def test_finds_dotted_identifier_when_rg_returns_a_hit(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:12:    sku = Column(String)"])
    answer = "```python\nx = item.sku\n```"

    report = verify.verify_claims(answer)

    assert "FOUND" in report
    assert "NOT FOUND" not in report


def test_skips_stdlib_prefixes_in_code_blocks(monkeypatch):
    calls = []
    def fake_rg(tok, **k):
        calls.append(tok)
        return []
    monkeypatch.setattr(verify, "_rg", fake_rg)
    answer = "```python\nimport csv\nw = csv.writer(f)\noutput = io.StringIO()\n```"

    verify.verify_claims(answer)

    assert not any("csv.writer" in c for c in calls)
    assert not any("io.StringIO" in c for c in calls)


def test_prose_backtick_extraction_still_works(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "The function is `doTheThing`."

    report = verify.verify_claims(answer)

    assert "doTheThing" in report
    assert "NOT FOUND" in report


def test_code_block_and_prose_idents_are_deduplicated(monkeypatch):
    calls = []
    def fake_rg(tok, **k):
        calls.append(tok)
        return []
    monkeypatch.setattr(verify, "_rg", fake_rg)
    answer = "Uses `item.stock_quantity`.\n```python\nx = item.stock_quantity\n```"

    verify.verify_claims(answer)

    assert calls.count("item.stock_quantity") == 1


def _reset_repeat_tracking():
    verify._checked_answer_counts = {}


def test_identical_answer_checked_twice_hard_stops(monkeypatch):
    _reset_repeat_tracking()
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "Uses `item.stock_quantity`."

    first = verify.verify_claims(answer)
    second = verify.verify_claims(answer)

    assert "NOT FOUND" in first
    assert "STOPPED" in second


def test_hard_stop_resets_so_a_third_identical_call_checks_again(monkeypatch):
    _reset_repeat_tracking()
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "Uses `item.stock_quantity`."

    verify.verify_claims(answer)   # first — checked normally
    verify.verify_claims(answer)   # second — STOPPED
    third = verify.verify_claims(answer)   # third — tracking was reset, checks normally again

    assert "STOPPED" not in third
    assert "NOT FOUND" in third


def test_different_answer_after_first_is_not_treated_as_repeat(monkeypatch):
    _reset_repeat_tracking()
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])

    first = verify.verify_claims("Uses `item.stock_quantity`.")
    second = verify.verify_claims("Uses `item.sku`.")

    assert "STOPPED" not in first
    assert "STOPPED" not in second


def test_revised_answer_after_a_stop_is_checked_normally(monkeypatch):
    _reset_repeat_tracking()
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])

    verify.verify_claims("Uses `item.stock_quantity`.")   # first
    verify.verify_claims("Uses `item.stock_quantity`.")   # second — STOPPED
    revised = verify.verify_claims("Uses `item.sku`.")     # a genuinely different, revised answer

    assert "STOPPED" not in revised
    assert "sku" in revised


def test_stopped_message_still_classifies_as_bad_for_the_orchestrator(monkeypatch):
    """swarm/team.py's _verify_claims classifies a report as bad via the literal
    check `"could NOT be found" in report`, and calls this tool up to twice per
    answer (an initial check, then a recheck of a correction round). If the
    correction round genuinely changes nothing, its second call lands on the
    STOPPED path — which must still satisfy that same string check, or the
    orchestrator would misread a stuck repeat as "verified good" and silently
    drop the fabrication disclaimer it would otherwise attach."""
    _reset_repeat_tracking()
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "Uses `item.stock_quantity`."

    verify.verify_claims(answer)
    second = verify.verify_claims(answer)

    assert "STOPPED" in second
    assert "could NOT be found" in second


# ── Phase 4 (AGNOHive Reliability Program): cross-caller dedup isolation ────
#
# hive-mcp is one process; this module's tracking state is shared across
# every concurrent tool call from every agent in every run pointed at it.
# The single-scalar design being replaced here (_last_checked_answer/
# _repeat_count) meant an interleaved, unrelated call on DIFFERENT text
# silently overwrote another caller's pending "first-seen" state, defeating
# that caller's own stuck-loop detection. These are synthetic reproductions
# (direct module-state interleaving, not a live concurrent server) of that
# exact mechanism, and a regression pin for the still-open residual case
# (two callers whose text is byte-identical by coincidence).

def test_interleaved_different_text_no_longer_corrupts_the_other_caller_s_tracking(monkeypatch):
    """Two 'runs' (A and B) interleave one call each on DIFFERENT text, then
    both submit their SECOND, unchanged call. Each must independently see
    its own repeat -- neither should be reset by the other's traffic."""
    _reset_repeat_tracking()
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    text_a = "Uses `item.stock_quantity`."
    text_b = "Uses `item.sku`."

    a1 = verify.verify_claims(text_a)   # Run A, 1st call
    b1 = verify.verify_claims(text_b)   # Run B, 1st call -- interleaves
    a2 = verify.verify_claims(text_a)   # Run A, 2nd call -- its own genuine repeat
    b2 = verify.verify_claims(text_b)   # Run B, 2nd call -- its own genuine repeat

    assert "STOPPED" not in a1 and "STOPPED" not in b1
    assert "STOPPED" in a2, "Run A's own repeat must be caught despite Run B's interleaved call"
    assert "STOPPED" in b2, "Run B's own repeat must be caught despite Run A's interleaved call"


def test_three_way_interleave_each_caller_s_own_repeat_is_still_isolated(monkeypatch):
    """x, y, z are each checked once, interleaved. Only y then repeats: y's
    slot must reset (its own stuck-loop signal was consumed), but x's and
    z's tracking must survive untouched -- proving one caller's repeat/reset
    cannot wipe a DIFFERENT caller's still-pending first-seen state."""
    _reset_repeat_tracking()
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    x, y, z = "Uses `item.x`.", "Uses `item.y`.", "Uses `item.z`."

    verify.verify_claims(x)  # 1st: x seen
    verify.verify_claims(y)  # 1st: y seen -- must not disturb x's tracking
    verify.verify_claims(z)  # 1st: z seen -- must not disturb x's or y's tracking

    assert "STOPPED" in verify.verify_claims(y)  # y's own genuine repeat, correctly caught
    # y's reset must be scoped to y alone -- x and z are still tracked as
    # "seen once", so THEIR eventual repeats remain detectable.
    assert x in verify._checked_answer_counts
    assert z in verify._checked_answer_counts
    assert y not in verify._checked_answer_counts


def test_tracking_is_bounded_and_does_not_grow_without_limit(monkeypatch):
    _reset_repeat_tracking()
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])

    for i in range(verify._MAX_TRACKED_ANSWERS + 5):
        verify.verify_claims(f"Uses `item.field_{i}`.")

    assert len(verify._checked_answer_counts) <= verify._MAX_TRACKED_ANSWERS


def test_residual_known_limitation_byte_identical_text_from_different_callers_still_collides(monkeypatch):
    """Documents what this fix does NOT solve, so it is never mistaken for a
    complete fix later: two callers whose answer text is byte-identical by
    coincidence still share one dedup slot, because nothing here carries a
    caller/session identity. The second caller is told STOPPED without its
    own claims ever having been checked once. Resolving this needs
    provenance this module does not have -- see the Phase 4 report."""
    _reset_repeat_tracking()
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    identical_text = "Uses `item.stock_quantity`."

    run_a_first_ever_check = verify.verify_claims(identical_text)
    run_b_first_ever_check = verify.verify_claims(identical_text)  # different caller, same text

    assert "STOPPED" not in run_a_first_ever_check
    assert "STOPPED" in run_b_first_ever_check  # known, documented residual gap
