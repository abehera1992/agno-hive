"""Phase T8-P: DB result-set metadata must never be misread as a returned value.

Root cause (T8, 2026-09-21 post-deployment battery): `SELECT COUNT(*) FROM
inventory.parties` returned the real preview "count\n0\n[1 row(s)]" -- the table
genuinely has 0 rows. The old evidence regex scanned the whole preview for ANY
"<digits> row(s)?" text and matched "1" straight out of the tool's own "[1 row(s)]"
footer (its literal, always-singular "row(s)" marker for the SIZE of the result set),
never reaching the real value on the line above. A correct "0 rows" answer was flagged
as contradicting the database.

_parse_db_row_count_evidence replaces the flat regex scan with a structural read of
hive-mcp's fixed _render() shape (header line, one line per row, then the "[N row(s)]"
footer): a single data row of a single column (the COUNT(*)/aggregate/single-value
shape) reports its own cell value; anything else reports the footer's row count;
nothing recognizable reports nothing (silent, never a false accusation).
"""
from types import SimpleNamespace

from swarm.team import (
    _DB_ROW_FOOTER_RE, _integrity_db_count_contradiction, _parse_db_row_count_evidence,
)


# ── the parser directly ──────────────────────────────────────────────────────────────────

def test_t8_exact_preview_reports_the_real_value_not_the_footer():
    """The exact regression: a correct '0 rows' answer must not be flagged."""
    assert _parse_db_row_count_evidence("count\n0\n[1 row(s)]") == "0"


def test_1_zero_returned_rows_no_data_line():
    assert _parse_db_row_count_evidence("count\n[0 row(s)]") == "0"


def test_1_zero_returned_rows_generic_header():
    assert _parse_db_row_count_evidence("party_id | name\n[0 row(s)]") == "0"


def test_2_one_returned_row_aggregate_value():
    assert _parse_db_row_count_evidence("count\n47\n[1 row(s)]") == "47"


def test_2_one_returned_row_non_numeric_falls_back_to_the_footer():
    assert _parse_db_row_count_evidence("name\nAlice\n[1 row(s)]") == "1"


def test_3_multiple_returned_rows_single_column_uses_the_footer_not_the_first_row():
    """A genuine multi-row result: the footer (the true row count) must win over the
    first data row's own value, which the leftmost-match approach this replaces got
    wrong once re.MULTILINE was added to fix the T8 case."""
    assert _parse_db_row_count_evidence("id\n1\n2\n3\n[3 row(s)]") == "3"


def test_3_multiple_returned_rows_multi_column():
    assert _parse_db_row_count_evidence(
        "id | name\n1 | Alice\n2 | Bob\n[2 row(s)]") == "2"


def test_4_bracketed_1_row_s_metadata_is_never_the_answer_when_a_real_value_exists():
    """The exact bug shape, restated as its own test: '[1 row(s)]' must never win over
    a real data value one line above it."""
    real = _parse_db_row_count_evidence("count\n999\n[1 row(s)]")
    assert real == "999"
    assert real != "1"


def test_5_bracketed_n_row_s_metadata_used_only_when_it_genuinely_is_the_count():
    for n in (0, 1, 5, 100):
        preview = f"id\n" + "\n".join(str(i) for i in range(1, n + 1)) + f"\n[{n} row(s)]"
        if n == 1:
            continue  # single data row -- the aggregate-cell branch legitimately wins
        expected = str(n)
        assert _parse_db_row_count_evidence(preview) == expected, preview


def test_6_actual_count_values_inside_returned_rows_are_distinguished_from_the_footer():
    assert _parse_db_row_count_evidence("count\n0\n[1 row(s)]") == "0"
    assert _parse_db_row_count_evidence("total\n3\n[1 row(s)]") == "3"
    assert _parse_db_row_count_evidence("count\n1000000\n[1 row(s)]") == "1000000"


def test_truncated_footer_suffix_still_parses():
    assert _parse_db_row_count_evidence(
        "id\n1\n2\n[2 row(s)] (truncated — refine with a tighter query)") == "2"


