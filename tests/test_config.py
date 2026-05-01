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
