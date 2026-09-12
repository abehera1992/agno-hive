"""Phase 3 (AGNOHive Reliability Program): T8 schema -> query grounding.

Forensic trace (three live battery runs, R4/R5/R6 T8 -- ZGX journal +
/sessions/{id} transcripts, not simulated): the coordinator called
db_schema('parties'), got back real column rows (schema=inventory, columns
including party_id, tenant_id, name, pan, ...) but the response never stated
the single string "inventory.parties" the caller actually needed for a
follow-up db_query call -- only the bare name in the header and the schema
repeated per COLUMN ROW. All three runs then constructed
db_query("... FROM inventory.party_id") (mistaking the COLUMN name party_id
for a table) and/or "... FROM inventory.party" (a guessed singular), both of
which failed with an accurate "relation does not exist" from Postgres -- and
concluded the parties table does not exist, when it does, with 0 rows, and
had already been named in the schema evidence the run held. The tool error
was accurate; the query TARGET was wrong; the schema evidence needed to build
the right target was already in hand and never surfaced as a single, copyable
string.

These tests exercise db_schema()'s bare-table-name branch directly against a
fake psycopg-shaped connection, reproducing the exact real column set from
the incident, plus regression coverage for the schema-qualified branch, the
full listing, and db_query's own existing error-hint behaviour -- none of
which this phase touches.
"""
from tools.integrations import db


class _Col:
    """Stand-in for a psycopg cursor.description entry -- only `.name` is used."""
    def __init__(self, name):
        self.name = name


class _FakeCursor:
    """Mimics psycopg's context-managed cursor for exactly the query shapes
    db_schema()/db_query() issue. Dispatches on a normalised, whitespace-
    collapsed uppercase copy of the SQL text -- no ORM, no real driver."""

    def __init__(self, *, bare_columns=None, qualified_columns=None,
                 full_listing=None, query_result=None, query_error=None):
        self.bare_columns = bare_columns or {}          # {table_name: [(schema, col, type, null), ...]}
        self.qualified_columns = qualified_columns or {}  # {(schema, table): [(col, type, null), ...]}
        self.full_listing = full_listing or []           # [(schema, table), ...]
        self.query_result = query_result                 # (cols, rows) for a plain SELECT
        self.query_error = query_error                   # Exception to raise on a plain SELECT
        self._current_cols = None
        self._current_rows = []
        self.description = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = " ".join(sql.split()).upper()
        if s.startswith("SET STATEMENT_TIMEOUT"):
            self.description = None
            self._current_rows = []
            return
        if "TABLE_SCHEMA=%S AND TABLE_NAME=%S" in s:
            sch, tbl = params
            self._current_rows = self.qualified_columns.get((sch, tbl), [])
            self.description = None
            return
        if "WHERE TABLE_NAME=%S" in s:
            (table,) = params
            self._current_rows = self.bare_columns.get(table, [])
            self.description = None
            return
        if "INFORMATION_SCHEMA.TABLES" in s:
            self._current_rows = self.full_listing
            self.description = None
            return
        # Anything else is a db_query-issued SELECT.
        if self.query_error is not None:
            raise self.query_error
        cols, rows = self.query_result or ([], [])
        self._current_rows = rows
        self.description = [_Col(c) for c in cols]

    def fetchall(self):
        return list(self._current_rows)

    def fetchmany(self, n):
        return list(self._current_rows)[:n]


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _patch_connect(monkeypatch, cursor):
    monkeypatch.setattr(db, "_connect", lambda: (_FakeConn(cursor), None))


# The exact real column set from the live T8 incident (schema=inventory), captured
# verbatim from the ZGX journal's db_schema('parties') result_preview.
REAL_PARTIES_COLUMNS = [
    ("inventory", "party_id", "uuid", "NO"),
    ("inventory", "tenant_id", "uuid", "NO"),
    ("inventory", "name", "character varying", "NO"),
    ("inventory", "pan", "character varying", "YES"),
    ("inventory", "party_type", "character varying", "NO"),
]


# ── 1. correct schema + correct table → query proceeds ─────────────────────

def test_1_correct_qualified_table_query_proceeds(monkeypatch):
    cursor = _FakeCursor(query_result=(["count"], [(0,)]))
    _patch_connect(monkeypatch, cursor)
    out = db.db_query("SELECT COUNT(*) FROM inventory.parties;")
    assert "count" in out
    assert "0" in out
    assert "1 row(s)" in out


# ── 2. correct schema + nonexistent/wrong table → deterministic repair ─────

def test_2a_bare_lookup_on_a_real_table_now_states_the_qualified_name(monkeypatch):
    """The fix itself: db_schema('parties') must now state 'inventory.parties'
    as a single, literal, copyable string -- not just the bare name in the
    header and the schema scattered across per-column rows."""
    cursor = _FakeCursor(bare_columns={"parties": REAL_PARTIES_COLUMNS})
    _patch_connect(monkeypatch, cursor)
    out = db.db_schema("parties")
    assert "inventory.parties" in out
    assert "'parties' resolves to: inventory.parties" in out


def test_2b_reproduces_the_real_t8_failure_shape_pre_fix_would_have_missed_this(monkeypatch):
    """Before this fix, nothing in db_schema's own output ever printed
    'inventory.parties' as one string -- only 'inventory' (per column row) and
    'parties' (bare, in the header) separately. This test pins that the FIX
    closes exactly that gap, using the real incident's own column data."""
    cursor = _FakeCursor(bare_columns={"parties": REAL_PARTIES_COLUMNS})
    _patch_connect(monkeypatch, cursor)
    out = db.db_schema("parties")
    # The exact two wrong guesses the live runs made must be distinguishable
    # from the real answer: a reader (model or human) told "inventory.parties"
    # explicitly has no reason to try "inventory.party_id" (a column) or
    # "inventory.party" (a guess) instead.
    assert "inventory.party_id" not in out.split("\n")[0]
    assert "inventory.party\n" not in out
    assert out.startswith("'parties' resolves to: inventory.parties")
    assert "none of the column names listed below are table names" in out


