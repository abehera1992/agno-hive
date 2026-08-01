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
    verify._last_checked_answer = None
    verify._repeat_count = 0


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
