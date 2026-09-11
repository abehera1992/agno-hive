"""Experiment 5 Phase 1: .hive-verify.json must be unwritable by model-directed calls.

Written against the REAL write_file/apply_diff, not a re-implementation of the check
-- the whole point is that the manifest a future verifier trusts cannot be edited
through the same tools a Coder uses for everything else.

Uses this repo's existing test convention (monkeypatch the module-local PROJECT_ROOT
bound name, per test_files.py's own _setup -- config.PROJECT_ROOT patching has no
effect here since files.py imports the name directly).
"""
from tools import files


def _setup(tmp_path, monkeypatch, write_review=False):
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "WRITE_REVIEW", write_review)
    files._last_failed_call.clear()
    files._consecutive_failures.clear()
    return tmp_path


# ── 1. write_file rejected ──────────────────────────────────────────────────────────

def test_write_file_rejects_the_manifest(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = files.write_file(".hive-verify.json", '{"version": 1}')
    assert "blocked" in result
    assert not (tmp_path / ".hive-verify.json").exists()


# ── 2. apply_diff rejected ───────────────────────────────────────────────────────────

def test_apply_diff_rejects_the_manifest(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    manifest = root / ".hive-verify.json"
    manifest.write_text('{"version": 1, "checks": {}}', encoding="utf-8")
    result = files.apply_diff(".hive-verify.json", '"version": 1', '"version": 2')
    assert "blocked" in result
    assert manifest.read_text(encoding="utf-8") == '{"version": 1, "checks": {}}'


# ── 3. equivalent spellings the existing normalization already treats as the same file

def test_write_file_rejects_backslash_spelling(tmp_path, monkeypatch):
    """is_excluded() itself normalizes backslash -> slash; the protection must too."""
    _setup(tmp_path, monkeypatch)
    result = files.write_file(".hive-verify.json", "{}")  # baseline, then:
    result2 = files.write_file(r".\.hive-verify.json", "{}")
    assert "blocked" in result and "blocked" in result2


def test_write_file_rejects_leading_dot_slash(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = files.write_file("./.hive-verify.json", "{}")
    assert "blocked" in result


def test_apply_diff_rejects_the_staged_suffix_spelling(tmp_path, monkeypatch):
    """write_file does not already strip .hive_proposed the way apply_diff does --
    without handling it here, '.hive-verify.json.hive_proposed' would slip past a
    bare string match while clearly naming the same protected file."""
    root = _setup(tmp_path, monkeypatch)
    manifest = root / ".hive-verify.json"
    manifest.write_text('{"version": 1}', encoding="utf-8")
    result = files.apply_diff(".hive-verify.json.hive_proposed", '"version": 1',
                              '"version": 2')
    assert "blocked" in result


def test_write_file_rejects_case_variant():
    """The project mount observed in this environment is a case-preserving,
    case-INsensitive Windows volume -- '.Hive-Verify.json' and '.hive-verify.json'
    are the same file on disk regardless of what a string comparison says."""
    assert files._is_protected_config(".Hive-Verify.json") is True
    assert files._is_protected_config(".HIVE-VERIFY.JSON") is True


def test_nested_file_with_the_same_name_is_not_protected():
    """Root-relative only -- the manifest's trust model is 'the one at the project
    root', same as .gitignore or tsconfig.json. A same-named file elsewhere is a
    different file and this rule has no opinion about it."""
    assert files._is_protected_config("subdir/.hive-verify.json") is False


# ── 4. normal files remain writable ─────────────────────────────────────────────────

def test_write_file_still_creates_an_ordinary_new_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = files.write_file("src/new_module.py", "x = 1\n")
    assert result.startswith("written:")
    assert (tmp_path / "src" / "new_module.py").read_text(encoding="utf-8") == "x = 1\n"


def test_apply_diff_still_edits_an_ordinary_file(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    f = root / "sample.py"
    f.write_text("x = 1\n", encoding="utf-8")
    result = files.apply_diff("sample.py", "x = 1", "x = 2")
    assert "blocked" not in result
    assert f.read_text(encoding="utf-8") == "x = 2\n"


def test_a_file_merely_containing_the_manifest_name_as_a_substring_is_unaffected(
        tmp_path, monkeypatch):
    """The check is an exact-name match, not a substring search -- a file legitimately
    named e.g. 'my.hive-verify.json.md' must not be caught by accident."""
    _setup(tmp_path, monkeypatch)
    result = files.write_file("notes-about.hive-verify.json.md", "notes")
    assert result.startswith("written:")


# ── 5. existing excluded paths retain their existing behaviour ─────────────────────

def test_existing_exclusion_behaviour_is_unchanged(tmp_path, monkeypatch):
    """node_modules etc. must still be blocked by is_excluded(), independent of this
    new check -- this proves the new guard was added BEFORE is_excluded(), not instead
    of it."""
    _setup(tmp_path, monkeypatch)
    result = files.write_file("node_modules/pkg/index.js", "module.exports = {}")
    assert "excluded path" in result
    assert not (tmp_path / "node_modules").exists()


def test_write_review_staging_still_stages_an_ordinary_edit(tmp_path, monkeypatch):
    """The manifest guard must not interfere with the ordinary WRITE_REVIEW path for
    everything else."""
    root = _setup(tmp_path, monkeypatch, write_review=True)
    f = root / "sample.py"
    f.write_text("x = 1\n", encoding="utf-8")
    result = files.apply_diff("sample.py", "x = 1", "x = 2")
    assert result.startswith("review_pending:")
    assert (root / "sample.py.hive_proposed").read_text(encoding="utf-8") == "x = 2\n"


# ── 6. rejection message identifies the manifest as protected verification config ──

def test_write_file_rejection_names_it_as_protected_verification_config(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = files.write_file(".hive-verify.json", "{}")
    assert "protected verification" in result
    assert ".hive-verify.json" in result


def test_apply_diff_rejection_names_it_as_protected_verification_config(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / ".hive-verify.json").write_text('{"a": 1}', encoding="utf-8")
    result = files.apply_diff(".hive-verify.json", '"a": 1', '"a": 2')
    assert "protected verification" in result
    assert ".hive-verify.json" in result


# ── the helper itself, directly ─────────────────────────────────────────────────────

def test_is_protected_config_true_for_exact_name():
    assert files._is_protected_config(".hive-verify.json") is True


def test_is_protected_config_false_for_unrelated_dotfiles():
    """.gitignore, .env.example etc. must be entirely unaffected -- this rule is one
    exact filename, not a general dotfile policy."""
    assert files._is_protected_config(".gitignore") is False
    assert files._is_protected_config(".env.example") is False
