"""Tests for the 2026-08-15 Researcher instruction-bloat reduction.

Measured before this change: Researcher's always-loaded instructions had grown
to 55 lines / 16,086 chars / 16 separately-named rules across this session's
own incident-driven patches (DECOMPOSE-FIRST, SCAN-FIRST, COVERAGE,
ENUMERATION, SEARCH, CHAIN, WEB, NOTION, PATH-CORRECTION, VERIFY-EXTERNAL,
REUSABLE-CITATION, EVIDENCE, DESIGN-INTENT, HARD RULE, NEGATIVE-CLAIM,
COMPARISON) -- 3-15x every other role's instruction volume (ContextRouter
1,626 chars, Coder 5,737, Reviewer 1,566, Executor 1,039). Live-confirmed
twice in one evening (2026-08-14 and 2026-08-15) that a newly-added rule
(Step 3a, search-before-browse) sat inside that block and was NOT followed on
either of its first two live tests, despite sitting reasonably early in the
list -- consistent with a 32B model defaulting to older, more habitual
patterns (SCAN-FIRST's "Always run list_directory_tree()") over a newer,
more conditional one when holding many competing named rules at once.

Fix: 6 of the 16 rules -- the ones relevant only to specific, uncommon task
shapes (list-all-services questions, cross-service chains, external
URLs/libraries, Notion doc references, a specific error-recovery case,
external-framework citation) -- moved to on-demand hive-mcp skills, each
left behind a single short always-on pointer line naming its exact trigger
condition. The always-loaded set now concentrates on what's relevant to
EVERY grounding/comparison task: DECOMPOSE-FIRST (incl. the consolidated
Step 3a search-before-browse, merged with the old standalone SEARCH rule),
SCAN-FIRST (tightened to explicitly exclude checklist-item grounding, since
it was observed being applied as a default first action regardless of task
shape), HARD RULE, NEGATIVE-CLAIM, COMPARISON, REUSABLE-CITATION.

The `comparison-discipline` skill's own history (measured to never actually
get load_skill()'d in two consecutive relevant runs, hence promoted to
always-on) is the known risk on the other side of this trade -- mitigated
here by a single always-on pointer line naming each moved skill's exact
trigger, not silence.
"""
from pathlib import Path

import yaml

_ENGINEERING_YAML = Path(__file__).parent.parent / "teams" / "engineering.yaml"
_SKILLS_DIR = Path(__file__).parent.parent / "hive-mcp" / "skills"

_MOVED_SKILLS = [
    "codebase-enumeration-discipline",
    "chain-tracing-discipline",
    "external-web-research",
    "notion-reference-discovery",
    "path-not-found-recovery",
    "external-framework-verification",
]


def _load_engineering_yaml() -> dict:
    return yaml.safe_load(_ENGINEERING_YAML.read_text(encoding="utf-8"))


def _researcher(data: dict) -> dict:
    return next(a for a in data["agents"] if a["name"] == "Researcher")


# ── Volume actually went down ────────────────────────────────────────────────

def test_researcher_instruction_volume_is_meaningfully_reduced():
    data = _load_engineering_yaml()
    researcher = _researcher(data)
    total_chars = sum(len(i) for i in researcher["instructions"])
    # Was 16,086 chars / 55 lines before this change -- must be well below both.
    assert total_chars < 12000
    assert len(researcher["instructions"]) < 40


# ── Each moved skill file exists with correct frontmatter and real content ──

def test_all_six_moved_skills_exist_with_correct_frontmatter_name():
    for skill_name in _MOVED_SKILLS:
        skill_path = _SKILLS_DIR / skill_name / "SKILL.md"
        assert skill_path.exists(), f"missing skill file: {skill_path}"
        text = skill_path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        parsed = yaml.safe_load(text.split("---\n")[1])
        assert parsed["name"] == skill_name
        assert parsed["description"]


def test_moved_skill_bodies_are_non_trivial():
    """Each skill must carry the real, substantial rule text -- not a stub."""
    for skill_name in _MOVED_SKILLS:
        skill_path = _SKILLS_DIR / skill_name / "SKILL.md"
        body = skill_path.read_text(encoding="utf-8").split("---\n", 2)[2]
        assert len(body) > 200, f"{skill_name} body looks too small: {len(body)} chars"


def test_researcher_skills_list_references_all_six_moved_skills():
    data = _load_engineering_yaml()
    researcher = _researcher(data)
    for skill_name in _MOVED_SKILLS:
        assert skill_name in researcher["skills"]


# ── An always-on pointer line survives for each moved rule, naming its trigger ──

def test_instruction_budget_pointer_names_every_moved_skill_and_its_trigger():
    data = _load_engineering_yaml()
    researcher = _researcher(data)
    joined = " ".join(researcher["instructions"])
    for skill_name in _MOVED_SKILLS:
        assert skill_name in joined, f"no pointer line names {skill_name}"
    # Spot-check the trigger conditions are actually stated, not just the names.
    assert "list all apis" in joined.lower() or "what services exist" in joined.lower()
    assert "notion page" in joined.lower()
    assert "file not found" in joined.lower()


# ── Content specific to a moved rule is genuinely gone from always-on text ──

def test_web_rule_full_text_no_longer_always_loaded():
    data = _load_engineering_yaml()
    researcher = _researcher(data)
    joined = " ".join(researcher["instructions"])
    # The old rule's own distinctive long-form text must not still be inlined.
    assert "call web_fetch(url) immediately before doing anything else" not in joined


def test_enumeration_rule_full_text_no_longer_always_loaded():
    data = _load_engineering_yaml()
    researcher = _researcher(data)
    joined = " ".join(researcher["instructions"])
    assert "read exactly one anchor file" not in joined.lower()


# ── SEARCH rule consolidated into Step 3a, not duplicated ──────────────────

def test_standalone_search_rule_heading_is_gone():
    """SEARCH rule and Step 3a said nearly the same thing twice -- consolidated
    into one place (Step 3a) rather than kept as two overlapping rules."""
    data = _load_engineering_yaml()
    researcher = _researcher(data)
    joined = " ".join(researcher["instructions"])
    assert '"SEARCH rule:' not in " ".join(f'"{i}"' for i in researcher["instructions"])


# ── SCAN-FIRST tightened to not compete with Step 3a on checklist grounding ──

def test_scan_first_explicitly_excludes_checklist_item_grounding():
    data = _load_engineering_yaml()
    researcher = _researcher(data)
    joined = " ".join(researcher["instructions"])
    assert "not as a default first action for a decompose-first checklist item" in joined.lower()


# ── Every other role's instructions are untouched by this Researcher-only change ──

def test_other_roles_instruction_volume_is_not_bloated_like_researcher_was():
    """Not an exact byte-count pin (fragile across encodings/line-ending
    normalization) -- just confirms this pruning pass didn't accidentally
    balloon a role it wasn't meant to touch. Researcher itself is exempt
    here (that's the one role this change deliberately shrinks)."""
    data = _load_engineering_yaml()
    for agent in data["agents"]:
        if agent["name"] == "Researcher":
            continue
        total_chars = sum(len(i) for i in agent["instructions"])
        assert total_chars < 8000, f"{agent['name']} unexpectedly grew to {total_chars} chars"
