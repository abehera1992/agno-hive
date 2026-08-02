# Skills Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the always-on instruction blobs in `swarm/team.py` and `hive-mcp/main.py` with an always-on L1 skill catalog (name + one-line description) plus a `load_skill(name)` MCP tool that fetches one skill's full text on demand, scoped per agent role via each team's existing YAML.

**Architecture:** `hive-mcp/tools/skills.py` discovers `SKILL.md` files under `hive-mcp/skills/` (generic) and `PROJECT_ROOT/skills/` (project-specific), plus one synthesized `code-conventions` skill from existing lint-rule config. Two new MCP tools — `list_skills()` (orchestrator-only, fetched once per run by `swarm/team.py`) and `load_skill(name)` (agent-callable) — replace ~24K chars of always-on instruction text with a ~600-char catalog plus on-demand full text.

**Tech Stack:** Python 3.12, FastMCP (hive-mcp), Agno `Team`/`Agent` (swarm), PyYAML for SKILL.md frontmatter and team YAML, pytest.

## Global Constraints

- hive-mcp stays project-independent (per `docs/superpowers/specs/2026-08-01-skills-architecture-design.md`, Non-Goals): nothing EkamApp-specific is written into `agno-hive/hive-mcp/`.
- **Scope boundary, decided during planning:** this plan covers the agno-hive-repo mechanism only — the skill loader, the two MCP tools, the 5 generic hive-mcp skills, and the team-YAML wiring. Migrating EkamApp's `hive.md`/`patterns/*.md` into `EkamApp/skills/` (the `project-overview`, `code-generation-guards`, `frontend-conventions` skills named in the design doc) is a separate follow-up plan — those files live in EkamApp, which per this project's standing rule is edited only through `agno_run` review, not directly. The mechanism built here already supports a project skills directory (`PROJECT_ROOT/skills/`) with no further code changes needed when that follow-up happens.
- `read_only` request handling (`_MUTATING_TOOLS`, `_strip_mutating`) is untouched — `list_skills`/`load_skill` are read-only tools and need no entry in that set.
- Every new/changed Python file matches the codebase's existing style: no type-checking framework beyond stdlib type hints, docstrings explain *why* not *what*, `pytest` for tests (see `tests/test_config.py` for the existing test style — reload-safe config access, no mocking framework).
- Never rename `AgentSpec` fields or `make_agent_from_spec`'s existing positional signature — only add new optional parameters, so `teams/parallel-review.yaml`, `teams/planning.yaml`, `teams/sprint-master.yaml` keep working unchanged if they don't opt into `skills:`.

---

## File Structure

```
hive-mcp/
  skills/                          # NEW — generic protocol skills, ships with hive-mcp
    notion-grounding/SKILL.md
    db-facts/SKILL.md
    counting-marker/SKILL.md
    file-write-review/SKILL.md
    verification-discipline/SKILL.md
  tools/
    skills.py                      # NEW — discovery, list_skills(), load_skill(name)
  tests/                            # NEW — hive-mcp had no test suite; mirrors tools/ layout
    conftest.py
    test_skills.py
  main.py                          # MODIFIED — register tools, trim _instructions, drop _conventions_block

swarm/
  agents.py                        # MODIFIED — format_skill_catalog(), make_agent_from_spec() gains skill_catalog kwarg
  team.py                          # MODIFIED — _fetch_skill_catalog(), _build_team() + call sites, trim _COORDINATOR_INSTRUCTIONS

api/
  models.py                        # MODIFIED — AgentSpec.skills field

teams/
  engineering.yaml                 # MODIFIED — tools: gains load_skill, agents gain skills:, migrated rules become pointers
  parallel-review.yaml             # MODIFIED — same, where applicable
  sprint-master.yaml               # MODIFIED — same, where applicable

training/eval/
  agent_cases.json                 # MODIFIED — 4 new skill-dependent cases
```

---

### Task 1: Skill catalog core (`hive-mcp/tools/skills.py`)

**Files:**
- Create: `hive-mcp/tools/skills.py`
- Create: `hive-mcp/tests/__init__.py` (empty)
- Create: `hive-mcp/tests/conftest.py`
- Create: `hive-mcp/tests/test_skills.py`

**Interfaces:**
- Produces: `list_skills() -> str` (JSON array of `{name, description, source}`, sorted by name)
- Produces: `load_skill(name: str) -> str` (full body text, or a "skill not found" message naming what IS available)
- Internal names later tasks depend on: `_HIVE_SKILLS_DIR` (module-level `Path`, monkeypatchable), `_catalog() -> dict[str, dict]`

- [ ] **Step 1: Write the failing tests**

```python
# hive-mcp/tests/conftest.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
```

