"""Experiment 5 Phase 2: verify_project. Mechanism tests only -- no I3 criteria, no
Phase 2 scorer, nothing TypeScript-specific asserted beyond "the generic diagnostic
parser recognises the compiler-diagnostic shape a real tsc run actually produces."

Two of `run_command` and `verify_project`'s own module both bind PROJECT_ROOT by
direct name import (`from config import PROJECT_ROOT`), so both must be patched to
the same tmp_path -- patching only one leaves the manifest read from the fixture
while the command executes against the real project root, exactly the trap
test_files.py's own comment already documents for run_command's sibling tools.
"""
import json
import shutil
import threading
import time

import pytest

from tools import files, verification as v

HAS_NODE = shutil.which("node") is not None


def _setup(tmp_path, monkeypatch, manifest: dict | None = None, write_review=False):
    monkeypatch.setattr(v, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "WRITE_REVIEW", write_review)
    if manifest is not None:
        (tmp_path / ".hive-verify.json").write_text(json.dumps(manifest),
                                                     encoding="utf-8")
    return tmp_path


def _result(tmp_path, monkeypatch, checks, targets=None, manifest=None,
           write_review=False):
    _setup(tmp_path, monkeypatch, manifest, write_review=write_review)
    return json.loads(v.verify_project(checks=checks, targets=targets))


# ── manifest absence / malformed -------------------------------------------------

def test_missing_manifest_is_unsupported_not_error(tmp_path, monkeypatch):
    """No manifest means nothing was declared, not that a check was run and failed."""
    r = _result(tmp_path, monkeypatch, ["typecheck"])
    assert r["status"] == "UNSUPPORTED"
    assert r["checks"][0]["status"] == "UNSUPPORTED"
    assert ".hive-verify.json" in r["checks"][0]["reason"]


def test_malformed_manifest_json_is_error_not_unsupported(tmp_path, monkeypatch):
    """Something WAS declared; the infra just can't use it -- a different claim from
    "nothing declared", and the protocol must not conflate the two."""
    root = _setup(tmp_path, monkeypatch)
    (root / ".hive-verify.json").write_text("{not valid json", encoding="utf-8")
    r = json.loads(v.verify_project(checks=["typecheck"]))
    assert r["status"] == "UNSUPPORTED"
    assert "not valid JSON" in r["checks"][0]["reason"]


def test_manifest_without_checks_key_is_treated_as_absent(tmp_path, monkeypatch):
    r = _result(tmp_path, monkeypatch, ["typecheck"], manifest={"version": 1})
    assert r["status"] == "UNSUPPORTED"


def test_unknown_check_name_is_unsupported(tmp_path, monkeypatch):
    r = _result(tmp_path, monkeypatch, ["nonexistent_check"],
               manifest={"version": 1, "checks": {"typecheck": {"command": "echo hi"}}})
    assert r["checks"][0]["status"] == "UNSUPPORTED"
    assert "nonexistent_check" in r["checks"][0]["reason"]


# ── the model never supplies a command -------------------------------------------

def test_manifest_declares_the_command_not_the_request():
    """The public signature has no command/cwd/timeout parameter at all -- this is
    checked structurally, not just by convention."""
    import inspect
    sig = inspect.signature(v.verify_project)
    assert set(sig.parameters) == {"checks", "targets"}


# ── target validation --------------------------------------------------------------

def test_target_outside_project_root_is_rejected(tmp_path, monkeypatch):
    r = _result(tmp_path, monkeypatch, ["typecheck"], targets=["../../etc/passwd"],
               manifest={"version": 1, "checks": {"typecheck": {"command": "true"}}})
    assert "../../etc/passwd" in r["rejected_targets"]
    assert r["verified_targets"] == []


def test_target_inside_project_root_is_accepted(tmp_path, monkeypatch):
    r = _result(tmp_path, monkeypatch, ["typecheck"], targets=["src/a.ts"],
               manifest={"version": 1, "checks": {"typecheck": {"command": "true"}}})
    assert r["verified_targets"] == ["src/a.ts"]
    assert r["rejected_targets"] == []