def test_footer_regex_requires_the_literal_singular_row_s_marker():
    assert _DB_ROW_FOOTER_RE.match("[3 row(s)]")
    assert not _DB_ROW_FOOTER_RE.match("[3 table(s)]")   # db_schema's own footer, unrelated
    assert not _DB_ROW_FOOTER_RE.match("[3 rows]")        # never the real shape


def test_db_schema_table_listing_footer_is_not_mistaken_for_a_row_count():
    assert _parse_db_row_count_evidence(
        "auth.users\ninventory.parties\n[2 table(s)]") is None


def test_multi_row_multi_column_with_no_usable_single_value_stays_silent_when_footer_absent():
    assert _parse_db_row_count_evidence("id | name\n1 | Alice\n2 | Bob") is None


def test_empty_and_none_preview():
    assert _parse_db_row_count_evidence("") is None
    assert _parse_db_row_count_evidence(None) is None


def test_thousands_separator_in_the_cell_value():
    assert _parse_db_row_count_evidence("count\n12,473\n[1 row(s)]") == "12473"


# ── 7. existing legitimate DB-count behaviour is unchanged ──────────────────────────────

def test_7_legacy_bare_zero_rows_text_unchanged():
    assert _parse_db_row_count_evidence("0 rows") == "0"


def test_7_legacy_bare_thousands_rows_text_unchanged():
    assert _parse_db_row_count_evidence("12,473 rows") == "12473"


def test_7_legacy_bare_singular_row_text_unchanged():
    assert _parse_db_row_count_evidence("1 row") == "1"


# ── through the real guard entry point (team._tool_evidence), pinning the T8 shape ──────

def test_t8_shape_through_the_real_guard_produces_no_contradiction():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "db_query", "agent": "Researcher", "preview": "count\n0\n[1 row(s)]", "chars": 20},
    ])
    assert _integrity_db_count_contradiction(
        "The parties table has 0 rows.", team) is None


def test_t8_shape_through_the_real_guard_still_catches_a_genuine_contradiction():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "db_query", "agent": "Researcher", "preview": "count\n0\n[1 row(s)]", "chars": 20},
    ])
    found = _integrity_db_count_contradiction(
        "The parties table has 5 rows.", team)
    assert found == ("5", "0")


def test_existing_bare_text_contradiction_test_shape_unchanged():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "db_query", "agent": "Researcher", "preview": "0 rows", "chars": 6},
    ])
    assert _integrity_db_count_contradiction(
        "The parties table contains 12,473 rows.", team) == ("12473", "0")


def test_existing_bare_text_agreement_test_shape_unchanged():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "db_query", "agent": "Researcher", "preview": "0 rows", "chars": 6},
    ])
    assert _integrity_db_count_contradiction(
        "The parties table contains 0 rows.", team) is None


def test_existing_no_db_tool_evidence_test_shape_unchanged():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "get_file_content", "agent": "Researcher", "preview": "x", "chars": 1},
    ])
    assert _integrity_db_count_contradiction(
        "The parties table contains 12,473 rows.", team) is None


def test_multiple_db_evidence_items_the_first_matching_contradiction_wins():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "db_query", "agent": "Researcher", "preview": "count\n7\n[1 row(s)]", "chars": 20},
    ])
    found = _integrity_db_count_contradiction("There are 0 rows.", team)
    assert found == ("0", "7")


def test_db_schema_evidence_naming_table_s_not_row_s_does_not_falsely_contradict():
    """A db_schema() no-argument call's own footer says '[N table(s)]' -- never 'row(s)' --
    so it must never be misread as row-count evidence for an unrelated claim."""
    team = SimpleNamespace(_tool_evidence=[
        {"name": "db_schema", "agent": "Researcher",
         "preview": "auth.users\ninventory.parties\n[2 table(s)]", "chars": 40},
    ])
    assert _integrity_db_count_contradiction(
        "The parties table has 0 rows.", team) is None