```python
# hive-mcp/tests/test_skills.py
import json
from pathlib import Path

import config
from tools import skills


def _write_skill(root: Path, name: str, description: str, body: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def test_list_skills_merges_hive_and_project_sources(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive_skills"
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_skill(hive_dir, "notion-grounding", "Notion rules", "body-a")
    _write_skill(project_root / "skills", "project-overview", "Project snapshot", "body-b")
    monkeypatch.setattr(skills, "_HIVE_SKILLS_DIR", hive_dir)
    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)

    entries = {e["name"]: e for e in json.loads(skills.list_skills())}

    assert entries["notion-grounding"]["description"] == "Notion rules"
    assert entries["notion-grounding"]["source"] == "hive-mcp"
    assert entries["project-overview"]["source"] == "project"
    assert "body-a" not in skills.list_skills()  # L1 catalog never carries full body


def test_project_skill_overrides_hive_skill_of_same_name(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive_skills"
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_skill(hive_dir, "shared-name", "generic version", "generic body")
    _write_skill(project_root / "skills", "shared-name", "project version", "project body")
    monkeypatch.setattr(skills, "_HIVE_SKILLS_DIR", hive_dir)
    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)

    assert skills.load_skill("shared-name") == "project body"


def test_load_skill_returns_full_body(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive_skills"
    monkeypatch.setattr(skills, "_HIVE_SKILLS_DIR", hive_dir)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "no-project-skills-dir")
    _write_skill(hive_dir, "counting-marker", "Count rules", "line one\nline two")

    assert skills.load_skill("counting-marker") == "line one\nline two"


def test_load_skill_unknown_name_lists_available(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive_skills"
    monkeypatch.setattr(skills, "_HIVE_SKILLS_DIR", hive_dir)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "no-project-skills-dir")
    _write_skill(hive_dir, "db-facts", "DB rules", "body")

    result = skills.load_skill("does-not-exist")

    assert "not found" in result
    assert "db-facts" in result


def test_dynamic_code_conventions_skill_from_lint_config(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_HIVE_SKILLS_DIR", tmp_path / "empty_hive_skills")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "no-project-skills-dir")
    monkeypatch.setattr(config, "CODE_LINT_FORBID", ['className="::use styles.x, not a bare className string'])
    monkeypatch.setattr(config, "CODE_LINT_REQUIRE", [r"styles\.::components must reference SCSS module classes"])

    entries = {e["name"] for e in json.loads(skills.list_skills())}
    body = skills.load_skill("code-conventions")

    assert "code-conventions" in entries
    assert "use styles.x, not a bare className string" in body
    assert "components must reference SCSS module classes" in body


def test_no_dynamic_skill_when_no_lint_rules_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_HIVE_SKILLS_DIR", tmp_path / "empty_hive_skills")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "no-project-skills-dir")
    monkeypatch.setattr(config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(config, "CODE_LINT_REQUIRE", [])

    entries = {e["name"] for e in json.loads(skills.list_skills())}

    assert "code-conventions" not in entries
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hive-mcp && python -m pytest tests/test_skills.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools.skills'`

- [ ] **Step 3: Write the implementation**

```python
# hive-mcp/tools/skills.py
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
        "body": parts[2].lstrip("\n").rstrip() + "",
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

    Moved here from hive-mcp/main.py's _conventions_block(): the rules were always
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hive-mcp && python -m pytest tests/test_skills.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add hive-mcp/tools/skills.py hive-mcp/tests/
git commit -m "feat(hive-mcp): add skill catalog core (list_skills, load_skill)"
```

---

### Task 2: Author the 5 generic skills and consolidate duplicated rules

**Files:**
- Create: `hive-mcp/skills/notion-grounding/SKILL.md`
- Create: `hive-mcp/skills/db-facts/SKILL.md`
- Create: `hive-mcp/skills/counting-marker/SKILL.md`
- Create: `hive-mcp/skills/file-write-review/SKILL.md`
- Create: `hive-mcp/skills/verification-discipline/SKILL.md`

**Interfaces:**
- Consumes: none (static content files, no code dependency)
- Produces: 5 skill names discoverable by Task 1's `_catalog()` once `hive-mcp/skills/` exists — used by Task 3's tests and Task 6's team YAML

These move content **verbatim** from two places that currently say nearly the same
thing in two files: `hive-mcp/main.py`'s `_instructions` (Notion/DB/counting/file-write
blocks) and `swarm/team.py`'s `_COORDINATOR_INSTRUCTIONS` ("Counts must be tool-filled",
"Editing files", "run_command is READ-ONLY", "File write review"). Each skill below is
now the ONE canonical copy; Task 3 deletes both duplicates and replaces them with a
catalog pointer.

- [ ] **Step 1: Create `notion-grounding`**

```markdown
---
name: notion-grounding
description: Rules for creating/reading/updating Notion pages via notion_* tools — required before any Notion write.
---
Notion GROUNDING rules (MANDATORY — read before you write, never guess):

1. NEVER fabricate or guess a Notion page_id. Resolve real ids first:
   notion_find_work_item(query) for a work item (e.g. "Phase 6"),
   notion_items_in_sprint(...) / notion_search() / notion_query_database() for the rest.
2. BEFORE any notion_update_page_props or relation change, call
   notion_get_item_with_relations(page_id) to READ the page's current properties and
   relations. Never set a relation (Parent item 1, Sprint, Work Items) you have not
   just read.
3. Do NOT confuse "Spec" (a doc-link property) with "Parent item 1" (the work-item
   parent). Change a parent only if the task explicitly asks, and only to a page you
   confirmed is a Work Item via notion_get_item_with_relations — never to a
   Spec/doc URL.
4. In notion_update_page_props send ONLY the properties the task names. Do NOT
   re-send Parent item 1 or any relation you were not asked to change (omitted
   properties are left as-is).
5. Never report an item as "orphaned"/missing a value from assumption — read it
   first and report the actual current state.

If a Notion/Google tool returns "action_pending", STOP immediately. Do not call any
other tool. Tell the human the action is staged — they approve via the hive CLI.
Do NOT call confirm_action yourself.
```

- [ ] **Step 2: Create `db-facts`**

```markdown
---
name: db-facts
description: When a value lives in a database table, treat the live table as authoritative over a file grep — load before answering any DB-backed fact question.
---
Database-backed facts (when db_query / db_schema are available): if a value is
stored in a database table (a count of rows, the current value of a column, "how
many X have status Y"), the LIVE TABLE is the source of truth — a file grep of a
seed/migration/code fallback can be stale or incomplete. Call db_schema(table) to
confirm the exact schema + column names, then db_query with an aggregate
(SELECT col, count(*) ... GROUP BY col) to get the authoritative number. Report the
DB result as the total; treat file greps as SUPPLEMENTARY subtotals (and note when
the DB and the code/seed disagree — they often do).
```

- [ ] **Step 3: Create `counting-marker`**

