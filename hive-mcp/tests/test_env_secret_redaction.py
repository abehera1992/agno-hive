"""Security regression tests -- incident 2026-09-30.

get_env_info()'s old redaction was a name-substring-only denylist
(SECRET/PASSWORD/TOKEN/KEY/PRIVATE). It correctly caught GPG_KEY (via "KEY")
but a live production run leaked DB_CONN_URL and HIVE_DB_URL in full --
neither variable name contains any of those words, so a Postgres connection
string with an embedded password (postgresql://hive_ro:<password>@host:port/db)
streamed straight into the model's context and the systemd journal.

These tests use ONLY synthetic, obviously-fake credentials (no real value from
the incident appears anywhere in this file) and assert on shell.py's real
`_is_sensitive_env` / `get_env_info` -- not a reimplementation of the check.
"""
import os

from tools.shell import _is_sensitive_env, get_env_info

_SYNTHETIC_DB_SECRET = "postgresql://hive_ro:SYNTHETIC-NOT-REAL-abc123XYZ@host.docker.internal:5433/ekamApp"
_SYNTHETIC_REDIS_SECRET = "redis://default:SYNTHETIC-NOT-REAL-redispw@localhost:6379/0"
_SYNTHETIC_GPG = "SYNTHETIC-NOT-REAL-GPG-KEY-VALUE"


# ── A. DB connection strings are redacted, regardless of variable name ──────

def test_db_conn_url_value_is_redacted():
    assert _is_sensitive_env("DB_CONN_URL", _SYNTHETIC_DB_SECRET) is True


def test_hive_db_url_value_is_redacted():
    assert _is_sensitive_env("HIVE_DB_URL", _SYNTHETIC_DB_SECRET) is True


def test_a_differently_named_var_with_a_credential_shaped_value_is_still_redacted():
    """The value-shape check must catch a credential-embedded URL under ANY
    variable name -- not just the two names this incident happened to expose.
    REDIS_URL was never part of the incident and is not in any name list."""
    assert _is_sensitive_env("REDIS_URL", _SYNTHETIC_REDIS_SECRET) is True
    assert _is_sensitive_env("SOME_RANDOMLY_NAMED_VAR", _SYNTHETIC_DB_SECRET) is True


def test_common_db_connection_variable_names_are_redacted_by_name_alone():
    """2026-10-01 remediation: explicitly prove the acceptance-criteria name
    list is covered by NAME classification alone (not relying on the
    value-shape fallback), so a connection variable is redacted even before
    any value is ever assigned to it (e.g. an empty/unset-but-declared var)."""
    for name in ("DB_CONN_URL", "DATABASE_URL", "POSTGRES_URL", "DB_URL", "DSN"):
        assert _is_sensitive_env(name, "synthetic-secret") is True, name


def test_full_get_env_info_output_never_contains_the_synthetic_db_secret(monkeypatch):
    """End-to-end: the actual returned tool output, not just the classifier."""
    monkeypatch.setenv("DB_CONN_URL", _SYNTHETIC_DB_SECRET)
    monkeypatch.setenv("HIVE_DB_URL", _SYNTHETIC_DB_SECRET)

    output = get_env_info()

    assert "SYNTHETIC-NOT-REAL-abc123XYZ" not in output
    assert _SYNTHETIC_DB_SECRET not in output
    assert "DB_CONN_URL=<redacted>" in output
    assert "HIVE_DB_URL=<redacted>" in output


# ── B. Existing sensitive variables remain redacted ──────────────────────────

def test_gpg_key_still_redacted_by_name():
    assert _is_sensitive_env("GPG_KEY", _SYNTHETIC_GPG) is True


def test_existing_name_patterns_still_redacted():
    for name in ("API_SECRET", "DB_PASSWORD", "AUTH_TOKEN", "STRIPE_PRIVATE_KEY"):
        assert _is_sensitive_env(name, "anything") is True


# ── C. Ordinary non-sensitive variables remain visible ───────────────────────

def test_ordinary_variables_are_not_redacted():
    for name, value in (
        ("PROJECT_ROOT", "/project"),
        ("MCP_PORT", "9000"),
        ("LANG", "C.UTF-8"),
        ("EXCLUDE_DIRS", "signoz,graphify-out"),
        ("APP_ENV", "development"),
        ("LOG_LEVEL", "INFO"),
    ):
        assert _is_sensitive_env(name, value) is False


def test_get_env_info_does_not_over_redact_ordinary_values(monkeypatch):
    monkeypatch.setenv("SOME_HARMLESS_FLAG", "true")
    output = get_env_info()
    assert "SOME_HARMLESS_FLAG=true" in output


def test_a_url_without_embedded_credentials_is_not_redacted_by_value_shape():
    """https://example.com/path has no userinfo -- must not false-positive
    on every plain URL, only ones carrying scheme://user:pass@ credentials."""
    assert _is_sensitive_env("DOCS_SITE", "https://example.com/path") is False


# ── D. Case / naming variants ────────────────────────────────────────────────

def test_case_insensitive_name_matching():
    assert _is_sensitive_env("db_conn_url", _SYNTHETIC_DB_SECRET) is True
    assert _is_sensitive_env("Hive_Db_Url", _SYNTHETIC_DB_SECRET) is True


def test_other_db_credential_naming_variants_are_covered_by_value_shape():
    for name in ("MONGO_URI", "AMQP_URL", "SOME_SERVICE_DSN", "ARBITRARY_NAME"):
        assert _is_sensitive_env(name, _SYNTHETIC_DB_SECRET) is True


# ── E. No secret reaches the returned output under realistic env shape ──────

def test_realistic_mixed_environment_leaks_nothing(monkeypatch):
    """A monkeypatched os.environ shaped like the real incident -- sensitive
    and ordinary vars interleaved -- proves no secret value survives into the
    joined output string, not just that individual keys classify correctly."""
    monkeypatch.setenv("DB_CONN_URL", _SYNTHETIC_DB_SECRET)
    monkeypatch.setenv("HIVE_DB_URL", _SYNTHETIC_DB_SECRET)
    monkeypatch.setenv("GPG_KEY", _SYNTHETIC_GPG)
    monkeypatch.setenv("NOTION_API_KEY", "SYNTHETIC-NOT-REAL-notion-token")
    monkeypatch.setenv("PROJECT_ROOT", "/project")
    monkeypatch.setenv("MCP_PORT", "9000")

    output = get_env_info()

    for secret_fragment in (
        "SYNTHETIC-NOT-REAL-abc123XYZ", "SYNTHETIC-NOT-REAL-GPG-KEY-VALUE",
        "SYNTHETIC-NOT-REAL-notion-token",
    ):
        assert secret_fragment not in output
    assert "/project" in output  # ordinary values still visible
    assert "9000" in output


# ── F. No source-value dependency -- every test above uses only synthetic values ──
# (structural: no string in this file matches any value ever set for the real
# hive_ro role during this incident -- confirmed by inspection, not asserted
# programmatically, since the test process must never hold the real value.)
