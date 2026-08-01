"""Skill catalog + on-demand loader — progressive disclosure for bootstrap context.

Two file sources merge into one catalog, plus one synthesized entry:
  - hive-mcp/skills/       generic protocol skills, ships with the server
  - PROJECT_ROOT/skills/   project-specific skills (e.g. EkamApp/skills/) — wins on
                           a name collision, so a project can override a generic skill
  - code-conventions       built from CODE_LINT_REQUIRE/FORBID (see config.py) rather
                           than a hand-authored file, so a project's lint rules stay
                           defined in exactly one place

Each skill is a directory containing SKILL.md: '---\\n<yaml frontmatter>\\n---\\n<body>'.
Frontmatter needs only `name` and `description` — role-scoping is NOT a skill-file
concept; it is decided per agent by a team's own YAML (`skills:`, mirroring the
existing `tools:` allowlist), read in swarm/agents.py, not here.

list_skills() returns the L1 index (names + one-line descriptions, no body) so it
stays cheap enough to be present in every run. load_skill(name) returns one skill's
full body on demand — this is the thing the "always load everything" design this
replaces got wrong: paying for all of it before the model chose it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

import yaml

_HIVE_SKILLS_DIR = Path(__file__).parent.parent / "skills"


def _project_skills_dir() -> Path:
    return config.PROJECT_ROOT / "skills"


def _parse_skill_md(path: Path) -> dict | None:
    """'---\\nname: x\\ndescription: y\\n---\\nbody' -> {name, description, body}.

    Returns None for anything malformed (no frontmatter, unparsable YAML, missing
    name) rather than raising — one broken SKILL.md must not take down the whole
    catalog for every other skill.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except Exception:
        return None
    name = meta.get("name") if isinstance(meta, dict) else None
    if not name:
        return None
    return {
        "name": str(name),
        "description": str(meta.get("description", "")),
        "body": parts[2].lstrip("\n").rstrip(),
    }


def _discover(dir_path: Path, source: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    if not dir_path.is_dir():
        return found
    for skill_dir in sorted(dir_path.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        parsed = _parse_skill_md(skill_md)
        if parsed:
            parsed["source"] = source
            found[parsed["name"]] = parsed
    return found


def _dynamic_skills() -> dict[str, dict]:
    """code-conventions, synthesized from config.CODE_LINT_REQUIRE/FORBID.

    Replaces hive-mcp/main.py's old _conventions_block(): the rules used to be
    appended to every run's instructions regardless of task. As a skill, they load
    only when a code-writing task actually pulls them in.
    """
    req = [r.partition("::") for r in config.CODE_LINT_REQUIRE]
    forb = [r.partition("::") for r in config.CODE_LINT_FORBID]
    if not (req or forb):
        return {}
    msgs = [msg.rstrip(".") for _, _, msg in (req + forb) if msg]
    body = (
        "Project code conventions — authoritative and complete. Apply directly when "
        "writing or editing code; do not search the repo or docs for a styling guide, "
        "and do not infer conventions from unrelated files.\n\n"
        + "\n".join(f"- {m}." for m in msgs)
    )
    return {
        "code-conventions": {
            "name": "code-conventions",
            "description": "Required and forbidden code patterns for this project — "
                            "load before writing or editing any code.",
            "body": body,
            "source": "dynamic",
        }
    }


def _catalog() -> dict[str, dict]:
    catalog = _discover(_HIVE_SKILLS_DIR, "hive-mcp")
    catalog.update(_discover(_project_skills_dir(), "project"))
    catalog.update(_dynamic_skills())
    return catalog


def list_skills() -> str:
    """
    Return the L1 skill catalog as a JSON array: [{name, description, source}, ...].

    This is the always-on index — names and one-line descriptions only, never full
    text — so it stays cheap enough to be present in every run. Call load_skill(name)
    to fetch one skill's full instructions on demand, only for a skill whose
    description actually matches the current task.
    """
    catalog = _catalog()
    entries = [
        {"name": s["name"], "description": s["description"], "source": s["source"]}
        for s in catalog.values()
    ]
    return json.dumps(sorted(entries, key=lambda e: e["name"]))


def load_skill(name: str) -> str:
    """
    Return the full instruction text for one skill by name.

    Call this BEFORE acting on a task that matches a skill's description in the
    catalog (see list_skills) — e.g. before touching Notion, before citing a
    database-backed number, before writing or editing code, before claiming
    something is done/removed/verified.
    """
    catalog = _catalog()
    entry = catalog.get(name)
    if not entry:
        available = ", ".join(sorted(catalog.keys())) or "(none configured)"
        return f"skill not found: '{name}'. Available skills: {available}"
    return entry["body"]