```markdown
---
name: counting-marker
description: How to report any count, total, or "how many" — never write the number yourself; use the deterministic count mechanism.
---
Counts must be tool-filled, NEVER written by you (CRITICAL). You are FORBIDDEN from
writing any count / total / "how many" / "all" as a bare number you computed by
reading — reading and tallying is unreliable and treated as fabrication. Instead:

- If the count is over files in the repo: emit a COUNT MARKER and the system fills
  in the EXACT ripgrep count for you:
      [[COUNT pattern=`<ripgrep-regex>` glob=`<glob>`]]
  Example: 'There are [[COUNT pattern=`: *12\.0` glob=`**/gst_resolver.py`]] entries
  at 12%.' pattern = a ripgrep regex (backtick-delimited); glob = files to scan
  (e.g. **/gst_resolver.py, **/*.py). The system replaces the marker with the real
  number AFTER you finish — you never supply the digit, so the count cannot be
  wrong. Use ONE marker per distinct count.
- For a count of rows in a DATABASE table, use db_query (SELECT ... COUNT(*))
  instead — see the db-facts skill. Do NOT grep files for a value that lives in
  the DB.
- If you already ran count_matches / grep -c yourself and have the exact tool
  output, you may state that number directly. Otherwise ALWAYS use the marker —
  never guess.
- Research thoroughly first: a value may live in more than one place (e.g. a DB
  table AND a code fallback), so search across the WHOLE repo to confirm you
  found every occurrence before stating a total, and state which sources you
  checked. If the target is a big literal (a large dict/list/table/seed block),
  GREP it — do not scroll it and guess.
```

- [ ] **Step 4: Create `file-write-review`**

```markdown
---
name: file-write-review
description: How to edit files (apply_diff vs write_file), what review_pending means, and what run_command may and may not do — load before making any file change.
---
File editing rules:

- For EXISTING files: ALWAYS use apply_diff(), NEVER write_file(). apply_diff makes
  surgical line-level changes; write_file rewrites the whole file.
- Use write_file() ONLY when creating a brand-new file that does not exist yet.
- Read the file first (get_file_content) to get the exact old_string to replace.
- To APPEND content: include the anchor line in BOTH old_string AND new_string,
  then add the new content after it:
      old_string = "last_line"
      new_string = "last_line\nnew_content"
  Never drop existing lines from new_string unless intentionally deleting them.

run_command is READ-ONLY (CRITICAL):
- run_command is for tests, linters, grep, git status ONLY.
- NEVER use run_command to modify files — no >, >>, sed -i, tee, perl -i.
- "add a line", "update a comment", "change X to Y" → use apply_diff().
- Attempting to write via run_command will be BLOCKED by the server.
- For full shell access (npm install, docker compose, etc.) use run_shell().

File write review (CRITICAL):
- If write_file() or apply_diff() returns "review_pending", the proposed change is
  staged for human review. STOP immediately — do not call any other tool.
- For apply_diff on the SAME file: you MAY continue calling apply_diff on that
  file — each call accumulates into the same .hive_proposed file. AFTER each
  apply_diff, read the staged file (<path>.hive_proposed) via get_file_content to
  verify what is already applied. Then apply ONLY the NEXT distinct change not yet
  in the staged file. NEVER repeat a change already staged.
  Correct pattern (import + function body):
    1st call: update import line  → read .hive_proposed → verify import added
    2nd call: add usage in body   → review_pending (now STOP)
- STOP and report "review_pending: <path>" ONLY when: (a) all changes to the
  current file are staged, OR (b) you are about to write a DIFFERENT file.
- confirm_write and reject_write do NOT exist — you cannot approve writes. The
  human selects confirm/reject in their CLI — your job ends when you report.
- If the user asks to "delete", "undo", or "reject" a .hive_proposed file: do NOT
  call run_command, run_shell, or any tool. Reply: "Type /reject <path> or
  /cleanup in your hive CLI to discard the pending change."
```

- [ ] **Step 5: Create `verification-discipline`**

```markdown
---
name: verification-discipline
description: How to check a claim before stating it as fact — required before any answer that names a symbol, file:line, or claims something is done/removed/verified.
---
Verification & completion-claim discipline (MANDATORY — applies to ALL claims):

When you state whether something is implemented / done / removed / present / fixed,
base it ONLY on code you actually READ this run (get_file_content / search_files)
and cite the exact file path + line + the literal code as evidence.

BEFORE returning any answer that names a symbol, a file:line, or an API route, call
verify_claims(your_draft_answer). It greps every claim against the repo and reports
what does not exist. If it returns NOT FOUND or BAD, the claim is fabricated — fix
the answer, do not return it. The most common failure is naming a symbol that
merely RESEMBLES the answer: a single-item function offered when asked about a
batch operation, or a neighbouring symbol from the same file. Existing is not the
same as doing what was asked, and verify_claims cannot catch that — it only proves
the name exists.

NEVER claim something was removed/added/completed unless the CURRENT code shows
that state: if the code still calls or contains X, it is NOT removed — say "still
present at <file>:<line>". Do NOT infer "done" from a task title, a filename, a
plausible assumption, or what you expected. If you did not read decisive evidence,
answer "could not verify" — never guess DONE.
```

- [ ] **Step 6: Verify discovery picks up all 5 real files**

Run:
```bash
cd hive-mcp && python -c "
from tools import skills
import json
names = sorted(e['name'] for e in json.loads(skills.list_skills()))
print(names)
assert names == ['counting-marker', 'db-facts', 'file-write-review', 'notion-grounding', 'verification-discipline'], names
print('OK')
"
```
Expected: `OK` printed (this exercises `_HIVE_SKILLS_DIR` at its real, non-monkeypatched location for the first time)

- [ ] **Step 7: Commit**

```bash
git add hive-mcp/skills/
git commit -m "feat(hive-mcp): author the 5 generic protocol skills"
```

---

### Task 3: Register tools in hive-mcp/main.py, trim `_instructions`, remove `_conventions_block`

