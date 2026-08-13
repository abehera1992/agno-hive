import importlib
import sys


def _reload_config():
    """Reload config module so env changes take effect."""
    if "config.config" in sys.modules:
        del sys.modules["config.config"]
    return importlib.import_module("config.config")


def test_defaults_have_no_hardcoded_ips():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.ollama_host == ""
    assert cfg.mcp_url == ""


def test_patterns_glob_default():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.patterns_glob == "patterns/**/*.md"


def test_patterns_glob_from_env(monkeypatch):
    monkeypatch.setenv("PATTERNS_GLOB", "docs/patterns/**/*.md")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.patterns_glob == "docs/patterns/**/*.md"


def test_stream_defaults_false():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.stream is False


def test_session_defaults():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.session_ttl_days == 30
    assert cfg.session_window == 6
    assert cfg.compact_threshold == 20
    assert cfg.session_cleanup_interval == 3600


def test_session_window_from_env(monkeypatch):
    monkeypatch.setenv("AGNO_SESSION_WINDOW", "12")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.session_window == 12


def test_tool_call_limit_default():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.tool_call_limit == 25


def test_tool_call_limit_from_env(monkeypatch):
    monkeypatch.setenv("TOOL_CALL_LIMIT", "40")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.tool_call_limit == 40


def test_read_only_max_iterations_default():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.read_only_max_iterations == 10


def test_read_only_max_iterations_from_env(monkeypatch):
    monkeypatch.setenv("READ_ONLY_MAX_ITERATIONS", "6")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.read_only_max_iterations == 6


def test_read_only_max_iterations_is_tighter_than_the_default_max_iterations():
    """The whole point of this value: it must be meaningfully lower than the
    default max_iterations, or scoping it to read_only requests achieves nothing."""
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.read_only_max_iterations < cfg.max_iterations


# ── Member-agent sampling caps (2026-08-12) ───────────────────────────────────────
# Confirmed live: a Researcher turn stalled 4+ minutes with the same repetition-loop
# signature the coordinator's own temperature/max_tokens/frequency_penalty fix
# (2026-08-10) targets -- but that fix was scoped coordinator-only, leaving every
# member agent on get_model()'s raw, unbounded defaults. See config.py's
# member_temperature docstring for the full incident and swarm/agents.py for where
# these are actually wired into every member-agent-building get_model() call.

def test_member_temperature_default():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.member_temperature == 0.2


def test_member_temperature_from_env(monkeypatch):
    monkeypatch.setenv("MEMBER_TEMPERATURE", "0.5")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.member_temperature == 0.5


def test_member_frequency_penalty_default():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.member_frequency_penalty == 0.15


def test_member_max_tokens_default():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.member_max_tokens == 4096


def test_coder_max_tokens_default():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.coder_max_tokens == 8192


def test_coder_max_tokens_is_larger_than_the_member_default():
    """The whole point of a separate coder_max_tokens: Coder legitimately needs more
    headroom for large diffs than a prose-output member agent does."""
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.coder_max_tokens > cfg.member_max_tokens


def test_member_temperature_matches_the_coordinators_own_tuned_value_by_default():
    """Reuses the coordinator's already-proven value rather than introducing a
    separately-tuned one -- the failure mode being targeted is identical. Same-value
    by default does not mean same field: test_agents_member_sampling_params.py
    confirms retuning one does not silently retune the other."""
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.member_temperature == cfg.coordinator_temperature
    assert cfg.member_frequency_penalty == cfg.coordinator_frequency_penalty


# ── Liveness-based auto-kill (Recommendation #2, 2026-08-13) ──────────────────

def test_enable_liveness_autokill_defaults_off():
    """Off by default until validated live against a deliberately-reproduced
    stall -- same rollout discipline use_worker_process_isolation had."""
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.enable_liveness_autokill is False


def test_enable_liveness_autokill_from_env(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVENESS_AUTOKILL", "true")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.enable_liveness_autokill is True


def test_liveness_silence_threshold_default():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.liveness_silence_threshold_s == 300


def test_liveness_silence_threshold_from_env(monkeypatch):
    monkeypatch.setenv("LIVENESS_SILENCE_THRESHOLD_S", "120")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.liveness_silence_threshold_s == 120


def test_liveness_stub_serve_threshold_default():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.liveness_stub_serve_threshold == 8


def test_liveness_stub_serve_threshold_from_env(monkeypatch):
    monkeypatch.setenv("LIVENESS_STUB_SERVE_THRESHOLD", "5")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.liveness_stub_serve_threshold == 5