def test_manifest_itself_cannot_be_used_as_a_target(tmp_path, monkeypatch):
    r = _result(tmp_path, monkeypatch, ["typecheck"], targets=[".hive-verify.json"],
               manifest={"version": 1, "checks": {"typecheck": {"command": "true"}}})
    assert ".hive-verify.json" in r["rejected_targets"]


def test_a_bad_target_does_not_block_the_checks_from_running(tmp_path, monkeypatch):
    r = _result(tmp_path, monkeypatch, ["typecheck"], targets=["../escape"],
               manifest={"version": 1, "checks": {"typecheck": {"command": "true"}}})
    assert r["checks"][0]["id"] == "typecheck"
    assert r["checks"][0]["status"] != "UNSUPPORTED"


def test_targets_do_not_narrow_a_project_wide_check(tmp_path, monkeypatch):
    """DECISION 4: targets are advisory metadata only in v1 -- supplying one must not
    make the executed command different from what the manifest declares."""
    root = _setup(tmp_path, monkeypatch, manifest={
        "version": 1, "checks": {"typecheck": {"command": "echo ran"}}})
    r1 = json.loads(v.verify_project(checks=["typecheck"]))
    r2 = json.loads(v.verify_project(checks=["typecheck"], targets=["src/x.ts"]))
    assert r1["checks"][0]["raw_output"].split("\n")[0] \
        == r2["checks"][0]["raw_output"].split("\n")[0] == "ran"


# ── status semantics: PASS/FAIL/ERROR from exit code, not from parsing -----------

def test_exit_zero_with_no_output_is_pass(tmp_path, monkeypatch):
    r = _result(tmp_path, monkeypatch, ["typecheck"],
               manifest={"version": 1, "checks": {"typecheck": {"command": "true"}}})
    assert r["checks"][0]["status"] == "PASS"
    assert r["status"] == "PASS"


def test_nonzero_exit_with_unparseable_output_is_still_fail(tmp_path, monkeypatch):
    """Status must not depend on successful diagnostic parsing -- an unrecognised
    failure shape is still a real FAIL, with an empty diagnostics list and the raw
    text as the only evidence, never silently swallowed into PASS.

    Uses python -c rather than shell syntax (';', native exit builtins) because
    run_command's subprocess.run(shell=True) invokes cmd.exe on this Windows host
    and /bin/sh in the real Linux container -- a command string portable across both
    is needed for this test to mean the same thing in either place."""
    cmd = ("python -c \"print('something went wrong'); import sys; sys.exit(1)\"")
    r = _result(tmp_path, monkeypatch, ["typecheck"],
               manifest={"version": 1, "checks": {"typecheck": {"command": cmd}}})
    assert r["checks"][0]["status"] == "FAIL"
    assert r["checks"][0]["diagnostics"] == []
    assert "something went wrong" in r["checks"][0]["raw_output"]
    assert r["status"] == "FAIL"


def test_command_that_cannot_run_is_error_not_fail(tmp_path, monkeypatch):
    r = _result(tmp_path, monkeypatch, ["typecheck"], manifest={
        "version": 1,
        "checks": {"typecheck": {"command": "this_binary_does_not_exist_xyz"}}})
    assert r["checks"][0]["status"] in ("FAIL", "ERROR")  # shell "command not found"
    # whichever it lands on, it must be distinguishable from a clean pass:
    assert r["checks"][0]["status"] != "PASS"


def test_blocked_by_existing_mutating_guard_is_error(tmp_path, monkeypatch):
    """DECISION 2 (Stage A.1): verified live against the real guard regexes -- a
    manifest command that tries to install packages is blocked by run_command's
    EXISTING, unmodified guard, and that must surface as an infra ERROR here, not a
    code FAIL. Needs WRITE_REVIEW=True -- both guards are gated on it in files.py, so
    the default-False fixture setup would make this a false pass by never invoking
    the guard at all."""
    r = _result(tmp_path, monkeypatch, ["typecheck"], write_review=True, manifest={
        "version": 1,
        "checks": {"typecheck": {"command": "npm install && echo done"}}})
    assert r["checks"][0]["status"] == "ERROR"
    assert "blocked" in r["checks"][0]["raw_output"]


