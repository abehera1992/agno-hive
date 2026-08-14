"""Unit tests for the liveness-based auto-kill decision logic (Recommendation #2,
2026-08-13 -- see DOCS.md "Liveness-Based Auto-Kill"). _liveness_kill_reason and
_read_liveness_snapshot are pure/IO-isolated on purpose, so the DECISION can be
tested independent of the subprocess/file mechanics around it --
tests/test_run_worker_subprocess.py covers the actual end-to-end kill via the
fake_worker.py fixture's "stale" mode.

Two tiers, not one generic timeout:
- Tier 1 (backstop): stagnant_seconds -- neither a new tool call nor new stream
  content for config.liveness_silence_threshold_s. Catches a genuine hang this
  file's own _duplicate_read_stub-style tier can't see (e.g. no tool calls at
  all, a hung MCP call outside its own timeout).
- Tier 2 (primary, sharper): max_stub_serve_count -- a model still calling an
  identical read after being told to stop 3+ times (the escalated stub wording)
  is direct evidence of non-convergence, not just silence.
"""
import pytest

from api.server import _liveness_kill_reason, _read_liveness_snapshot
from config.config import config


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    monkeypatch.setattr(config, "liveness_silence_threshold_s", 300.0)
    monkeypatch.setattr(config, "liveness_stub_serve_threshold", 8)
    monkeypatch.setattr(config, "liveness_aggregate_stub_threshold", 15)


# ── _liveness_kill_reason ───────────────────────────────────────────────────────

def test_healthy_snapshot_returns_none():
    snapshot = {"stagnant_seconds": 12.0, "max_stub_serve_count": 2}
    assert _liveness_kill_reason(snapshot) is None


def test_empty_snapshot_returns_none():
    """Missing keys (e.g. a run that hasn't reached its first heartbeat tick
    yet) must default to "healthy," never crash."""
    assert _liveness_kill_reason({}) is None


def test_stagnant_seconds_at_the_threshold_does_not_trigger():
    """Strictly greater-than, not greater-or-equal -- exactly at the configured
    threshold is still healthy, matching the docstring's own '> config...'."""
    snapshot = {"stagnant_seconds": 300.0, "max_stub_serve_count": 0}
    assert _liveness_kill_reason(snapshot) is None


def test_stagnant_seconds_over_the_threshold_triggers():
    snapshot = {"stagnant_seconds": 300.1, "max_stub_serve_count": 0}
    reason = _liveness_kill_reason(snapshot)
    assert reason is not None
    assert "300" in reason


def test_stub_serve_count_at_the_threshold_does_not_trigger():
    snapshot = {"stagnant_seconds": 0, "max_stub_serve_count": 8}
    assert _liveness_kill_reason(snapshot) is None


def test_stub_serve_count_over_the_threshold_triggers():
    snapshot = {"stagnant_seconds": 0, "max_stub_serve_count": 9}
    reason = _liveness_kill_reason(snapshot)
    assert reason is not None
    assert "9" in reason


def test_silence_reason_mentions_the_configured_threshold_value(monkeypatch):
    monkeypatch.setattr(config, "liveness_silence_threshold_s", 60.0)
    snapshot = {"stagnant_seconds": 61.0, "max_stub_serve_count": 0}
    reason = _liveness_kill_reason(snapshot)
    assert "60" in reason


def test_both_signals_unhealthy_still_returns_one_reason_not_a_crash():
    snapshot = {"stagnant_seconds": 999.0, "max_stub_serve_count": 99}
    reason = _liveness_kill_reason(snapshot)
    assert reason is not None


# ── Tier 3: total_stub_serve_count, the aggregate signal (2026-08-14) ──────────
# Closes a real, precisely-measured gap: max_stub_serve_count is the highest
# count for any SINGLE (agent, tool, args) key, so a model rotating between
# several already-stubbed files can keep every individual key just under the
# Tier-2 threshold while the run is just as stuck in aggregate. Confirmed live:
# a real run rotated between 3 files (6-8 serves each, 21 total) and
# max_stub_serve_count peaked at exactly 8 -- one shy of the >8 trigger -- so
# only the much slower 300s Tier-1 backstop eventually caught it.

def test_aggregate_stub_count_at_the_threshold_does_not_trigger():
    snapshot = {"stagnant_seconds": 0, "max_stub_serve_count": 4, "total_stub_serve_count": 15}
    assert _liveness_kill_reason(snapshot) is None


def test_aggregate_stub_count_over_the_threshold_triggers():
    snapshot = {"stagnant_seconds": 0, "max_stub_serve_count": 4, "total_stub_serve_count": 16}
    reason = _liveness_kill_reason(snapshot)
    assert reason is not None
    assert "16" in reason


def test_the_exact_confirmed_live_incident_shape_now_trips_the_aggregate_tier():
    """3 files, 6-8 serves each, no single key ever crossing 8 -- the precise
    shape that slipped past Tier 2 alone in the real incident this closes."""
    snapshot = {"stagnant_seconds": 0, "max_stub_serve_count": 8, "total_stub_serve_count": 21}
    reason = _liveness_kill_reason(snapshot)
    assert reason is not None


def test_aggregate_signal_missing_from_an_older_snapshot_defaults_to_healthy():
    """Backward compat: a snapshot written before this field existed (or any
    caller that never sets it) must default to 0, not crash or false-trigger."""
    snapshot = {"stagnant_seconds": 0, "max_stub_serve_count": 0}
    assert _liveness_kill_reason(snapshot) is None


# ── _read_liveness_snapshot ─────────────────────────────────────────────────────

def test_reads_a_real_written_snapshot(tmp_path):
    path = tmp_path / "liveness.json"
    path.write_text('{"stagnant_seconds": 5.0, "max_stub_serve_count": 1}')

    snapshot = _read_liveness_snapshot(path)

    assert snapshot == {"stagnant_seconds": 5.0, "max_stub_serve_count": 1}


def test_missing_file_returns_none_not_an_exception(tmp_path):
    """The common case early in a run -- the worker hasn't reached its first
    heartbeat tick yet, so the file doesn't exist. Must be silently healthy,
    never crash the poll loop reading it."""
    path = tmp_path / "does-not-exist.json"

    assert _read_liveness_snapshot(path) is None


def test_malformed_json_returns_none_not_an_exception(tmp_path):
    """A torn read (write-in-progress caught mid-write) shouldn't happen given
    the write side is atomic (temp file + os.replace), but this stays
    defensive anyway -- a bookkeeping read must never crash the real poll loop
    that's also deciding whether to kill a real run."""
    path = tmp_path / "liveness.json"
    path.write_text("{not valid json")

    assert _read_liveness_snapshot(path) is None