def test_2c_db_query_against_the_wrong_guessed_names_still_errors_accurately(monkeypatch):
    """Unmodified behaviour: db_query itself is not changed by this fix --
    calling it with either wrong guess must still fail exactly as it did
    live, with Postgres' own accurate error text."""
    cursor = _FakeCursor(
        query_error=Exception('relation "inventory.party_id" does not exist'))
    _patch_connect(monkeypatch, cursor)
    out = db.db_query("SELECT COUNT(*) FROM inventory.party_id;")
    assert "does not exist" in out
    assert out.startswith("db error:")


def test_2d_bare_unqualified_query_still_gets_the_existing_err_schema_hint(monkeypatch):
    """Regression: db_query's own pre-existing _err() schema-lookup hint (for
    a genuinely UNQUALIFIED miss like bare 'parties') is untouched by this
    phase -- still fires exactly as before."""
    cursor = _FakeCursor(
        query_error=Exception('relation "parties" does not exist'),
        full_listing=[("inventory", "parties")],
    )
    # _err() re-queries information_schema.tables by bare table name after the
    # error -- reuse the same fake cursor's "INFORMATION_SCHEMA.TABLES" branch,
    # matched loosely enough to also serve _err()'s narrower table_name= query.
    def execute(self, sql, params=None):
        s = " ".join(sql.split()).upper()
        if "TABLE_SCHEMA FROM INFORMATION_SCHEMA.TABLES" in s:
            self._current_rows = [("inventory",)]
            self.description = None
            return
        return _FakeCursor.execute(self, sql, params)
    cursor.execute = execute.__get__(cursor, _FakeCursor)
    _patch_connect(monkeypatch, cursor)
    out = db.db_query("SELECT COUNT(*) FROM parties;")
    assert "DOES exist, in another schema: inventory.parties" in out


# ── 3. similarly named valid table → correct identifier preserved ──────────

def test_3_similarly_named_column_is_not_offered_as_the_table_identifier(monkeypatch):
    """party_id (a column) must never appear as if it were a candidate table
    name in the qualified-name line -- only the real table name is."""
    cursor = _FakeCursor(bare_columns={"parties": REAL_PARTIES_COLUMNS})
    _patch_connect(monkeypatch, cursor)
    out = db.db_schema("parties")
    resolves_line = out.splitlines()[0]
    assert resolves_line == "'parties' resolves to: inventory.parties"
    assert "party_id" not in resolves_line


def test_3b_multiple_schemas_owning_the_same_bare_table_name_are_all_listed(monkeypatch):
    """If two DIFFERENT schemas both happen to have a table with this bare
    name, both real qualified names must be stated -- not silently picking
    one, and not omitting either."""
    cols = [
        ("inventory", "id", "uuid", "NO"),
        ("archive", "id", "uuid", "NO"),
    ]
    cursor = _FakeCursor(bare_columns={"parties": cols})
    _patch_connect(monkeypatch, cursor)
    out = db.db_schema("parties")
    assert "archive.parties" in out
    assert "inventory.parties" in out
    assert out.startswith("'parties' resolves to: archive.parties, inventory.parties")


# ── 4. schema-qualified identifier handling (existing branch, unmodified) ──

def test_4_schema_qualified_lookup_branch_is_unchanged(monkeypatch):
    cursor = _FakeCursor(
        qualified_columns={("inventory", "parties"):
                            [("party_id", "uuid", "NO"), ("name", "character varying", "NO")]})
    _patch_connect(monkeypatch, cursor)
    out = db.db_schema("inventory.parties")
    assert out.startswith("inventory.parties:\ncolumn | type | nullable")
    assert "party_id | uuid | NO" in out


# ── 5. identifier normalization (none currently supported) ─────────────────

def test_5_no_case_normalization_is_performed_bare_lookup(monkeypatch):
    """Documents the current, unmodified behaviour: table_name is matched
    exactly as given (Postgres/information_schema.columns' own case rules),
    no upper/lower-casing is applied by this tool. Not something this phase
    adds or claims to fix."""
    cursor = _FakeCursor(bare_columns={"parties": REAL_PARTIES_COLUMNS})
    _patch_connect(monkeypatch, cursor)
    out = db.db_schema("Parties")  # wrong case
    assert out == "(no such table: Parties)"


# ── 6. T8 reproduction: no such table at all (genuine absence, unchanged) ──

def test_6_genuinely_missing_table_is_still_reported_as_missing(monkeypatch):
    cursor = _FakeCursor(bare_columns={})
    _patch_connect(monkeypatch, cursor)
    out = db.db_schema("not_a_real_table")
    assert out == "(no such table: not_a_real_table)"


# ── 7. non-T8 path: the full listing (no table arg) is unchanged ───────────

def test_7_full_listing_unchanged(monkeypatch):
    cursor = _FakeCursor(full_listing=[("inventory", "parties"), ("auth", "roles")])
    _patch_connect(monkeypatch, cursor)
    out = db.db_schema()
    assert "inventory.parties" in out
    assert "auth.roles" in out
    assert "2 table(s)" in out