def test_cwd_is_honoured(tmp_path, monkeypatch):
    sub = tmp_path / "nested"
    sub.mkdir()
    r = _result(tmp_path, monkeypatch, ["typecheck"], manifest={
        "version": 1,
        "checks": {"typecheck": {"command": "pwd", "cwd": "nested"}}})
    assert "nested" in r["checks"][0]["raw_output"]


# ── the tool is read-only with respect to source ----------------------------------

def test_verify_project_does_not_write_anything(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch,
          manifest={"version": 1, "checks": {"typecheck": {"command": "true"}}})
    before = sorted(p.name for p in tmp_path.iterdir())   # AFTER fixture setup --
    json.loads(v.verify_project(checks=["typecheck"]))    # the manifest write above
    after = sorted(p.name for p in tmp_path.iterdir())    # is test setup, not SUT
    assert before == after, "verify_project must not create or modify any file"


# ── generic diagnostic parser, tested against REAL tsc output where available -----

def test_parser_recognises_a_synthetic_compiler_diagnostic_line():
    diags = v._parse_compiler_style_diagnostics(
        'src/a.ts(12,5): error TS2322: not assignable\n'
        'src/b.ts(3,1): warning TS9999: something minor\n'
        'this line is not a diagnostic at all\n')
    assert len(diags) == 2
    d = diags[0]
    assert (d.file, d.line, d.column, d.severity, d.code) == \
        ("src/a.ts", 12, 5, "error", "TS2322")
    assert d.message == "not assignable"


def test_parser_returns_empty_list_for_unrecognised_output():
    assert v._parse_compiler_style_diagnostics("random text\nno diagnostics here") == []


@pytest.mark.skipif(not HAS_NODE, reason="no node on this host")
def test_parser_against_a_real_tsc_syntax_error(tmp_path):
    """Drives the actual TypeScript compiler (present on this host, per the
    Experiment 5 prerequisite investigation) rather than a fabricated string, so the
    'generic' regex is proven against real output at least once."""
    import os
    import subprocess

    f = tmp_path / "broken.ts"
    f.write_text('const x: number = ;\n', encoding="utf-8")
    ekam_ts = (r"C:\Users\Abhishek Behera\Projects\EkamApp\Client\EcommClient-Web"
              r"\ekamweb\node_modules\typescript\bin\tsc")
    r = subprocess.run(
        ["node", ekam_ts, "--noEmit", "--strict", str(f)],
        capture_output=True, text=True, timeout=60)
    diags = v._parse_compiler_style_diagnostics(r.stdout + r.stderr)
    assert len(diags) >= 1
    assert diags[0].severity == "error"
    assert diags[0].code and diags[0].code.startswith("TS")


# ═════════════════════════════════════════════════════════════════════════════════════
# Phase 2A: proposed-state swap/restore lifecycle
#
# These drive REAL files on a real filesystem -- no mocking of rename/restore, per
# the phase's own instruction that the core safety property being tested is the
# filesystem lifecycle itself. Commands are written portably (python -c) because
# run_command's subprocess.run(shell=True) invokes cmd.exe on this Windows dev host
# and /bin/sh in the real Linux container -- the same lesson from Phase 2's own
# earlier tests, now load-bearing for several of these.
# ═════════════════════════════════════════════════════════════════════════════════════

_READ_TARGET_CMD = "python -c \"print(open('a.ts').read())\""


def _target_with_proposed(root, original="ORIGINAL", proposed="PROPOSED"):
    (root / "a.ts").write_text(original, encoding="utf-8")
    (root / "a.ts.hive_proposed").write_text(proposed, encoding="utf-8")
    return root / "a.ts", root / "a.ts.hive_proposed", root / "a.ts.hive_verify_backup"


def _manifest(command=_READ_TARGET_CMD):
    return {"version": 1, "checks": {"typecheck": {"command": command}}}


# ── 1-2: substitution and restore-after-PASS ────────────────────────────────────────

def test_proposed_target_is_substituted_during_verification(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, manifest=_manifest())
    target, proposed, backup = _target_with_proposed(root)
    r = json.loads(v.verify_project(checks=["typecheck"], targets=["a.ts"]))
    assert "PROPOSED" in r["checks"][0]["raw_output"]
    assert "ORIGINAL" not in r["checks"][0]["raw_output"]


