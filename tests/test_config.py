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


def test_db_url_defaults_to_none():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.db_url is None


def test_patterns_glob_default():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.patterns_glob == "patterns/**/*.md"


def test_patterns_glob_from_env(monkeypatch):
    monkeypatch.setenv("PATTERNS_GLOB", "docs/patterns/**/*.md")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.patterns_glob == "docs/patterns/**/*.md"


def test_db_url_from_env(monkeypatch):
    monkeypatch.setenv("DB_URL", "postgresql://user:pass@host/db")
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.db_url == "postgresql://user:pass@host/db"


def test_stream_defaults_false():
    mod = _reload_config()
    cfg = mod.Config()
    assert cfg.stream is False
