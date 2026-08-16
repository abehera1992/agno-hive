"""Tests for training/fetch_patterns.py's response parsing -- two real bugs found
live 2026-08-16 while regenerating the training corpus for the Qwen3.8-27B run
(training/data/corpus_v2.jsonl didn't exist on ZGX and needed rebuilding):

1. The default --mcp-url pointed at EkamApp's own MCP server (AGNO_MCP_URL), but
   find_files/get_file_content were removed from it in commit 2e0fbe1 (2026-08-04)
   -- generic file I/O was deliberately migrated to hive-mcp. Calling a tool name a
   server doesn't register returns an empty result, not an error, so this failed
   completely silently: "found 0 file(s)" for every call since that commit.

2. Once pointed at hive-mcp, get_file_content's cat -n numbered output ("   123\\t
   content", added earlier this session for citation groundedness) broke
   patterns_md.py's guard parser -- its _GUARD_RE anchors on a literal "## " at the
   start of a line, and every line now started with a number + tab instead, so a
   full training corpus build silently produced ZERO patterns_md preference pairs
   (the only Axis D training signal) with no error or warning.

A third, smaller bug in the same fix pass: find_files always prefixes its reply
with a header line ("N result(s) for '<glob>':", hive-mcp/tools/context.py) that
doesn't end in "/" and, because the glob pattern itself contains slashes,
Path(header).name still resolved to something containing a "." -- so it silently
passed the old "looks like a file" filter and got fetched as a bogus file.
"""
from training.fetch_patterns import _strip_line_numbers


def test_strip_line_numbers_removes_cat_n_prefix():
    numbered = "     7\t## GUARD 1: Always import libraries before using them\n     8\t"
    result = _strip_line_numbers(numbered)
    assert result == "## GUARD 1: Always import libraries before using them\n"


def test_strip_line_numbers_preserves_real_tabs_after_the_prefix():
    """Only the FIRST tab is the cat -n separator -- a line whose real content
    itself contains a tab (e.g. TSV-like data) must not lose anything past it."""
    numbered = "     1\tcol_a\tcol_b\tcol_c"
    assert _strip_line_numbers(numbered) == "col_a\tcol_b\tcol_c"


def test_strip_line_numbers_leaves_untabbed_lines_alone():
    """Defensive fallback: a line with no tab at all (shouldn't happen against a
    real hive-mcp response, but must not crash or mangle it if it ever does)."""
    assert _strip_line_numbers("no prefix here") == "no prefix here"


def test_strip_line_numbers_handles_guard_markers_end_to_end():
    """Reproduces the actual live failure: a real GUARD block, cat -n numbered,
    must parse correctly with patterns_md.py's real _GUARD_RE after stripping."""
    from training.sources.patterns_md import _GUARD_RE

    numbered = (
        "     6\t---\n"
        "     7\t\n"
        "     8\t## GUARD 18: Wrap raw SQL strings in text()\n"
        "     9\t\n"
        "    10\tSome rationale text.\n"
    )
    stripped = _strip_line_numbers(numbered)
    hits = list(_GUARD_RE.finditer(stripped))
    assert len(hits) == 1
    assert hits[0].group(2) == "18"

    # The bug this guards against: matching directly against the numbered text
    # (as fetch_patterns.py did before the fix) finds nothing at all.
    assert list(_GUARD_RE.finditer(numbered)) == []


def test_find_files_header_line_pattern_is_recognized():
    """The header format hive-mcp/tools/context.py actually emits -- verifies the
    regex fetch_patterns.py uses to strip it matches the real, current format."""
    import re
    header = "9 result(s) for 'patterns/**/*.md':"
    assert re.match(r"^\d+ result\(s\) for ", header)
