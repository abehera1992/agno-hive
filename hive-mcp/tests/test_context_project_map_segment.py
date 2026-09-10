"""_segment_carries must match a name that carries its own separator.

project_map's bucketing step is this helper's only caller. It compared the WHOLE name
against a set of single-word tokens, so any name containing '-', '_' or '.' could never
match the segment that literally spells it:

    _segment_carries("inventory-service", "inventory-service")  ->  False
    _segment_carries("vouchers_api.py",   "vouchers_api.py")    ->  False

project_map therefore answered "no directory or file in this repository carries X" for
paths find_files located immediately. Because project_map is the coordinator's only
tool, being told its target did not exist sent it round name variants until its 60-call
budget was spent: two Phase-2 control pilots (I1, I4, both naming vouchers_api.py)
recorded 59 project_map calls and zero delegations.

The fix matches the name as a CONTIGUOUS RUN of tokens. A run of length one is exactly
the old membership test, so the single-token behaviour this helper was written for --
including its anti-substring guard -- is preserved rather than traded away. Both halves
are asserted below, because the guard is the reason the helper exists.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from tools.context import _segment_carries  # noqa: E402


@pytest.mark.parametrize("segment,name", [
    # The regression: a name carrying its own separator, against the segment that IS it.
    ("inventory-service", "inventory-service"),
    ("vouchers_api.py", "vouchers_api.py"),
    ("vouchers_api.py", "vouchers_api"),      # stem of a real file
    ("business-service", "business-service"),
    ("inventoryApi.ts", "inventoryApi.ts"),   # camelCase + extension
])
def test_multi_token_name_matches_the_segment_that_spells_it(segment, name):
    assert _segment_carries(segment, name) is True


@pytest.mark.parametrize("segment,name", [
    # Single-token behaviour, unchanged by the fix -- a run of length 1 is membership.
    ("inventory-service", "inventory"),
    ("inventory-service", "service"),
    ("vouchers_api.py", "vouchers"),
    ("vouchers", "vouchers"),
])
def test_single_token_matching_is_unchanged(segment, name):
    assert _segment_carries(segment, name) is True


@pytest.mark.parametrize("segment,name", [
    # The guard this helper exists for: a substring is not a word. project_map('Party')
    # once resolved into a vendored Go tree on exactly this shape.
    ("thirdpartyapi", "party"),
    ("inventoryservice", "inventory"),
    # Order is part of the claim: the reversed name is a different component.
    ("service-inventory", "inventory-service"),
    # A name whose tokens appear but not adjacently is not this segment.
    ("vouchers_api.py", "vouchers.py"),
    # Unrelated.
    ("payments_api.py", "vouchers_api.py"),
])
def test_non_matches_are_still_rejected(segment, name):
    assert _segment_carries(segment, name) is False


def test_empty_name_is_false():
    assert _segment_carries("anything", "") is False


def test_name_with_no_tokens_is_false():
    """A name of pure punctuation tokenises to nothing and must not match everything --
    an empty run is trivially contiguous, so this is the case the length guard covers."""
    assert _segment_carries("inventory-service", "---") is False
