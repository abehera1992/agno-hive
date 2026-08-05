"""Regression tests: verify_claims must check WHERE a quoted citation actually is,
not just that the cited line number is within the file's bounds.

Confirmed live 2026-08-04 against the ekam project, AFTER the labeled-citation fix
(test_verify_labeled_line_citations.py) was already deployed: an answer wrote

    - **Source**: `models.py`, line 450-497
      > the docstring "L1 -- Legal entity / PAN-based." (triple-quoted in source)

The quoted docstring is real -- it exists verbatim in the file -- but at line 236,
not 450. The old citation check only confirmed the file had >= 450 lines (it has
774), so this passed cleanly with "VERDICT: every checked claim exists". Existence
of the line number is not existence of the claimed content AT that line.

Fix: when a citation is immediately followed by a backticked quote, read the real
lines around the cited line number and confirm the quote is actually there.
"""
from tools import verify


def test_quoted_content_far_from_cited_line_is_flagged_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:1:x"])
    lines = ["x = 1"] * 235 + ['    """L1 -- Legal entity / PAN-based."""'] + ["x = 1"] * 600
    (tmp_path / "models.py").write_text("\n".join(lines), encoding="utf-8")

    # Real docstring text (line 236), but cited at line 450 -- the exact live failure.
    answer = '**File:** `models.py`, **Line:** 450  \n> `"""L1 -- Legal entity / PAN-based."""`'

    report = verify.verify_claims(answer)

    assert "MISMATCH" in report
    assert "models.py:450" in report
    assert "VERDICT: 1 claim" in report


def test_quoted_content_at_cited_line_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:1:x"])
    lines = ["x = 1"] * 235 + ['    """L1 -- Legal entity / PAN-based."""'] + ["x = 1"] * 5
    (tmp_path / "models.py").write_text("\n".join(lines), encoding="utf-8")

    answer = '**File:** `models.py`, **Line:** 236  \n> `"""L1 -- Legal entity / PAN-based."""`'

    report = verify.verify_claims(answer)

    assert "MISMATCH" not in report
    assert "content verified" in report
    assert "VERDICT: every checked claim exists" in report


def test_quoted_content_within_tolerance_of_cited_line_passes(tmp_path, monkeypatch):
    # Citing the class line when the docstring is one line below is a normal,
    # legitimate citation style -- must not be flagged just because it isn't exact.
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:1:x"])
    lines = ["x = 1"] * 234 + ["class Party(Base):", '    """L1 docstring."""'] + ["x = 1"] * 5
    (tmp_path / "models.py").write_text("\n".join(lines), encoding="utf-8")

    # Cites the class line (235); the quoted docstring is actually on the next line (236).
    answer = '**File:** `models.py`, **Line:** 235  \n> `"""L1 docstring."""`'

    report = verify.verify_claims(answer)

    assert "MISMATCH" not in report
    assert "content verified" in report


def test_citation_without_a_nearby_quote_is_unaffected(tmp_path, monkeypatch):
    # No quoted content following the citation -- behavior must be identical to
    # before this fix: existence-only, no MISMATCH machinery engaged.
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:1:x"])
    (tmp_path / "models.py").write_text("\n".join(["x = 1"] * 10), encoding="utf-8")

    answer = "**File:** `models.py`, **Line:** 5 defines the config."

    report = verify.verify_claims(answer)

    assert "MISMATCH" not in report
    assert "content verified" not in report
    assert "LINE 5" in report


def test_next_citations_path_is_not_mistaken_for_this_ones_content(tmp_path, monkeypatch):
    # A bare path in the quote-search window belongs to the NEXT citation, not this
    # one's content -- must not be swallowed as a false "quote" to check.
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    (tmp_path / "a.py").write_text("\n".join(["x = 1"] * 10), encoding="utf-8")
    (tmp_path / "b.py").write_text("\n".join(["x = 1"] * 10), encoding="utf-8")

    answer = "See `a.py`, line 3, then `b.py`, line 7 for the continuation."

    report = verify.verify_claims(answer)

    assert "MISMATCH" not in report


def test_quoted_content_absent_from_file_entirely_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:1:x"])
    (tmp_path / "models.py").write_text("\n".join(["x = 1"] * 10), encoding="utf-8")

    answer = '**File:** `models.py`, **Line:** 5  \n> `"""this docstring was never written"""`'

    report = verify.verify_claims(answer)

    assert "MISMATCH" in report


class _FakeRgFiles:
    """Stand-in for `subprocess.run(["rg", "--files", ...])` -- _resolve_path's
    multi-candidate search shells out to real ripgrep, which this dev machine does
    not have on PATH (confirmed: shutil.which("rg") is None here locally, even
    though the Docker image installs it). Real ripgrep is exercised in the deployed
    container; these tests only need to prove _resolve_path's OWN disambiguation
    logic once handed a candidate list, which is what this fakes.
    """
    def __init__(self, paths: list[str]):
        self.stdout = "\n".join(paths)


