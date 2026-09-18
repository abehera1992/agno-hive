"""Phase AH, fix 1 -- _blocks_not_in_their_file must not pick a NEGATED filename as
the block's attributed file.

Live incident (Phase AH, T3, fresh-session reproduction): an answer wrote

    "...the `BusinessProfile` model, defined in `API/business-service/models.py`
    (not `business_profile.py` as might be expected):"
    ```python
    class BusinessProfile(BaseModel):
        business_id: str
        ...
    ```

and then showed a completely fabricated class -- wrong base (`BaseModel` instead of
the real `Base`/SQLAlchemy declarative style), wrong field-declaration syntax (bare
type annotations instead of `Column(...)`). verify_claims ran and returned
bad=False, "clean -- nothing flagged", even though every one of the 9 distinctive
lines in that fenced block is genuinely absent from the real file.

Root cause: `_blocks_not_in_their_file` picked `names[-1]` -- the LAST filename
textually mentioned in the run-up -- as the file to check the block against.
Here the last-mentioned name is `business_profile.py`, which the answer's own
words explicitly rule out ("not ... as might be expected") and which does not
exist in the project at all. `_resolve_path` correctly failed to resolve it, and
the function bailed out via its own `if resolved is None: continue` -- never
reaching the real, earlier-named, correctly-cited `API/business-service/models.py`
at all.

Fix: prefer the last NON-negated filename (reusing `_is_negated_claim`, the same
primitive already used for backticked identifier claims elsewhere in this file),
falling back to the old `names[-1]` behavior only when every candidate in the
run-up is negated.

All tests here build a synthetic PROJECT_ROOT (pytest's tmp_path) rather than
depending on a real, mounted EkamApp checkout, so they run the same way in CI as
in a live deployment.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _project_root(tmp_path, monkeypatch):
    """A minimal synthetic repo: one real file at the real relative path this
    incident cites, with REAL content that does NOT contain the fabricated class
    body (mirroring the true shape: real SQLAlchemy `Base`/`Column`, not the
    fabricated Pydantic `BaseModel`/bare-annotation shape)."""
    real_dir = tmp_path / "API" / "business-service"
    real_dir.mkdir(parents=True)
    (real_dir / "models.py").write_text(
        'class BusinessProfile(Base):\n'
        '    __tablename__ = "business_profiles"\n'
        "    business_id = Column(UUID(as_uuid=True), primary_key=True)\n"
        "    user_id = Column(UUID(as_uuid=True), nullable=False)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    verify._checked_answer_counts = {}
    return tmp_path


def test_the_exact_t3_live_failure_now_produces_a_finding():
    answer = (
        "The primary database entity for seller verification is the "
        "`BusinessProfile` model, defined in `API/business-service/models.py` "
        "(not `business_profile.py` as might be expected):\n\n"
        "```python\n"
        "class BusinessProfile(BaseModel):\n"
        "    business_id: str\n"
        "    user_id: str\n"
        "    legal_name: str\n"
        "    business_pan: str\n"
        "    gstin: str\n"
        "    business_type: BusinessType\n"
        "    status: BusinessStatus  # REGISTERED, PENDING_VERIFICATION\n"
        "    # ... other fields ...\n"
        "```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    resolved, sample, n = findings[0]
    assert resolved == "API/business-service/models.py"
    assert n == 9


def test_full_verify_claims_report_now_flags_it():
    answer = (
        "The primary database entity for seller verification is the "
        "`BusinessProfile` model, defined in `API/business-service/models.py` "
        "(not `business_profile.py` as might be expected):\n\n"
        "```python\n"
        "class BusinessProfile(BaseModel):\n"
        "    business_id: str\n"
        "    user_id: str\n"
        "    legal_name: str\n"
        "    business_pan: str\n"
        "    gstin: str\n"
        "    business_type: BusinessType\n"
        "    status: BusinessStatus  # REGISTERED, PENDING_VERIFICATION\n"
        "    # ... other fields ...\n"
        "```\n"
    )
    report = verify.verify_claims(answer)
    assert "QUOTED BLOCKS" in report
    assert "BLOCK NOT IN API/business-service/models.py" in report
    assert "could NOT be found" in report


def test_positive_case_unaffected_last_name_still_wins_when_not_negated(tmp_path, monkeypatch):
    """No negation anywhere -- the pre-existing behavior (last named file wins)
    must be completely unchanged."""
    other_dir = tmp_path / "API" / "other"
    other_dir.mkdir(parents=True)
    (other_dir / "unrelated.py").write_text("class Unrelated:\n    pass\n", encoding="utf-8")

    answer = (
        "Defined across two related files, `API/other/unrelated.py` and finally "
        "`API/business-service/models.py`:\n\n"
        "```python\n"
        "class BusinessProfile(BaseModel):\n"
        "    totally_fake_field_xyz: str\n"
        "    another_fake_field_abc: str\n"
        "```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "API/business-service/models.py"


def test_negation_of_a_nonexistent_file_does_not_block_the_real_earlier_file_reversed_order():
    """Same shape, opposite order -- the negated name appears FIRST, the real one
    LAST. Must still resolve to the real one (this order already worked before the
    fix, by coincidence of names[-1]; pinned here so the fix cannot regress it)."""
    answer = (
        "Not `business_profile.py` as might be expected, but "
        "`API/business-service/models.py` defines it:\n\n"
        "```python\n"
        "class BusinessProfile(BaseModel):\n"
        "    totally_fake_field_xyz: str\n"
        "    another_fake_field_abc: str\n"
        "```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "API/business-service/models.py"


def test_all_candidates_negated_falls_back_to_old_behavior_not_a_crash():
    """Edge case the docstring calls out: if every filename in the run-up is
    negated, there is nothing better to prefer -- fall back to names[-1] rather
    than silently finding nothing (matches pre-fix behavior for this rare shape,
    never worse than before). Neither foo.py nor bar.py exist in the synthetic
    project root, so _resolve_path fails for the fallback target too -- the only
    requirement is that this does not raise."""
    answer = (
        "Not `foo.py`, and not `bar.py` either:\n\n"
        "```python\n"
        "class Whatever(BaseModel):\n"
        "    nonexistent_field_one: str\n"
        "    nonexistent_field_two: str\n"
        "```\n"
    )
    verify._blocks_not_in_their_file(answer)  # must not raise


def test_real_correctly_quoted_block_is_not_a_false_positive():
    """A block that genuinely matches its cited (non-negated) file must not be
    flagged -- this is the existing, unmodified 'at least one line matches -> stay
    silent' rule, confirmed still intact after the fix."""
    answer = (
        "Defined in `API/business-service/models.py` (not `made_up_file.py` as "
        "might be expected):\n\n"
        "```python\n"
        "class BusinessProfile(Base):\n"
        "    __tablename__ = \"business_profiles\"\n"
        "```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert findings == []
