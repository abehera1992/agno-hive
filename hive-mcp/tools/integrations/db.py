"""Read-only SQL — generic Postgres introspection + SELECT for grounding.

Gives hive a first-class way to VERIFY facts against the live database instead of
grepping files. A value stored in a table (a count, a current column value) is ground
truth only in the table — seed/migration/code text can be stale or incomplete. Two tools:

  db_schema(table=None) — list schemas/tables, or describe one table's columns
  db_query(sql)         — run a single read-only SELECT / WITH / EXPLAIN and return rows

Generic: registered only when HIVE_DB_URL is set (a DSN for a read-only DB role). The
tool holds NO project/schema knowledge — the access boundary is the DB role's grants,
not this code. Point it at any project's DB by changing HIVE_DB_URL.

Safety (defense in depth):
  - connect as a read-only role (recommended: member of pg_read_all_data with
    default_transaction_read_only = on) — the DB itself refuses any write
  - the psycopg connection is forced read_only; a per-call statement_timeout is set
  - single-statement allowlist (SELECT / WITH / EXPLAIN / TABLE / VALUES / SHOW),
    no ';' chaining of multiple statements
  - result rows capped at HIVE_DB_MAX_ROWS
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

try:
    import psycopg
except ImportError:  # image without the driver — tools stay registered but return a clear error
    psycopg = None

_ALLOWED = re.compile(r"^\s*(with|select|explain|table|values|show)\b", re.IGNORECASE)


_RELATION_MISSING_RE = re.compile(
    r'relation "([A-Za-z_][A-Za-z0-9_]*)" does not exist', re.IGNORECASE)


def _err(msg: str, conn=None) -> str:
    """Format a db error, adding the schema the table actually lives in when we can.

    Postgres answers an unqualified name against search_path only, so
    `SELECT COUNT(*) FROM parties` on a database whose table is `inventory.parties`
    comes back as 'relation "parties" does not exist'. That is technically true and
    reads as "the table is absent", which is how battery T8 concluded the parties
    table does not exist in a database where it exists with 0 rows -- a false absence
    claim faithfully reported from a real tool error.

    The fix belongs here rather than in a prompt: the catalogue knows the answer, and
    an unqualified miss is exactly when to look it up. Same shape as the filesystem
    near-miss hints -- a dead end that names the real location instead of implying
    nothing is there.

    Never raises: a hint is a nicety, and a failure to produce one must not replace a
    real error message with a traceback.
    """
    base = f"db error: {msg}"
    match = _RELATION_MISSING_RE.search(msg or "")
    if not match or conn is None:
        return base
    table = match.group(1)
    try:
        # The failing statement leaves the transaction aborted, so every further query
        # on this connection errors until it is rolled back. Read-only throughout, so
        # the rollback discards nothing.
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "select table_schema from information_schema.tables "
                "where table_name = %s order by table_schema limit 3", (table,))
            schemas = [r[0] for r in cur.fetchall()]
    except Exception:
        return base
    if not schemas:
        return (f"{base}\n-- no table named '{table}' exists in ANY schema of this "
                f"database, so this is a genuine absence, not a search_path miss.")
    qualified = ", ".join(f"{s}.{table}" for s in schemas)
    return (f"{base}\n-- '{table}' DOES exist, in another schema: {qualified}. The "
            f"query used an unqualified name, which Postgres resolves against "
            f"search_path only. Re-run it schema-qualified before concluding "
            f"anything is missing.")


def _connect():
    """Open a forced-read-only connection. Returns (conn, None) or (None, error_str)."""
    if psycopg is None:
        return None, "psycopg not installed in the hive-mcp image"
    if not config.HIVE_DB_URL:
        return None, "HIVE_DB_URL not configured"
    try:
        conn = psycopg.connect(config.HIVE_DB_URL, connect_timeout=10, autocommit=False)
        conn.read_only = True  # SET SESSION CHARACTERISTICS ... READ ONLY (before any txn)
        return conn, None
    except Exception as e:
        return None, f"connection failed: {e}"


def _render(cols, rows, truncated) -> str:
    if not cols:
        return "(no result set)"
    lines = [" | ".join(cols)]
    for r in rows:
        lines.append(" | ".join("NULL" if v is None else str(v) for v in r))
    note = f"\n[{len(rows)} row(s)]" + (" (truncated — refine with a tighter query)" if truncated else "")
    return "\n".join(lines) + note


def db_query(sql: str) -> str:
    """
    Run ONE read-only SQL query against the configured database and return the rows.

    Use this to VERIFY any fact that lives in the database — counts, totals, "how many
    rows have value X", the current value of a column — instead of grepping files. The
    live table is the source of truth; file greps (seed / migration / code fallback) are
    only supplementary and may be stale or incomplete.

    Only a single read query is allowed: SELECT / WITH … SELECT / EXPLAIN / TABLE / VALUES
    / SHOW. Writes and DDL are rejected here AND blocked by the database role. Results are
    capped at HIVE_DB_MAX_ROWS rows. Call db_schema first to confirm exact table/column
    names — do not guess them from code.

    Args:
        sql: a single read statement (no ';'-separated multiple statements).
    """
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return _err("empty query")
    if ";" in s:
        return _err("only a single statement is allowed (no ';' chaining)")
    if not _ALLOWED.match(s):
        return _err("only read queries allowed (must start with SELECT / WITH / EXPLAIN / TABLE / VALUES / SHOW)")
    conn, cerr = _connect()
    if cerr:
        return _err(cerr)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(config.HIVE_DB_TIMEOUT_MS)}")
            cur.execute(s)
            cols = [d.name for d in cur.description] if cur.description else []
            cap = int(config.HIVE_DB_MAX_ROWS)
            rows = cur.fetchmany(cap + 1) if cols else []
            truncated = len(rows) > cap
            return _render(cols, rows[:cap], truncated)
    except Exception as e:
        # conn passed so a missing-relation error can name the schema the table is
        # really in -- see _err.
        return _err(str(e).strip(), conn)
    finally:
        conn.close()


def db_schema(table: str | None = None) -> str:
    """
    Inspect the database structure so a query can be grounded on real names.

    - No argument: list every schema.table in the database (system schemas excluded).
    - With a table ('schema.table' or a bare table name): list its columns, types, and
      nullability.

    Use this BEFORE db_query to confirm the exact schema, table, and column names rather
    than guessing them from code.

    Args:
        table: optional 'schema.table' or bare table name to describe.
    """
    conn, cerr = _connect()
    if cerr:
        return _err(cerr)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(config.HIVE_DB_TIMEOUT_MS)}")
            if not table:
                cur.execute(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                    "ORDER BY table_schema, table_name"
                )
                rows = cur.fetchall()
                if not rows:
                    return "(no tables)"
                return "\n".join(f"{r[0]}.{r[1]}" for r in rows) + f"\n[{len(rows)} table(s)]"
            if "." in table:
                sch, tbl = table.split(".", 1)
                cur.execute(
                    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                    (sch, tbl),
                )
                rows = cur.fetchall()
                if not rows:
                    return f"(no such table: {table})"
                body = "\n".join(f"{r[0]} | {r[1]} | {r[2]}" for r in rows)
                return f"{table}:\ncolumn | type | nullable\n{body}"
            cur.execute(
                "SELECT table_schema, column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name=%s ORDER BY table_schema, ordinal_position",
                (table,),
            )
            rows = cur.fetchall()
            if not rows:
                return f"(no such table: {table})"
            body = "\n".join(f"{r[0]} | {r[1]} | {r[2]} | {r[3]}" for r in rows)
            return f"{table} (found in one or more schemas):\nschema | column | type | nullable\n{body}"
    except Exception as e:
        return _err(str(e).strip())
    finally:
        conn.close()