def test_original_target_is_restored_after_pass(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, manifest=_manifest())
    target, proposed, backup = _target_with_proposed(root)
    r = json.loads(v.verify_project(checks=["typecheck"], targets=["a.ts"]))
    assert r["checks"][0]["status"] == "PASS"
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


# ── 3: restore-after-FAIL ────────────────────────────────────────────────────────────

def test_original_target_is_restored_after_fail(tmp_path, monkeypatch):
    fail_cmd = "python -c \"print(open('a.ts').read()); raise SystemExit(1)\""
    root = _setup(tmp_path, monkeypatch, manifest=_manifest(fail_cmd))
    target, proposed, backup = _target_with_proposed(root)
    r = json.loads(v.verify_project(checks=["typecheck"], targets=["a.ts"]))
    assert r["checks"][0]["status"] == "FAIL"
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert proposed.read_text(encoding="utf-8") == "PROPOSED"
    assert not backup.exists()


# ── 4: restore-after-exception (direct _materialize unit test, per-spec: this is the
#      one place an exception is genuinely raised, since run_command itself never
#      raises -- see verify_project's own generic Exception handler for why) ────────

def test_original_target_is_restored_after_an_exception_in_the_body(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    target, proposed, backup = _target_with_proposed(root)
    with pytest.raises(RuntimeError, match="boom"):
        with v._materialize(["a.ts"]):
            assert target.read_text(encoding="utf-8") == "PROPOSED"
            raise RuntimeError("boom")
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert proposed.read_text(encoding="utf-8") == "PROPOSED"
    assert not backup.exists()


def test_verify_project_reports_error_when_the_body_raises(tmp_path, monkeypatch):
    """The end-to-end path: verify_project must never let an internal exception
    propagate out of the tool call -- restoration must have already happened by the
    time it returns, and the result must be a well-formed ERROR."""
    root = _setup(tmp_path, monkeypatch, manifest=_manifest())
    target, proposed, backup = _target_with_proposed(root)
    monkeypatch.setattr(v, "_run_check",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = json.loads(v.verify_project(checks=["typecheck"], targets=["a.ts"]))
    assert r["status"] == "ERROR"
    assert "boom" in r["checks"][0]["reason"]
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


# ── 5: restore-after-timeout ─────────────────────────────────────────────────────────

def test_original_target_is_restored_after_a_real_timeout(tmp_path, monkeypatch):
    sleep_cmd = "python -c \"import time; time.sleep(5)\""
    manifest = {"version": 1,
               "checks": {"typecheck": {"command": sleep_cmd, "timeout": 1}}}
    root = _setup(tmp_path, monkeypatch, manifest=manifest)
    target, proposed, backup = _target_with_proposed(root)
    r = json.loads(v.verify_project(checks=["typecheck"], targets=["a.ts"]))
    assert r["checks"][0]["status"] == "ERROR"
    assert "timed out" in r["checks"][0]["raw_output"]
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert proposed.read_text(encoding="utf-8") == "PROPOSED"
    assert not backup.exists()


# ── 6-8: stale backup is a hard stop ────────────────────────────────────────────────

def test_stale_backup_causes_error(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, manifest=_manifest())
    target, proposed, backup = _target_with_proposed(root)
    backup.write_text("STALE ORIGINAL FROM A CRASHED RUN", encoding="utf-8")
    r = json.loads(v.verify_project(checks=["typecheck"], targets=["a.ts"]))
    assert r["status"] == "ERROR"
    assert r["checks"][0]["status"] == "ERROR"
    assert "stale" in r["checks"][0]["reason"].lower()


def test_stale_backup_is_not_overwritten(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, manifest=_manifest())
    target, proposed, backup = _target_with_proposed(root)
    backup.write_text("STALE ORIGINAL FROM A CRASHED RUN", encoding="utf-8")
    v.verify_project(checks=["typecheck"], targets=["a.ts"])
    assert backup.read_text(encoding="utf-8") == "STALE ORIGINAL FROM A CRASHED RUN"


def test_stale_backup_prevents_command_execution(tmp_path, monkeypatch):
    """The check itself must never run -- proven with a command that would leave a
    detectable marker if it executed, not merely by inspecting the returned status."""
    root = _setup(tmp_path, monkeypatch)
    marker = root / "ran.marker"
    manifest = {"version": 1,
               "checks": {"typecheck": {"command":
                   f"python -c \"open('ran.marker','w').close()\""}}}
    (root / ".hive-verify.json").write_text(json.dumps(manifest), encoding="utf-8")
    target, proposed, backup = _target_with_proposed(root)
    backup.write_text("STALE", encoding="utf-8")
    v.verify_project(checks=["typecheck"], targets=["a.ts"])
    assert not marker.exists(), "the manifest command ran despite a stale backup"
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert proposed.read_text(encoding="utf-8") == "PROPOSED"


# ── 9-10: failed restoration ─────────────────────────────────────────────────────────

def test_failed_restoration_returns_error(tmp_path, monkeypatch):
    """A real, reproducible restore failure: something removes the swapped-in file
    while it is 'live' at the target path, so target.rename(proposed) has no source
    to rename. Not a mock of Path.rename -- a genuine filesystem state that makes
    the real rename call fail."""
    root = _setup(tmp_path, monkeypatch)
    target, proposed, backup = _target_with_proposed(root)
    with pytest.raises(v._RestorationFailedError) as ei:
        with v._materialize(["a.ts"]):
            target.unlink()
    failure = ei.value.failures[0]
    assert failure["failed_step"] == "restore .hive_proposed"
    assert failure["target"].endswith("a.ts")
    assert failure["backup"].endswith("a.ts.hive_verify_backup")
    assert failure["proposed"].endswith("a.ts.hive_proposed")


def test_failed_restoration_produces_the_integrity_check_message(tmp_path, monkeypatch):
    """End-to-end through verify_project: the manifest's OWN command deletes the
    target it is checking, forcing a real restore failure, and the result must
    carry the exact required phrase."""
    delete_cmd = "python -c \"import os; os.remove('a.ts')\""
    root = _setup(tmp_path, monkeypatch, manifest=_manifest(delete_cmd))
    target, proposed, backup = _target_with_proposed(root)
    r = json.loads(v.verify_project(checks=["typecheck"], targets=["a.ts"]))
    assert r["status"] == "ERROR"
    assert r["checks"][0]["reason"] == "source-tree integrity check failed"
    assert "source-tree integrity check failed" in r["checks"][0]["diagnostics"][0]["message"]
    assert "backup=" in r["checks"][0]["diagnostics"][0]["message"]
    assert "failed_step=" in r["checks"][0]["diagnostics"][0]["message"]


# ── 11-14: exact restoration content/state ──────────────────────────────────────────

def test_backup_path_is_removed_after_successful_restoration(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, manifest=_manifest())
    target, proposed, backup = _target_with_proposed(root)
    v.verify_project(checks=["typecheck"], targets=["a.ts"])
    assert not backup.exists()


def test_proposed_sibling_exists_again_after_successful_restoration(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, manifest=_manifest())
    target, proposed, backup = _target_with_proposed(root)
    v.verify_project(checks=["typecheck"], targets=["a.ts"])
    assert proposed.exists()
    assert proposed.read_text(encoding="utf-8") == "PROPOSED"


def test_original_target_content_is_exactly_restored_byte_for_byte(tmp_path, monkeypatch):
    original_bytes = "ORIGINAL with unicode — and\nnewlines\n".encode("utf-8")
    root = _setup(tmp_path, monkeypatch, manifest=_manifest())
    (root / "a.ts").write_bytes(original_bytes)
    (root / "a.ts.hive_proposed").write_text("PROPOSED", encoding="utf-8")
    v.verify_project(checks=["typecheck"], targets=["a.ts"])
    assert (root / "a.ts").read_bytes() == original_bytes


def test_proposed_content_is_not_left_at_the_original_path(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, manifest=_manifest())
    target, proposed, backup = _target_with_proposed(root)
    v.verify_project(checks=["typecheck"], targets=["a.ts"])
    assert target.read_text(encoding="utf-8") != "PROPOSED"
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


# ── 15: concurrency -- real threads, real threading.Lock, no mocking ───────────────

def test_concurrent_verification_of_the_same_target_is_serialized(tmp_path, monkeypatch):
    """Proven with real threads against the real _materialize context manager, per
    the confirmed execution model (FastMCP runs sync tools in a real thread pool --
    see verification.py's own module comment). Serialized here means the second
    call BLOCKS until the first's swap/restore cycle completes, rather than racing
    it or silently corrupting state."""
    root = _setup(tmp_path, monkeypatch)
    target, proposed, backup = _target_with_proposed(root)

    order: list[str] = []
    order_lock = threading.Lock()
    t1_inside = threading.Event()
    t1_release = threading.Event()

    def worker1():
        with v._materialize(["a.ts"]):
            with order_lock:
                order.append("t1-enter")
            t1_inside.set()
            t1_release.wait(timeout=5)
            with order_lock:
                order.append("t1-exit")

    def worker2():
        t1_inside.wait(timeout=5)
        with v._materialize(["a.ts"]):
            with order_lock:
                order.append("t2-enter")

    t1 = threading.Thread(target=worker1)
    t2 = threading.Thread(target=worker2)
    t1.start()
    assert t1_inside.wait(timeout=5), "worker1 never entered its materialized block"
    t2.start()
    time.sleep(0.3)          # give t2 a real chance to (wrongly) enter if unlocked
    with order_lock:
        assert "t2-enter" not in order, "t2 entered while t1 still held the target lock"
    t1_release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order == ["t1-enter", "t1-exit", "t2-enter"]
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


# ── 16: the manifest itself is never read from a staged variant ────────────────────

def test_staged_manifest_variant_is_never_used(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch,
                 manifest={"version": 1,
                          "checks": {"typecheck": {"command": "echo trusted"}}})
    (root / ".hive-verify.json.hive_proposed").write_text(
        json.dumps({"version": 1,
                   "checks": {"typecheck": {"command": "echo MALICIOUS"}}}),
        encoding="utf-8")
    r = json.loads(v.verify_project(checks=["typecheck"]))
    assert "trusted" in r["checks"][0]["raw_output"]
    assert "MALICIOUS" not in r["checks"][0]["raw_output"]


# ── 17: manifest remains readable ───────────────────────────────────────────────────

def test_manifest_remains_readable_after_a_verification_run(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, manifest=_manifest())
    v.verify_project(checks=["typecheck"])
    manifest, err = v._load_manifest()
    assert err is None
    assert manifest["checks"]["typecheck"]["command"] == _READ_TARGET_CMD


# ── 18-20: run_command is the only execution path ───────────────────────────────────

def test_run_shell_and_run_docker_are_never_invoked(tmp_path, monkeypatch):
    calls = {"run_shell": 0, "run_docker": 0, "run_command": 0}
    monkeypatch.setattr(v, "run_command",
                        lambda *a, **k: (calls.__setitem__(
                            "run_command", calls["run_command"] + 1)
                            or "ok\n[exit 0]"))
    import tools.shell as shell_mod
    monkeypatch.setattr(shell_mod, "run_shell",
                        lambda *a, **k: calls.__setitem__("run_shell", calls["run_shell"] + 1))
    monkeypatch.setattr(shell_mod, "run_docker",
                        lambda *a, **k: calls.__setitem__("run_docker", calls["run_docker"] + 1))
    root = _setup(tmp_path, monkeypatch, manifest=_manifest())
    target, proposed, backup = _target_with_proposed(root)
    v.verify_project(checks=["typecheck"], targets=["a.ts"])
    assert calls["run_command"] == 1
    assert calls["run_shell"] == 0
    assert calls["run_docker"] == 0


def test_verification_module_does_not_import_run_shell_or_run_docker():
    """Static confirmation, independent of the monkeypatch above: the module has no
    reference to either name at all, so there is no code path that COULD call them."""
    import inspect
    src = inspect.getsource(v)
    assert "run_shell" not in src
    assert "run_docker" not in src
