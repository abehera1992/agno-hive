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
