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


def test_ambiguous_candidates_message_explicitly_warns_against_retrying_the_same_path(tmp_path, monkeypatch):
    """Confirmed live 2026-08-11: a run called get_file_content() with the same wrong
    truncated path 4 times in a row, each time receiving this exact disambiguation
    message and each time ignoring it, before giving up and pivoting to irrelevant web
    searches. The message itself must explicitly say not to retry the same path --
    complementary reinforcement to the teams/engineering.yaml PATH-CORRECTION rule,
    not a substitute for it (a model can still ignore either one in isolation)."""
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    (tmp_path / "admin").mkdir()
    (tmp_path / "admin" / "Row.tsx").write_text("A", encoding="utf-8")
    (tmp_path / "seller").mkdir()
    (tmp_path / "seller" / "Row.tsx").write_text("B", encoding="utf-8")

    result = context.get_file_content("wrong/path/Row.tsx")

    assert "do NOT retry 'wrong/path/Row.tsx' again" in result
    assert "copied verbatim" in result


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


# ── CLAUDE.md is excluded from every content-serving path ───────────────────────
# Confirmed live 2026-08-09: a cloud model (gemini-3.1-flash-lite) skipped the
# documented hive.md/get_project_context flow and called get_file_content('CLAUDE.md')
# directly, then answered a codebase question by pattern-matching against instruction
# text instead of real source. CLAUDE.md tells an assistant how to BEHAVE in this
# repo -- it is not project documentation, and must never reach a swarm agent as if
# it were. AGENTS.md/GEMINI.md are NOT excluded -- no observed problem with them yet;
# scope is deliberately narrow to the one file with a confirmed live failure.

def test_get_file_content_refuses_claude_md_at_root(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    (tmp_path / "CLAUDE.md").write_text("secret agent instructions", encoding="utf-8")

    result = context.get_file_content("CLAUDE.md")

    assert "excluded from tool access" in result
    assert "secret agent instructions" not in result


def test_get_file_content_still_serves_agents_and_gemini_md(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    (tmp_path / "AGENTS.md").write_text("codex instructions", encoding="utf-8")
    (tmp_path / "GEMINI.md").write_text("gemini cli instructions", encoding="utf-8")

    assert "codex instructions" in context.get_file_content("AGENTS.md")
    assert "gemini cli instructions" in context.get_file_content("GEMINI.md")


def test_get_file_content_refuses_nested_claude_md_by_basename(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    sub = tmp_path / "some" / "subdir"
    sub.mkdir(parents=True)
    (sub / "CLAUDE.md").write_text("directory-scoped instructions", encoding="utf-8")

    result = context.get_file_content("some/subdir/CLAUDE.md")

    assert "excluded from tool access" in result
    assert "directory-scoped instructions" not in result


def test_get_project_context_excludes_claude_md_but_includes_docs_and_readme(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    (tmp_path / "CLAUDE.md").write_text("secret agent instructions", encoding="utf-8")
    (tmp_path / "DOCS.md").write_text("real architecture docs", encoding="utf-8")
    (tmp_path / "README.md").write_text("real readme", encoding="utf-8")

    result = context.get_project_context()

    assert "secret agent instructions" not in result
    assert "CLAUDE.md" not in result
    assert "real architecture docs" in result
    assert "real readme" in result


# ── ambiguous-candidate ranking (2026-08-11) ────────────────────────────────────
# Confirmed live: an ambiguous basename ('index.tsx', legitimately present in several
# unrelated parts of a monorepo) produced a disambiguation list the model then worked
# through mechanically top-to-bottom -- including an unrelated app's own file --
# instead of recognizing which candidate actually matched the task. Ranking by shared
# leading directory segments with the ORIGINAL (wrong) guess surfaces the
# almost-certainly-correct candidate first, deterministically.

def test_shared_leading_segments_counts_common_directories_before_divergence():
    a = "Client/EcommClient-Web/ekamweb/src/components/business/index.tsx"
    b = "Client/EcommClient-Web/ekamweb/src/app/(portal)/business/index.tsx"
    # Shared: Client, EcommClient-Web, ekamweb, src -- diverges at components vs app
    assert context._shared_leading_segments(a, b) == 4


def test_shared_leading_segments_is_low_for_an_unrelated_app():
    a = "Client/EcommClient-Web/ekamweb/src/components/business/index.tsx"
    b = "Client/EcommClient-Mobile/app/(tabs)/index.tsx"
    # Shared: Client only -- diverges immediately at EcommClient-Web vs EcommClient-Mobile
    assert context._shared_leading_segments(a, b) == 1


def test_shared_leading_segments_ignores_the_filename_itself():
    # Same directory, different filename -- must not count the filename as a segment
    a = "src/components/Foo.tsx"
    b = "src/components/Bar.tsx"
    assert context._shared_leading_segments(a, b) == 2


def test_rank_candidates_by_relevance_puts_the_closest_directory_match_first():
    guessed = "Client/EcommClient-Web/ekamweb/src/components/business/index.tsx"
    candidates = [
        "Client/EcommClient-Mobile/app/(tabs)/index.tsx",
        "Client/EcommClient-Web/ekamweb/src/app/(portal)/business/index.tsx",
        "signoz/frontend/src/hooks/useDarkMode/index.tsx",
    ]

    ranked = context._rank_candidates_by_relevance(guessed, candidates)

    assert ranked[0] == "Client/EcommClient-Web/ekamweb/src/app/(portal)/business/index.tsx"


def test_get_file_content_ambiguous_candidates_are_ranked_in_the_response(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    mobile = tmp_path / "Client" / "EcommClient-Mobile" / "app" / "(tabs)"
    mobile.mkdir(parents=True)
    (mobile / "index.tsx").write_text("mobile", encoding="utf-8")
    web = tmp_path / "Client" / "EcommClient-Web" / "ekamweb" / "src" / "app" / "business"
    web.mkdir(parents=True)
    (web / "index.tsx").write_text("web", encoding="utf-8")

    result = context.get_file_content("Client/EcommClient-Web/ekamweb/src/components/business/index.tsx")

    # The web candidate (closer directory match to the guess) must be listed before
    # the mobile candidate, not just in whatever order _find_by_basename returned.
    web_pos = result.index("Client/EcommClient-Web/ekamweb/src/app/business/index.tsx")
    mobile_pos = result.index("Client/EcommClient-Mobile/app/(tabs)/index.tsx")
    assert web_pos < mobile_pos
    assert "sorted by how closely their directory matches your guess" in result
    assert "most likely match FIRST" in result
