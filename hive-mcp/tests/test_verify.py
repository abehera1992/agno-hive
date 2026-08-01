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
