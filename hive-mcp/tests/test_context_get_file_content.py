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


# ── Not-found fallback: recover from a wrong-but-plausible directory guess ──────
# Measured live 2026-08-08: a task naming bare filenames ("SellerRow.tsx") caused
# the agent to guess a wrong-but-plausible directory 3 times, each requiring a
# separate search_files() call to recover -- a 6-call round trip. The real repo
# layout's mismatch was an internal subdirectory, not just a missing root prefix
# (which find_files()'s GLOB_FALLBACK_PREFIXES already handles), so this needs a
# basename search, not a prefix retry.

def test_wrong_directory_guess_auto_corrects_when_basename_is_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    real_dir = tmp_path / "Client" / "EcommClient-Web" / "ekamweb" / "src" / "components" / "portal" / "admin" / "users"
    real_dir.mkdir(parents=True)
    (real_dir / "SellerRow.tsx").write_text("export function SellerRow() {}", encoding="utf-8")

    result = context.get_file_content("src/components/user-management/SellerRow.tsx")

    assert "NOTE:" in result
    assert "src/components/user-management/SellerRow.tsx' not found" in result
    assert "Client/EcommClient-Web/ekamweb/src/components/portal/admin/users/SellerRow.tsx" in result
    assert "export function SellerRow" in result


def test_wrong_directory_guess_lists_candidates_when_basename_is_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    (tmp_path / "admin").mkdir()
    (tmp_path / "admin" / "Row.tsx").write_text("A", encoding="utf-8")
    (tmp_path / "seller").mkdir()
    (tmp_path / "seller" / "Row.tsx").write_text("B", encoding="utf-8")

    result = context.get_file_content("wrong/path/Row.tsx")

    assert "File not found: wrong/path/Row.tsx" in result
    assert "2 files named 'Row.tsx' exist" in result
    assert "admin/Row.tsx" in result
    assert "seller/Row.tsx" in result
    # Neither candidate's content is returned -- the agent must retry with the exact path.
    assert "A" not in result.split("\n")
    assert "B" not in result.split("\n")


def test_no_matching_basename_anywhere_returns_plain_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    (tmp_path / "unrelated.py").write_text("x = 1", encoding="utf-8")

    result = context.get_file_content("nowhere/NeverExisted.tsx")

    assert result == "File not found: nowhere/NeverExisted.tsx"


def test_auto_corrected_read_preserves_offset_and_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    real_dir = tmp_path / "real" / "location"
    real_dir.mkdir(parents=True)
    (real_dir / "Big.py").write_text("\n".join(f"line{i}" for i in range(300)), encoding="utf-8")

    result = context.get_file_content("wrong/Big.py", offset=100, limit=2)

    assert "NOTE:" in result
    assert "   101\tline100" in result
    assert "   102\tline101" in result
    assert "line299" not in result