**Files:**
- Modify: `hive-mcp/main.py:24-40` (imports), `hive-mcp/main.py:80-186` (`_instructions`), `hive-mcp/main.py:153-186` (`_conventions_block` + assembly), `hive-mcp/main.py:229-238` (tool registration)

**Interfaces:**
- Consumes: `list_skills`, `load_skill` from Task 1's `hive-mcp/tools/skills.py`
- Produces: `list_skills`/`load_skill` registered as MCP tools, reachable at `http://<host>:9000/mcp` under those names — Task 6 (team YAML) and Task 5 (`swarm/team.py`) depend on these names existing on the wire

- [ ] **Step 1: Add the import**

In `hive-mcp/main.py`, after the existing `from tools.verify import verify_claims` line:

```python
from tools.skills import list_skills, load_skill
```

- [ ] **Step 2: Delete the 5 migrated blocks from `_instructions`, replace with a pointer**

Replace the whole `_instructions = (...)` assignment (currently `hive-mcp/main.py:80-150`) with:

```python
_instructions = (
    "You are connected to a project via hive-mcp. "
    "The project files are at the root level — use get_file_content, find_files, "
    "and search_files to explore before making any changes. "
    ""
    "Skills: call list_skills() to see the always-loaded catalog of available "
    "protocols (Notion grounding, DB-backed facts, counting, file-write review, "
    "verification discipline, and any project-specific skills). Call "
    "load_skill(name) to fetch ONE skill's full instructions before acting on a "
    "task its description matches — e.g. before writing to Notion, before citing "
    "a database-backed number, before making any file change, before claiming "
    "something is done/removed/verified. Do not load a skill unrelated to the "
    "current task."
)
```

(The Notion / DB-backed facts / counting-marker / file-editing / review-pending /
verification-discipline paragraphs that were here are now `hive-mcp/skills/*/SKILL.md`
from Task 2, verbatim.)

- [ ] **Step 3: Delete `_conventions_block()` and its call**

Delete the `_conventions_block()` function definition (`hive-mcp/main.py:153-183`) and
change:

```python
_instructions = _instructions + " " + _conventions_block()

mcp = FastMCP(config.MCP_NAME, instructions=_instructions)
```

to:

```python
mcp = FastMCP(config.MCP_NAME, instructions=_instructions)
```

(`code-conventions` is now a dynamic entry in the skill catalog itself — Task 1's
`_dynamic_skills()` — served through `load_skill('code-conventions')` instead of
always appended.)

- [ ] **Step 4: Register the two tools**

In the tool-registration section (`hive-mcp/main.py`, right after `_tool(verify_claims)`):

```python
_tool(list_skills)
_tool(load_skill)
```

- [ ] **Step 5: Verify the server still imports cleanly**

Run: `cd hive-mcp && python -c "import main; print('OK')"`
Expected: `OK` (no `NameError`/`ImportError` — confirms `_conventions_block` references were fully removed and the two new tools import without error)

- [ ] **Step 6: Commit**

```bash
git add hive-mcp/main.py
git commit -m "feat(hive-mcp): register skill tools, trim always-on instructions"
```

---

### Task 4: `AgentSpec.skills` + `format_skill_catalog()` + `make_agent_from_spec` wiring

**Files:**
- Modify: `api/models.py` (`AgentSpec`)
- Modify: `swarm/agents.py` (`make_agent_from_spec`, new `format_skill_catalog`)
- Test: `tests/test_agents_skills.py`

**Interfaces:**
- Consumes: nothing new (pure Python, no MCP call — the catalog is handed in already-fetched)
- Produces: `format_skill_catalog(catalog: list[dict], names: list[str] | None) -> str` — used by Task 5's `swarm/team.py` for the coordinator's own (unfiltered) catalog text
- Produces: `make_agent_from_spec(spec, *mcps, skill_catalog: list[dict] | None = None) -> Agent` — new keyword-only param, defaults preserve current behavior exactly when omitted

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agents_skills.py
from api.models import AgentSpec
from swarm.agents import format_skill_catalog, make_agent_from_spec


_CATALOG = [
    {"name": "notion-grounding", "description": "Notion rules", "source": "hive-mcp"},
    {"name": "db-facts", "description": "DB rules", "source": "hive-mcp"},
]


def test_format_skill_catalog_lists_all_when_names_is_none():
    text = format_skill_catalog(_CATALOG, None)

    assert "notion-grounding: Notion rules" in text
    assert "db-facts: DB rules" in text


def test_format_skill_catalog_filters_to_given_names():
    text = format_skill_catalog(_CATALOG, ["db-facts"])

    assert "db-facts: DB rules" in text
    assert "notion-grounding" not in text


def test_format_skill_catalog_empty_catalog_returns_empty_string():
    assert format_skill_catalog([], None) == ""


def test_format_skill_catalog_no_matching_names_returns_empty_string():
    assert format_skill_catalog(_CATALOG, ["does-not-exist"]) == ""


def test_make_agent_from_spec_appends_filtered_catalog_to_instructions(monkeypatch):
    monkeypatch.setattr("swarm.agents.config.inference_backend", "ollama")
    spec = AgentSpec(
        name="Coder", role="engineer", model="qwen2.5-coder:32b",
        instructions=["base instruction"], skills=["db-facts"],
    )

    agent = make_agent_from_spec(spec, skill_catalog=_CATALOG)

    joined = "\n".join(agent.instructions)
    assert "base instruction" in joined
    assert "db-facts: DB rules" in joined
    assert "notion-grounding" not in joined