def _mock_rg_files(monkeypatch, paths: list[str]):
    monkeypatch.setattr(verify.shutil, "which", lambda name: "rg")
    monkeypatch.setattr(verify.subprocess, "run", lambda *a, **k: _FakeRgFiles(paths))


def test_bare_filename_disambiguated_by_full_path_named_earlier(tmp_path, monkeypatch):
    """Confirmed live 2026-08-04: EkamApp has 8 files named models.py, one per
    service. An answer that states the full path ONCE up front ("as defined in
    `API/inventory-service/models.py`") and then cites the bare filename for every
    individual claim ("models.py:236") was reported AMBIGUOUS on every single one --
    never reaching the content-location check at all, because the answer's own
    disambiguating context was thrown away. It should use that context instead of
    giving up as soon as more than one file shares the basename.
    """
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["x:1:x"])
    (tmp_path / "inventory-service").mkdir()
    (tmp_path / "billing-service").mkdir()
    inv = tmp_path / "inventory-service" / "models.py"
    inv.write_text("\n".join(["x = 1"] * 234 + ["class Party(Base):", '    """L1."""']),
                    encoding="utf-8")
    (tmp_path / "billing-service" / "models.py").write_text("x = 1\n", encoding="utf-8")
    _mock_rg_files(monkeypatch, ["inventory-service/models.py", "billing-service/models.py"])

    answer = (
        "As defined in `inventory-service/models.py`, the Party model is at:\n"
        '**File:** `models.py`, **Line:** 236  \n> `"""L1."""`'
    )

    report = verify.verify_claims(answer)

    assert "AMBIGUOUS" not in report
    assert "MISMATCH" not in report
    assert "content verified" in report


def test_bare_filename_with_no_disambiguating_hint_stays_ambiguous(tmp_path, monkeypatch):
    # Regression guard: without the answer ever naming a full path for this
    # basename, ambiguity must still be reported rather than guessed at.
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["x:1:x"])
    (tmp_path / "inventory-service").mkdir()
    (tmp_path / "billing-service").mkdir()
    (tmp_path / "inventory-service" / "models.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "billing-service" / "models.py").write_text("x = 1\n", encoding="utf-8")
    _mock_rg_files(monkeypatch, ["inventory-service/models.py", "billing-service/models.py"])

    answer = "**File:** `models.py`, **Line:** 1"

    report = verify.verify_claims(answer)

    assert "AMBIGUOUS" in report


def test_quote_across_a_markdown_heading_is_not_paired_with_the_citation(tmp_path, monkeypatch):
    """Confirmed live 2026-08-05: a fully CORRECT citation --
    `API/inventory-service/models.py:293` -- was immediately followed by a new
    markdown section ("## Introduction History") introducing an unrelated commit
    hash in backticks. The quote-pairing window doesn't stop at section boundaries,
    so the commit hash got treated as this citation's claimed content and reported
    a MISMATCH the citation never actually made -- a false positive on an answer
    that was, for this citation, entirely correct.
    """
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["x:1:x"])
    lines = ["x = 1"] * 292 + ["class PartyLocation(Base):"] + ["x = 1"] * 5
    (tmp_path / "models.py").write_text("\n".join(lines), encoding="utf-8")

    answer = (
        "- **File citation**: `models.py:293`\n\n"
        "## Introduction History\n"
        "The Parties module was introduced in commit `af635cc`."
    )

    report = verify.verify_claims(answer)

    assert "MISMATCH" not in report
    assert "LINE 293" in report


def test_self_backtick_wrapped_citation_does_not_pair_with_later_unrelated_quote(
    tmp_path, monkeypatch
):
    """Confirmed live 2026-08-05: a real answer wrote

        No existing symbol named `useGetPartyByIdQuery`, `getPartyById`, or
        `inventoryApi.ts:101` exists in the codebase -- the `getParty` endpoint...

    _FILE_LINE_RE matches "inventoryApi.ts:101" INSIDE its own backticks; pos then
    lands right before that citation's own closing backtick. The old quote search
    started there anyway, treated that closing backtick as an OPENING one, and
    paired it with the next real backtick (before `getParty`) -- capturing the
    plain prose "exists in the codebase -- the" as if it were the citation's
    quoted content, then correctly-but-uselessly reporting that prose fragment
    as not found near line 101. Nothing about this citation was quoting anything.
    """
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["x:1:x"])
    lines = ["x = 1"] * 100 + ["export interface Party {"] + ["x = 1"] * 5
    (tmp_path / "inventoryApi.ts").write_text("\n".join(lines), encoding="utf-8")

    answer = (
        "No existing symbol named `useGetPartyByIdQuery`, `getPartyById`, or "
        "`inventoryApi.ts:101` exists in the codebase -- the `getParty` endpoint "
        "is the only one for parties."
    )

    report = verify.verify_claims(answer)

    assert "MISMATCH" not in report
