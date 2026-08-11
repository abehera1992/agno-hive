"""Regression test: Researcher's web_search/web_fetch instructions in
teams/engineering.yaml must not let a failed internal file lookup fall through to an
external web search, and must tell the model to reuse a disambiguation candidate
verbatim instead of retrying the same wrong path.

Confirmed live 2026-08-11: a read-only retest (find the real party edit panel
component) had Researcher repeatedly call get_file_content() with the same wrong,
truncated path -- each time receiving hive-mcp's own disambiguation message listing
the real candidate paths, each time ignoring it and retrying the identical wrong path.
After several failed attempts it gave up and pivoted to web_search('eKam platform
GitHub repository') and similar queries -- searching the public web for a private,
internal codebase question, which can never return anything useful. Root cause: the
WEB rule's own catch-all line ("Codebase context alone is insufficient to answer
confidently -> web_search to fill the gap") was vague enough that "I failed to find
an internal file" satisfied it, directly contradicting the very next line ("Always
prefer local file tools... use web tools only for external context").
"""
from pathlib import Path

import yaml

_ENGINEERING_YAML = Path(__file__).parent.parent / "teams" / "engineering.yaml"


def _researcher_instructions() -> str:
    data = yaml.safe_load(_ENGINEERING_YAML.read_text(encoding="utf-8"))
    researcher = next(a for a in data["agents"] if a["name"] == "Researcher")
    return "\n".join(researcher["instructions"])


def test_web_search_catchall_no_longer_covers_a_failed_internal_lookup():
    text = _researcher_instructions()
    # The old, vague catch-all line must be gone -- it's what let "I couldn't find the
    # file" get treated as "codebase context is insufficient".
    assert "Codebase context alone is insufficient to answer confidently" not in text


def test_web_search_explicitly_excludes_failed_internal_file_lookups():
    text = _researcher_instructions()
    assert "NEVER a case for web_search/web_fetch" in text
    assert "failing to locate an INTERNAL project file" in text.replace("\n", " ")


def test_path_correction_rule_exists_and_names_the_concrete_behavior():
    text = _researcher_instructions()
    assert "PATH-CORRECTION rule" in text
    assert "verbatim" in text
    assert "retry the same guessed/truncated path again" in text.replace("\n", " ")


def test_path_correction_rule_directs_to_the_ranked_first_candidate():
    """Confirmed live 2026-08-11: a second incident, distinct from simple path
    repetition -- an AMBIGUOUS basename (e.g. 'index.tsx', legitimately present in
    several unrelated parts of the monorepo) got tried mechanically in whatever order
    was listed, including an unrelated file. The disambiguation list is now ranked by
    relevance to the original guess (see hive-mcp's _rank_candidates_by_relevance),
    so the instruction must tell the model to trust that ranking rather than pick
    arbitrarily."""
    text = _researcher_instructions()
    joined = text.replace("\n", " ")
    assert "FIRST listed candidate" in joined
    assert "ranked by relevance" in joined


def test_web_search_rule_still_covers_genuine_external_topics():
    """Must not regress into removing web_search entirely -- it's still correct for
    real external-library/tool questions, only the internal-lookup-fallback misuse is
    excluded."""
    text = _researcher_instructions()
    assert "unfamiliar library, tool, or service" in text
    assert "web_search" in text