def test_make_agent_from_spec_without_skill_catalog_is_unchanged(monkeypatch):
    monkeypatch.setattr("swarm.agents.config.inference_backend", "ollama")
    spec = AgentSpec(
        name="Coder", role="engineer", model="qwen2.5-coder:32b",
        instructions=["base instruction"],
    )

    agent = make_agent_from_spec(spec)

    assert agent.instructions == ["base instruction"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_agents_skills.py -v`
Expected: FAIL — `AgentSpec` has no field `skills`, `format_skill_catalog` doesn't exist, `make_agent_from_spec` doesn't accept `skill_catalog`

- [ ] **Step 3: Add `AgentSpec.skills`**

In `api/models.py`, in the `AgentSpec` class:

```python
class AgentSpec(BaseModel):
    name: str
    role: str
    model: str
    instructions: list[str]
    description: str | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None       # names from the skill catalog this role should
                                          # see in its L1 index. None means "no skills
                                          # advertised" — the agent can still call
                                          # load_skill(name) directly if it knows the
                                          # name, but nothing is proactively listed.
```

- [ ] **Step 4: Add `format_skill_catalog` and wire `make_agent_from_spec`**

In `swarm/agents.py`, add the function above `make_agent_from_spec` and update the
function signature and body:

```python
def format_skill_catalog(catalog: list[dict], names: list[str] | None) -> str:
    """Render the L1 skill catalog as one instruction-list entry.

    names=None means "show everything" (the coordinator's case — it can delegate
    to any skill-relevant task). A non-None list filters to those names only,
    mirroring how spec.tools already filters the MCP tool surface per agent.
    """
    if not catalog:
        return ""
    entries = catalog if names is None else [c for c in catalog if c["name"] in names]
    if not entries:
        return ""
    lines = [
        "Available skills — call load_skill(name) for the full text of ONE before "
        "acting on a task it covers. Do not load a skill unrelated to the task:"
    ]
    for e in sorted(entries, key=lambda c: c["name"]):
        lines.append(f"  - {e['name']}: {e['description']}")
    return "\n".join(lines)


def make_agent_from_spec(spec, *mcps: MCPTools, skill_catalog: list[dict] | None = None) -> Agent:
    """Build an Agent from a dynamic spec.

    If spec.tools lists tool names, only those Function objects are passed to the
    agent — everything else in the connected MCPs is hidden from the model.
    Falls back to all MCPs when spec.tools is absent or none of the names match.

    skill_catalog (already fetched once per run by swarm/team.py) is filtered to
    spec.skills and appended as one more instruction entry — the always-on L1
    index this agent sees. Omitted or empty catalog leaves instructions unchanged.
    """
    if spec.tools:
        all_funcs: dict = {}
        for mcp in mcps:
            all_funcs.update(mcp.functions)
        scoped = [all_funcs[t] for t in spec.tools if t in all_funcs]
        agent_tools = scoped if scoped else list(mcps)
    else:
        agent_tools = list(mcps)

    instructions = list(spec.instructions)
    catalog_text = format_skill_catalog(skill_catalog or [], getattr(spec, "skills", None))
    if catalog_text:
        instructions.append(catalog_text)

    return Agent(
        name=spec.name,
        model=get_model(spec.model, config.ollama_host),
        tools=agent_tools,
        instructions=instructions,
        role=spec.role,
        description=spec.description,
        markdown=True,
        add_name_to_context=True,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_agents_skills.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add api/models.py swarm/agents.py tests/test_agents_skills.py
git commit -m "feat(swarm): add AgentSpec.skills and per-agent catalog filtering"
```

---

### Task 5: Wire skill catalog fetch into `swarm/team.py`, trim `_COORDINATOR_INSTRUCTIONS`

**Files:**
- Modify: `swarm/team.py` (imports, new `_fetch_skill_catalog`, `_build_team`, both call sites in `run_task_async` and `run_task_stream`, trim `_COORDINATOR_INSTRUCTIONS`)
- Test: `tests/test_team_skills.py`

**Interfaces:**
- Consumes: `format_skill_catalog` from Task 4's `swarm/agents.py`; the `list_skills` MCP tool from Task 3
- Produces: `_fetch_skill_catalog(hive_mcp_url) -> list[dict]` and `_build_team(..., skill_catalog=None)` — both used by the two run functions; the shape (`list[dict]` with `name`/`description`/`source` keys) matches Task 1's `list_skills()` JSON exactly

- [ ] **Step 1: Write the failing test**

```python
# tests/test_team_skills.py
import json
from types import SimpleNamespace

import pytest

from swarm import team


class _FakeToolResult:
    def __init__(self, text: str):
        self.content = [SimpleNamespace(text=text)]


class _FakeSession:
    def __init__(self, catalog_json: str):
        self._catalog_json = catalog_json

    async def initialize(self):
        return None

    async def call_tool(self, name: str, args: dict):
        assert name == "list_skills"
        return _FakeToolResult(self._catalog_json)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeStreamCtx:
    def __init__(self, session: _FakeSession):
        self._session = session

    async def __aenter__(self):
        return (None, None, None)

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_fetch_skill_catalog_parses_list_skills_response(monkeypatch):
    catalog_json = json.dumps([{"name": "db-facts", "description": "x", "source": "hive-mcp"}])
    fake_session = _FakeSession(catalog_json)
    monkeypatch.setattr(team, "ClientSession", lambda *a, **k: fake_session)
    monkeypatch.setattr(team, "streamablehttp_client", lambda url: _FakeStreamCtx(fake_session))

    result = await team._fetch_skill_catalog("http://fake/mcp")

    assert result == [{"name": "db-facts", "description": "x", "source": "hive-mcp"}]


@pytest.mark.asyncio
async def test_fetch_skill_catalog_returns_empty_list_when_no_url():
    assert await team._fetch_skill_catalog(None) == []


@pytest.mark.asyncio
async def test_fetch_skill_catalog_returns_empty_list_on_connection_failure(monkeypatch):
    def _raise(url):
        raise ConnectionError("no server")
    monkeypatch.setattr(team, "streamablehttp_client", _raise)

    assert await team._fetch_skill_catalog("http://fake/mcp") == []
```

`_verify_claims` already imports `ClientSession`/`streamablehttp_client` locally inside
its own function body (not at module scope), so this test's `monkeypatch.setattr(team,
"ClientSession", ...)` requires `_fetch_skill_catalog` to import them the same
LOCAL way `_verify_claims` does — Step 3 below follows that exact pattern.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_team_skills.py -v`
Expected: FAIL — `swarm.team` has no attribute `_fetch_skill_catalog`

- [ ] **Step 3: Add `_fetch_skill_catalog`**

In `swarm/team.py`, add `import json` to the top-of-file imports (alongside the
existing `import asyncio`, `import re`, `import time`), then add this function right
after `_verify_claims` (which it mirrors):

```python
async def _fetch_skill_catalog(hive_mcp_url: str | None) -> list[dict]:
    """Fetch the L1 skill catalog once per run via hive-mcp's list_skills tool.

    Returns [] — not an error — when hive-mcp isn't connected or the call fails.
    Skills are an enhancement to instruction delivery, not a hard dependency: a run
    must still work with no catalog, exactly like _verify_claims degrades to "skip
    the check" rather than failing the run when hive-mcp is unreachable.
    """
    if not hive_mcp_url:
        return []
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    try:
        async with streamablehttp_client(hive_mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("list_skills", {})
                text = _extract_mcp_text(res)
                return json.loads(text)
    except Exception as exc:
        print(f"[team] skill catalog unavailable ({hive_mcp_url}): {exc}")
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_team_skills.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Wire `_build_team` to accept and forward `skill_catalog`**

In `swarm/team.py`, change the `_build_team` signature and body (currently
`swarm/team.py:592-626`):

```python
def _build_team(
    agent_specs: list | None,
    coordinator_model: str,
    coordinator_tools: list[str] | None,
    mode: str,
    mcp_list: list,
    instructions: list,
    *,
    name: str = "AgnoHive",
    description: str | None = None,
    read_only: bool = False,
    skill_catalog: list[dict] | None = None,
) -> Team:
    """Build a coordinator Team from agent specs (or the default Coder+Reviewer), sharing the
    already-connected `mcp_list`. Factored out of run_task_async / run_task_stream so the same
    build is reusable for router sub-teams (EK-88). `coordinator_model` is the already-resolved
    model name. `description` (default None = previous behaviour) lets the router leader route to
    this team. Behaviour is identical to the previous inline Team(...) construction when omitted.
    `skill_catalog` (default None) is forwarded to each agent's spec-based construction so its
    L1 catalog can be filtered per agent role — the default Coder+Reviewer fallback path (used
    only when agent_specs is empty) does not take a catalog; that path predates team YAMLs."""
    if agent_specs:
        members = [make_agent_from_spec(spec, *mcp_list, skill_catalog=skill_catalog) for spec in agent_specs]
    else:
        members = [make_coder(*mcp_list), make_reviewer(*mcp_list)]
    return Team(
        name=name,
        description=description,
        mode=mode,
        model=get_model(coordinator_model, config.ollama_host),
        members=members,
        tools=_scope_coordinator_tools(coordinator_tools, mcp_list, read_only),
        instructions=instructions,
        show_members_responses=True,
        share_member_interactions=True,
        add_member_tools_to_context=True,
        markdown=True,
        max_iterations=config.max_iterations,
    )
```

- [ ] **Step 6: Wire `run_task_async`**

In `swarm/team.py`'s `run_task_async` (currently lines 769-857), move the
`all_mcp_urls` computation earlier and fetch the catalog alongside the existing
`asyncio.gather`:

```python
    effective_mcp_url = mcp_url or config.mcp_url
    effective_coordinator = coordinator_model or config.leader_model

    from swarm.sessions import get_context as get_session_context

    async def _load_session_context():
        if session_id:
            try:
                return await get_session_context(session_id)
            except Exception as exc:
                print(f"[team] session context warning: {exc}")
        return "", []

    # Computed here (not after the gather, as before) because the skill-catalog
    # fetch below needs it, and connecting MCPTools further down needs the same
    # value — one computation, not two that could silently diverge.
    all_mcp_urls = [u for u in (mcp_urls or []) + [effective_mcp_url] if u]

    failure_context, (session_summary, session_messages), skill_catalog = (
        await asyncio.gather(
            load_failure_context(project_id),
            _load_session_context(),
            _fetch_skill_catalog(all_mcp_urls[0] if all_mcp_urls else None),
        )
    )

    instructions = list(_COORDINATOR_INSTRUCTIONS)
    if skill_catalog:
        instructions += ["", format_skill_catalog(skill_catalog, None)]
    if failure_context:
        instructions += ["", failure_context]
```

(everything from `if session_summary:` through the end of the existing session-context
block is unchanged — only the two new lines above and the moved `all_mcp_urls` line
are added; delete the old `all_mcp_urls = [...]` line that used to appear later, right
before the `async with AsyncExitStack()` block, since it now lives above the gather.)

Then update the `_build_team` call in the same function:

```python
        team = _build_team(
            _specs, effective_coordinator, _ctools, mode, mcp_list, instructions,
            read_only=read_only, skill_catalog=skill_catalog,
        )
```

Add the import at the top of `swarm/team.py`:

```python
from .agents import make_coder, make_reviewer, make_agent_from_spec, get_model, format_skill_catalog
```

- [ ] **Step 7: Wire `run_task_stream` identically**

Apply the same three changes (moved `all_mcp_urls`, `asyncio.gather` with
`_fetch_skill_catalog`, `instructions` gains the catalog line, `_build_team` call gains
`skill_catalog=skill_catalog`) to `run_task_stream` (currently lines 629-719) — the
existing code in that function is structurally identical to `run_task_async`'s.

- [ ] **Step 8: Trim `_COORDINATOR_INSTRUCTIONS`**

Delete these two blocks from `_COORDINATOR_INSTRUCTIONS` (`swarm/team.py:17-235`),
now covered by `hive-mcp/skills/counting-marker/SKILL.md` and
`hive-mcp/skills/file-write-review/SKILL.md` from Task 2:

1. The `"── Counts must be tool-filled, NEVER written by you (CRITICAL) ──"` block
   through its blank-string separator (currently lines 45-58).
2. The `"── Editing files (CRITICAL) ────────────────────────────────────"` block,
   the `"── run_command is READ-ONLY (CRITICAL) ─────────────────────────"` block, and
   the `"── File write review (CRITICAL) ───────────────────────────────"` block —
   three consecutive blocks (currently lines 164-228).

Replace the deleted region with one short pointer line inserted where the first
deleted block was:

```python
    "── Skills — on-demand instruction detail (CRITICAL) ─────────────",
    "  Call load_skill(name) for the full text of a skill BEFORE acting on a task",
    "  it covers — available skills are listed above/below in this prompt. Do NOT",
    "  guess counting-marker or file-write-review behaviour from memory; load it.",
    "",
```

- [ ] **Step 9: Full test suite sanity check**

Run: `python -m pytest tests/ -v`
Expected: PASS — no regressions in `test_config.py`, `test_bootstrap.py`,
`test_sessions.py`, plus the new `test_agents_skills.py` and `test_team_skills.py`

- [ ] **Step 10: Commit**

```bash
git add swarm/team.py tests/test_team_skills.py
git commit -m "feat(swarm): fetch skill catalog per run, trim coordinator instructions"
```

---

### Task 6: Update team YAMLs — expose `load_skill`, add per-agent `skills:`

**Files:**
- Modify: `teams/engineering.yaml`
- Modify: `teams/parallel-review.yaml`
- Modify: `teams/sprint-master.yaml`

**Interfaces:**
- Consumes: `AgentSpec.skills` from Task 4, `load_skill`/`list_skills` tool names from Task 3
- Produces: none (terminal — config only, validated by loading and running the team)

- [ ] **Step 1: Update `teams/engineering.yaml`**

For each of the 6 agents, add `load_skill` to its `tools:` list and add a `skills:`
list naming the catalog entries relevant to that role. Example for `ContextRouter`
(apply the same pattern — add the two lines below — to `Researcher`, `Planner`,
`Coder`, `Executor`, `Reviewer`, each with a role-appropriate `skills:` list):

```yaml
  - name: ContextRouter
    model: llama3.1:8b
    description: Lightweight query router. Pick the fastest retrieval path and return raw results — never interpret or answer yourself.
    role: Routing agent that retrieves the right context from the right backend.
    tools:
      - list_directory_tree
      - find_files
      - search_files
      - list_directory
      - get_file_content
      - search_knowledge_graph
      - lightrag_query
      - web_search
      - web_fetch
      - load_skill
    skills:
      - verification-discipline
```

For `Coder`, add `load_skill` to `tools:` and:

```yaml
    skills:
      - file-write-review
      - counting-marker
      - verification-discipline
      - code-conventions
```

Also replace the now-redundant inline instruction in `Coder`'s `instructions:` list —

```yaml
      - "PATTERNS FIRST: Before writing any code, call get_file_content('patterns/ekam-backend.md') for Python/backend tasks or get_file_content('patterns/ekam-frontend.md') for TypeScript/frontend tasks. Also call get_file_content('patterns/ekam-code-generation-guards.md'). These files contain project-specific patterns you MUST follow exactly."
```

— with:

```yaml
      - "PATTERNS FIRST: Before writing any code, call load_skill('code-conventions') for this project's required/forbidden code patterns."
```

(This does NOT reference `code-generation-guards` or `frontend-conventions` — those
remain `patterns/*.md` `get_file_content()` calls for now, per this plan's stated
scope boundary; only `code-conventions`, which Task 1/3 already ship, is switched over.)

For `Reviewer` and `Executor`:

```yaml
    skills:
      - file-write-review
      - verification-discipline
```

For `Planner` and `Researcher`:

```yaml
    skills:
      - verification-discipline
```

- [ ] **Step 2: Update `teams/parallel-review.yaml` and `teams/sprint-master.yaml`**

Read each file first (`Read teams/parallel-review.yaml`, `Read teams/sprint-master.yaml`)
to see their actual agent names and tool lists, then apply the same two changes per
agent: add `load_skill` to `tools:`, add a `skills:` list with `verification-discipline`
at minimum (both teams are read-mostly/board-CRUD, so `notion-grounding` also belongs
on `sprint-master`'s Notion-writing agents specifically — check which agent in that
YAML calls `notion_create_page`/`notion_update_page_props` and give only that one
`skills: [notion-grounding, verification-discipline]`).

- [ ] **Step 3: Validate every team YAML still loads**

Run:
```bash
python -c "
import yaml
from pathlib import Path
from api.models import AgentSpec
for f in Path('teams').glob('*.yaml'):
    data = yaml.safe_load(f.read_text())
    agents = [AgentSpec(**a) for a in data['agents']]
    print(f.name, '->', [(a.name, a.skills) for a in agents])
"
```
Expected: prints every team's agents with their `skills` list, no `ValidationError`

- [ ] **Step 4: Commit**

```bash
git add teams/
git commit -m "feat(teams): expose load_skill, add per-agent skill catalogs"
```

---

### Task 7: Eval — skill-dependent cases, before/after comparison

**Files:**
- Modify: `training/eval/agent_cases.json` (append 4 new cases)

**Interfaces:**
- Consumes: `training/eval/agent_harness.py` (unmodified — existing `--only` filter and
  `score_grounding` already support these cases with no code change)
- Produces: none (validation artifact — a report file and a go/no-go read)

Per the design doc's Validation section: the risk of on-demand `load_skill()` not
being called is measured, not enforced. These 4 cases specifically require the
migrated skill content to answer correctly — if the model doesn't call `load_skill`
when it should, the answer is generically wrong in a detectable way (missing the
specific rule), which is exactly what `required_facts`/`forbidden_facts` scoring
already catches for the existing 40 cases.

- [ ] **Step 1: Append the 4 new cases**

Add to the JSON array in `training/eval/agent_cases.json` (matching the existing
case shape — `id`, `note`, `prompt`, `required_facts`, `forbidden_facts`):

```json
 {
  "id": "S01-counting-marker-protocol",
  "note": "tests counting-marker skill — verified hive-mcp/skills/counting-marker/SKILL.md content",
  "prompt": "READ-ONLY. If you need to report a count of matches across the repository (not a database value), what mechanism must you use to produce the number, and why can't you just count them yourself while reading?",
  "required_facts": [
   "\\[\\[COUNT",
   "ripgrep|count_matches|deterministic"
  ],
  "forbidden_facts": []
 },
 {
  "id": "S02-file-write-review-protocol",
  "note": "tests file-write-review skill — verified hive-mcp/skills/file-write-review/SKILL.md content",
  "prompt": "READ-ONLY. If apply_diff() returns 'review_pending', what are you supposed to do next, and who actually approves or rejects the change?",
  "required_facts": [
   "stop|STOP",
   "human|CLI|user approves|user selects"
  ],
  "forbidden_facts": [
   "confirm_write",
   "I (will )?approve"
  ]
 },
 {
  "id": "S03-db-facts-protocol",
  "note": "tests db-facts skill — verified hive-mcp/skills/db-facts/SKILL.md content",
  "prompt": "READ-ONLY. If a count of rows exists both in a database table and as a fallback value in a code file, and the two disagree, which one is authoritative and why?",
  "required_facts": [
   "database|db_query|live table",
   "authoritative|source of truth"
  ],
  "forbidden_facts": [
   "code (fallback )?is (the )?(authoritative|source of truth)"
  ]
 },
 {
  "id": "S04-verification-discipline-protocol",
  "note": "tests verification-discipline skill — verified hive-mcp/skills/verification-discipline/SKILL.md content",
  "prompt": "READ-ONLY. Before you return an answer that names a specific file:line or symbol, what must you do, and what tool call helps catch a fabricated one?",
  "required_facts": [
   "verify_claims",
   "read|actually read|get_file_content"
  ],
  "forbidden_facts": []
 }
```

- [ ] **Step 2: Run the new cases against the CURRENT (pre-Task-3) deployed hive-mcp**

This step establishes the baseline BEFORE the skills mechanism is deployed to the
running hive-mcp container — run it before Task 3's `git rm -f hive-mcp && compose
up -d` deployment step (outside this plan's scope; deployment is a manual step per
this project's standing hive-mcp update procedure). Record the output.

Run: `python -m training.eval.agent_harness --only S0 --repeats 3 --out training/eval/skills_baseline.json`
Expected: some or all of S01-S04 fail or score partial — the migrated content isn't
reachable via `load_skill` yet because hive-mcp hasn't been rebuilt/redeployed with
Tasks 1-3's changes.

- [ ] **Step 3: Deploy Tasks 1-3 to the running hive-mcp container, then re-run**

Follow this project's existing hive-mcp update procedure (local commit → push
`agno-hive` `main` → on the machine running hive-mcp: `docker compose -f
docker-compose.hive.yml pull hive-mcp && docker rm -f hive-mcp && docker compose -f
docker-compose.hive.yml up -d hive-mcp`).

Run: `python -m training.eval.agent_harness --only S0 --repeats 3 --out training/eval/skills_after.json`
Expected: S01-S04 pass at BEST aggregation — this confirms `load_skill` content is
reachable and the coordinator/agents call it when the question matches a skill's
description.

- [ ] **Step 4: Run the full 40+4 case suite for the overall regression check**

Run: `python -m training.eval.agent_harness --repeats 3 --aggregate best --out training/eval/full_after_skills.json`
Expected: exit code 0 (>= 95% at BEST aggregation) with no new failures among the
original 40 cases — a regression here means the `_COORDINATOR_INSTRUCTIONS` trim in
Task 5 Step 8 removed something a well-posed case still needed inline, which would
mean that block should NOT have migrated to a skill and belongs back inline.

- [ ] **Step 5: Commit the case additions (not the report JSONs — those are scratch measurement output, same convention as the rest of `training/eval/`)**

```bash
git add training/eval/agent_cases.json
git commit -m "test(eval): add 4 skill-dependent cases to validate load_skill reachability"
```

---

## Self-Review

**Spec coverage** (against `docs/superpowers/specs/2026-08-01-skills-architecture-design.md`):
- L1 catalog + on-demand `load_skill` → Tasks 1, 3 ✓
- Two catalogs (hive-mcp generic + project), project wins on collision → Task 1 (`_catalog()`) ✓
- Role-scoped catalogs per agent → Tasks 4, 6 (`AgentSpec.skills` + `format_skill_catalog`) ✓
- `hive.md`/`patterns/*.md` folding into skills → explicitly deferred, documented as a scope boundary in Global Constraints and Task 6 Step 1 — this is a deliberate plan-time decision (cross-repo, EkamApp is edited only through `agno_run` review), not a gap
- Validation via extended eval harness, no second enforcement layer → Task 7 ✓
- `_conventions_block()` → dynamic skill, not always-appended → Task 1 (`_dynamic_skills`), Task 3 (removal from main.py) ✓

**Placeholder scan:** no TBD/TODO; every step has real code or a real shell command.

**Type consistency:** `list[dict]` catalog shape (`{name, description, source}`) is
identical across `list_skills()` (Task 1), `_fetch_skill_catalog()` (Task 5), and
`format_skill_catalog()` (Task 4) — verified by re-reading each signature above.
`AgentSpec.skills: list[str] | None` (Task 4) matches the `names: list[str] | None`
parameter of `format_skill_catalog` and the `getattr(spec, "skills", None)` call site
in `make_agent_from_spec`.

---

**Plan complete and saved to `docs/superpowers/plans/2026-08-01-skills-architecture.md`.**
