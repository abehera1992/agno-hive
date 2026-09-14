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
    monkeypatch.setattr(config, "liveness_repetition_threshold", 4)


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


# ── _MCP_TIMEOUT must stay meaningfully below liveness_silence_threshold_s ──────
#
# Root-caused live 2026-08-18 (T6 of a T1-T13 groundedness battery, task
# kn7ohwq3h): swarm/team.py's _MCP_TIMEOUT (agno MCPTools' own client-side
# per-tool-call timeout) was set to the exact same value as
# config.liveness_silence_threshold_s (300s default). A genuinely hung MCP tool
# call (confirmed: the coordinator called verify_claims; hive-mcp's own docker
# logs show the request never arrived -- the hang was client-side) then raced
# two timeouts set to identical thresholds. The outer, cruder liveness watchdog
# (api/server.py, polling on its own clock) won that race by about a second
# every time -- confirmed live: heartbeat logged "277s since last tool call" at
# 18:29:12, the liveness SIGKILL fired at 18:29:13, and the interception hook's
# own "RAISED ... after Ns" log (swarm/team.py's _make_tool_interception_hook)
# never printed at all. The MCP client's own timeout never got a chance to fire
# and produce a real, diagnostic exception -- the run just silently burned the
# full 300s and died with a content-free 504. This test guards the fix: as long
# as _MCP_TIMEOUT has real headroom below the liveness threshold, a stuck tool
# call times out cleanly, with a logged reason, before the blunter kill fires.
def test_mcp_timeout_has_headroom_before_liveness_kill():
    from swarm.team import _MCP_TIMEOUT

    assert _MCP_TIMEOUT < config.liveness_silence_threshold_s
    assert config.liveness_silence_threshold_s - _MCP_TIMEOUT >= 60


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


# ── Tier 5: repetition_count, recalibrated 2026-09-14 (Phase Q) ────────────────
#
# Original threshold (6) never fired across 48 hours of real production-adjacent
# traffic pulled from the live journal (32 firings, 13 distinct runs, every
# single-run streak <= 3), while T1-T13 battery test T11 ran 15+ minutes
# regenerating the same verbatim block ~5 times with no synthesized final answer
# and was never killed. Lowered to 4 -- one firing of margin above every streak
# actually observed live, closing most of the gap that let T11's run continue
# unbounded. See config.py's own comment on liveness_repetition_threshold for
# the full calibration evidence.

def test_repetition_count_below_the_threshold_does_not_trigger():
    snapshot = {"stagnant_seconds": 0, "repetition_count": 3}
    assert _liveness_kill_reason(snapshot) is None


def test_repetition_count_at_the_threshold_triggers():
    """Tier 5 uses >=, not > (unlike Tier 1's strict > threshold) -- exactly at
    the configured threshold already triggers, matching _liveness_kill_reason's
    own `if repeats >= config.liveness_repetition_threshold` check."""
    snapshot = {"stagnant_seconds": 0, "repetition_count": 4}
    reason = _liveness_kill_reason(snapshot)
    assert reason is not None
    assert "4" in reason


def test_repetition_reason_names_looping_not_stagnation():
    """Distinguishes this tier's own failure mode in the message -- a repetition
    kill is NOT a silence/stall kill, and an operator reading the reason should
    not have to guess which tier fired."""
    snapshot = {"stagnant_seconds": 0, "repetition_count": 6}
    reason = _liveness_kill_reason(snapshot)
    assert reason is not None
    assert "looping" in reason.lower()


def test_the_observed_max_benign_streak_from_the_live_journal_never_triggers():
    """Direct regression for the calibration evidence itself: every streak this
    phase actually observed across 48h of real traffic (max 3, spread across 13
    distinct runs) must stay healthy under the new threshold -- the whole point
    of choosing 4 rather than something tighter."""
    for observed_streak in (1, 2, 3):
        snapshot = {"stagnant_seconds": 0, "repetition_count": observed_streak}
        assert _liveness_kill_reason(snapshot) is None


def test_repetition_signal_missing_from_an_older_snapshot_defaults_to_healthy():
    """Backward compat: a snapshot written before this field existed (or a
    caller, e.g. _stream_team_run's older activity dicts, that never sets it)
    must default to 0, not crash or false-trigger."""
    snapshot = {"stagnant_seconds": 0}
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
