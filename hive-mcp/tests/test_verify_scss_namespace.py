"""Regression tests: verify_claims must catch a staged SCSS file that references a
shared variable bare when the SAME file already references that exact variable
through its own @use namespace alias elsewhere.

Confirmed live 2026-08-05, twice in back-to-back identical test runs: a Coder
correctly wrote `index.$success-bg` / `index.$success` on one run, then wrote bare
`$success-bg` / `$success` on the very next run of the SAME task against the SAME
file -- which already uses `index.$success-bg` / `index.$success` in an existing
.badgeBoth rule a few lines above. A prose "match the file's own convention"
instruction (NAMESPACE-CONSISTENCY rule in engineering.yaml) was measured
inconsistent: correct on one run, wrong on the next. Bare `$success-bg` is
undefined in a file that only `@use`s styles/_index as `index` (no wildcard import),
so this is a real compile-time error, not a style nit.
"""
from tools import verify


def _stage(tmp_path, rel_path: str, content: str):
    p = tmp_path / (rel_path + ".hive_proposed")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


_FILE_HEADER = (
    '@use "@/styles/_index" as index;\n\n'
    ".badgeBoth {\n"
    "  background: index.$success-bg;\n"
    "  color: index.$success;\n"
    "}\n\n"
)


def test_bare_variable_flagged_when_same_name_is_prefixed_elsewhere_in_file(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    _stage(
        tmp_path, "x.module.scss",
        _FILE_HEADER + ".statusBadge {\n  background: $success-bg;\n  color: $success;\n}\n",
    )

    answer = "I've added the statusBadge class."

    report = verify.verify_claims(answer)

    assert "NAMESPACE MISMATCH" in report
    assert "$success-bg" in report
    assert "index.$success-bg" in report


def test_correctly_prefixed_new_variable_use_reports_no_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    _stage(
        tmp_path, "x.module.scss",
        _FILE_HEADER + ".statusBadge {\n  background: index.$success-bg;\n  color: index.$success;\n}\n",
    )

    answer = "I've added the statusBadge class."

    report = verify.verify_claims(answer)

    assert "NAMESPACE MISMATCH" not in report


def test_file_with_no_use_alias_is_not_checked(tmp_path, monkeypatch):
    """A file with no @use ... as alias has no namespace convention to be
    inconsistent with -- bare $variables there are the file's own local variables,
    not a missed prefix."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    _stage(
        tmp_path, "x.module.scss",
        "$local-color: #fff;\n.card {\n  background: $local-color;\n}\n",
    )

    answer = "I've added the card class."

    report = verify.verify_claims(answer)

    assert "NAMESPACE MISMATCH" not in report


def test_non_scss_staged_file_is_not_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    _stage(
        tmp_path, "x.ts",
        'import index from "./index";\nconst y = index.$success + "$success";\n',
    )

    answer = "I've added a new constant."

    report = verify.verify_claims(answer)

    assert "NAMESPACE MISMATCH" not in report


def test_a_bare_variable_with_no_elsewhere_evidence_is_still_flagged(tmp_path, monkeypatch):
    """Confirmed live 2026-08-06: a Coder used bare $info-bg/$info-dark for the
    FIRST time anywhere in a file whose only prior variable usages were the
    $success family -- the original, narrower version of this check required
    "already prefixed elsewhere in this file" as proof, so it had no evidence for
    $info-* to compare against and stayed silent on a real compile error. The
    check was generalised: a file with only named (non-wildcard) @use imports and
    no local declaration for a bare variable cannot resolve it, elsewhere-evidence
    or not."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    _stage(
        tmp_path, "x.module.scss",
        _FILE_HEADER + ".statusBadge {\n  background: $totally-unrelated-var;\n}\n",
    )

    answer = "I've added the statusBadge class."

    report = verify.verify_claims(answer)

    assert "NAMESPACE MISMATCH" in report
    assert "$totally-unrelated-var" in report
    assert "whichever of index" in report  # the general-tier message, no false "elsewhere" claim


def test_a_locally_declared_variable_is_never_flagged(tmp_path, monkeypatch):
    """A file may legitimately define its OWN local $variable alongside named
    imports -- that must stay bare, not get an alias prefix forced onto it."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    _stage(
        tmp_path, "x.module.scss",
        _FILE_HEADER + "$local-radius: 9999px;\n.statusBadge {\n  border-radius: $local-radius;\n}\n",
    )

    answer = "I've added the statusBadge class."

    report = verify.verify_claims(answer)

    assert "NAMESPACE MISMATCH" not in report


def test_a_wildcard_import_disables_the_check_entirely(tmp_path, monkeypatch):
    """@use '...' as *; legitimises bare $variable references from that module --
    a file with one gets none of these checks, specific or general."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    _stage(
        tmp_path, "x.module.scss",
        '@use "@/styles/_variables" as *;\n\n'
        ".statusBadge {\n  background: $success-bg;\n  color: $info-dark;\n}\n",
    )

    answer = "I've added the statusBadge class."

    report = verify.verify_claims(answer)

    assert "NAMESPACE MISMATCH" not in report
