"""Regression test: hive-mcp's get_file_content must return real line numbers.

hive-mcp/tools/context.py duplicates EkamApp's own mcp-server/tools/context.py --
same tool name, same signature, both connected to every swarm run (hive-mcp first,
"primary" per CLAUDE.md; the project MCP second, "supplementary"). The line-numbering
fix landed in EkamApp's copy first (2026-08-04) and was verified directly -- but the
swarm kept citing fabricated line numbers afterward anyway, because nothing forces
its tool calls through the one that was actually fixed. This locks the same fix into
hive-mcp's copy, the one the swarm is meant to use by design.
"""
from tools import context


def test_full_file_read_has_line_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    f = tmp_path / "models.py"
    f.write_text("\n".join(["x = 1"] * 234 + ["class Party(Base):", '    """L1."""']),
                  encoding="utf-8")

    result = context.get_file_content("models.py")

    assert "   235\tclass Party(Base):" in result
    assert '   236\t    """L1."""' in result


def test_ranged_read_numbers_from_the_real_offset(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    f = tmp_path / "models.py"
    f.write_text("\n".join(["x = 1"] * 234 + ["class Party(Base):", '    """L1."""']),
                  encoding="utf-8")

    result = context.get_file_content("models.py", offset=234, limit=2)

    assert "   235\tclass Party(Base):" in result
    assert '   236\t    """L1."""' in result
