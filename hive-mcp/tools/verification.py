"""Experiment 5 Phase 2 -- project-side mechanical verification.

    AGNOHive
       |  MCP: verify_project(checks=[...], targets=[...])
       v
    hive-mcp
       |  trusted .hive-verify.json
       v
    named check -> manifest command
       |
       v
    existing run_command
       |
       v
    VerificationResult

The model supplies a CHECK NAME, never a command. The command that actually runs
comes only from .hive-verify.json -- trusted, committed, project-owned configuration,
protected from model-directed writes by Phase 1's _is_protected_config(). This file
does not reopen that trust boundary: it reads the manifest with plain pathlib, the
same way Phase 1's docstring said it would, never through get_file_content, and never
by looking for a `.hive-verify.json.hive_proposed` staged variant -- no code path here
ever constructs that filename, because nothing should ever need to.

DELIBERATELY LANGUAGE-AGNOSTIC. Nothing in VerificationRequest/Result/CheckResult/
Diagnostic below names TypeScript, RTK Query, tagTypes, providesTags, or any of
I3.1-I3.7. The one place any language-specific knowledge exists is
_parse_compiler_style_diagnostics(), and even that is a generic best-effort pattern
(`file(line,col): severity code: message`) that tsc happens to emit, not a parser
that knows what TS2322 means. If it does not recognise a check's output it returns no
diagnostics and the caller still gets the raw text and a real status -- status is
never gated on successful parsing (see _run_check).

Reused, not duplicated: command execution goes through the SAME run_command() in
tools/files.py, unmodified -- this module supplies it a fully pre-resolved,
manifest-derived command string (the only thing this module ever hands it) and
nothing else. The Phase 2 verifier does not talk to the Phase 2 scorer at all: no
import of anything under scratchpad/, no I3 criterion names anywhere in this file.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import PROJECT_ROOT

from .files import run_command, _is_protected_config, _proposed_path, _PROPOSED_SUFFIX

_MANIFEST_NAME = ".hive-verify.json"
_DEFAULT_TIMEOUT = 120

# ── result shapes ────────────────────────────────────────────────────────────────────

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_ERROR = "ERROR"
STATUS_UNSUPPORTED = "UNSUPPORTED"


@dataclass
class Diagnostic:
    file: str
    severity: str          # "error" | "warning"
    message: str
    line: int | None = None
    column: int | None = None
    code: str | None = None


@dataclass
class CheckResult:
    id: str
    status: str             # PASS | FAIL | ERROR | UNSUPPORTED
    exit_code: int | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    raw_output: str = ""
    reason: str = ""         # populated for ERROR/UNSUPPORTED -- why, not a diagnostic


@dataclass
class VerificationResult:
    status: str
    checks: list[CheckResult] = field(default_factory=list)
    verified_targets: list[str] = field(default_factory=list)      # audit metadata only
    rejected_targets: list[str] = field(default_factory=list)      # failed containment


def _to_json(result: VerificationResult) -> str:
    return json.dumps(asdict(result), indent=1)


# ── manifest ─────────────────────────────────────────────────────────────────────────

def _manifest_path() -> Path:
    return PROJECT_ROOT / _MANIFEST_NAME


def _load_manifest() -> tuple[dict | None, str | None]:
    """(manifest, error). manifest is None when absent (UNSUPPORTED, not ERROR --
    "nothing declared" is not the same claim as "verification found a defect", the
    same rule Phase 2's design settled on for a missing manifest generally) or when
    it fails to parse (ERROR -- something WAS declared and the infra can't use it).

    Read via plain pathlib, deliberately never through get_file_content -- this is
    the one thing every stage of this design insisted on, so it's asserted here by
    construction rather than by convention: there is no MCP session object anywhere
    in this module.
    """
    p = _manifest_path()
    if not p.exists():
        return None, "no .hive-verify.json at the project root"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"manifest is not valid JSON: {exc}"
    if not isinstance(data, dict) or "checks" not in data:
        return None, "manifest is missing a top-level 'checks' object"
    return data, None


# ── target validation (advisory metadata only -- see module docstring / DECISION 4) ──

def _validate_targets(targets: list[str] | None) -> tuple[list[str], list[str]]:
    """(accepted, rejected). A target must resolve under PROJECT_ROOT and must not be
    the protected manifest itself -- reused from Phase 1's own check rather than a
    second copy of what "protected" means. Rejection is not fatal to the request: a
    bad target is recorded and dropped, the checks still run.
    """
    if not targets:
        return [], []
    root = PROJECT_ROOT.resolve()
    accepted, rejected = [], []
    for t in targets:
        if not isinstance(t, str) or not t.strip():
            rejected.append(str(t))
            continue
        if _is_protected_config(t):
            rejected.append(t)
            continue
        try:
            resolved = (PROJECT_ROOT / t).resolve()
            resolved.relative_to(root)
        except (ValueError, OSError):
            rejected.append(t)
            continue
        accepted.append(t)
    return accepted, rejected


# ── proposed-state materialization (Experiment 5 Phase 2A) ──────────────────────────
#
# Rename-only swap: <target> -> <target>.hive_verify_backup, then
# <target>.hive_proposed -> <target>. Both are same-filesystem renames -- no file
# content is ever read or copied by this mechanism, so "the original bytes are
# restored exactly" is true by CONSTRUCTION (a rename cannot alter the bytes it
# moves), not something this code has to verify by comparing content after the fact.
# The real project dependency tree is untouched because nothing here duplicates it --
# the check runs against the SAME directory, just briefly containing different bytes
# at one filename.
#
# Locking: run_command and verify_project are both plain `def` (not `async def`).
# Read directly from the installed fastmcp package (tools/function_tool.py's own
# Tool.run(), Windows dev host, 2026-09-11): a synchronous tool is dispatched via
# `call_sync_fn_in_threadpool` -- "Sync function: run in threadpool to avoid
# blocking", its own comment. That means concurrent calls execute on REAL OS
# threads from a thread pool, not as coroutines interleaved on one event loop, so an
# asyncio.Lock would be the wrong primitive here (it is bound to one event loop and
# is not safe to acquire/release across separate worker threads without extra
# plumbing this doesn't need). threading.Lock, keyed per resolved target path via
# the standard lock-striping pattern below, is the smallest correct mechanism for
# that execution model.
_BACKUP_SUFFIX = ".hive_verify_backup"

_target_locks_guard = threading.Lock()
_target_locks: dict[str, threading.Lock] = {}


def _lock_for(key: str) -> threading.Lock:
    with _target_locks_guard:
        lock = _target_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _target_locks[key] = lock
        return lock


def _backup_path(target: Path) -> Path:
    return target.with_name(target.name + _BACKUP_SUFFIX)


class _StaleBackupError(Exception):
    def __init__(self, target: str, backup: str):
        super().__init__(f"stale verification backup exists for {target!r}: {backup!r}")
        self.target, self.backup = target, backup


class _RestorationFailedError(Exception):
    """Carries forensic detail for every target whose restore step failed --
    'preserve enough information to identify target/backup/proposed/failed step'."""

    def __init__(self, failures: list[dict]):
        super().__init__(f"{len(failures)} target(s) failed to restore")
        self.failures = failures


@contextlib.contextmanager
def _materialize(targets: list[str]):
    """Swap in .hive_proposed content for every target that has one, run the body,
    restore unconditionally.

    Three passes, deliberately in this order:

    PASS 0 locks every REQUESTED target, before checking anything about its current
    file state. This ordering is load-bearing, not cosmetic: a target's
    `.hive_proposed` sibling is exactly the file another concurrent call's PASS 2
    consumes (renames away) while it holds the lock. Checking "does .hive_proposed
    exist" BEFORE acquiring the lock -- the first version of this function did this
    -- means a second concurrent call can observe "nothing to swap" for a target
    that a first call is actively swapping, skip locking it entirely, and run
    straight past the first call's still-held lock. Locking on the REQUESTED target
    set first, then re-checking file state only once every lock is held, is what
    actually serializes two calls against the same target; a live two-thread test
    (test_concurrent_verification_of_the_same_target_is_serialized) caught this
    exact ordering bug before this comment existed.

    PASS 1 (still before any rename) checks every locked target for a stale backup
    -- "do not start swapping if a known stale backup exists for ANOTHER target"
    means the whole multi-target check must be resolved before touching ANY file,
    not interleaved with the swap loop.

    PASS 2 does the actual renames only once every target in this call has cleared
    PASS 1. Pure renames; no content is read or written by this code.

    Locks are acquired in a FIXED (sorted-by-resolved-path) order regardless of the
    order `targets` were supplied in, so two concurrent multi-target calls that
    share some targets can never deadlock waiting on each other in reverse order.
    """
    root = PROJECT_ROOT.resolve()

    # PASS 0 -- resolve + lock every REQUESTED target first, before looking at
    # whether it currently has a .hive_proposed sibling at all.
    locked: list[tuple[str, Path]] = []          # (key, target), in acquisition order
    for rel in sorted(set(targets)):
        target = PROJECT_ROOT / rel
        try:
            key = str(target.resolve())
        except OSError:
            continue
        locked.append((key, target))

    held: list[threading.Lock] = []
    swapped: list[tuple[Path, Path, Path]] = []   # (target, proposed, backup) actually swapped
    try:
        for _key, target in locked:
            lock = _lock_for(_key)
            lock.acquire()
            held.append(lock)

        # Now that every requested target's lock is held, it is safe to look at
        # file state -- a concurrent call already holding one of these locks has
        # finished its own restore by the time we get past acquire() above.
        swappable: list[tuple[Path, Path, Path]] = []   # (target, proposed, backup)
        for _key, target in locked:
            proposed = _proposed_path(target)
            if proposed.exists():
                swappable.append((target, proposed, _backup_path(target)))

        # PASS 1 -- stale-backup check across every target that WILL be swapped,
        # before any rename.
        for target, _proposed, backup in swappable:
            if backup.exists():
                raise _StaleBackupError(str(target.relative_to(root)), str(backup))

        # PASS 2 -- swap.
        for target, proposed, backup in swappable:
            target.rename(backup)
            proposed.rename(target)
            swapped.append((target, proposed, backup))

        yield
    finally:
        failures = []
        # Restore in REVERSE of swap order -- last swapped, first restored.
        for target, proposed, backup in reversed(swapped):
            step = "restore .hive_proposed"
            try:
                target.rename(proposed)
                step = "restore original"
                backup.rename(target)
                step = "post-restore existence check"
                if not target.exists() or backup.exists():
                    raise OSError(f"target missing or backup still present after {step}")
            except Exception as exc:  # noqa: BLE001
                failures.append({"target": str(target), "backup": str(backup),
                                 "proposed": str(proposed), "failed_step": step,
                                 "error": f"{type(exc).__name__}: {exc}"})
        for lock in reversed(held):
            lock.release()
        if failures:
            raise _RestorationFailedError(failures)


def _run_check(check_id: str, cfg: dict) -> CheckResult:
    command = cfg.get("command")
    if not isinstance(command, str) or not command.strip():
        return CheckResult(id=check_id, status=STATUS_UNSUPPORTED,
                           reason=f"check {check_id!r} has no 'command' in the manifest")
    timeout = cfg.get("timeout", _DEFAULT_TIMEOUT)
    cwd = cfg.get("cwd")
    # cwd/command both come from the TRUSTED manifest, never from the request -- this
    # is the one place a string is built rather than passed through verbatim, and
    # nothing in it is model-influenced. run_command() itself is called completely
    # unmodified; its own write/mutating guards (files.py's _WRITE_CMD_RE /
    # _MUTATING_CMD_RE) still apply to whatever the manifest declares.
    full_command = command if not cwd else f"cd {_shell_quote(cwd)} && {command}"
    raw = run_command(full_command, timeout=timeout)

    exit_match = re.search(r"\[exit (-?\d+)\]\s*$", raw)
    exit_code = int(exit_match.group(1)) if exit_match else None
    if raw.startswith("blocked:"):
        return CheckResult(id=check_id, status=STATUS_ERROR, raw_output=raw,
                           reason="the manifest's command was blocked by run_command's "
                                  "own write/mutating guard")
    if raw.startswith("run_command timed out"):
        return CheckResult(id=check_id, status=STATUS_ERROR, raw_output=raw,
                           reason=f"check timed out after {timeout}s")
    if raw.startswith("run_command failed:"):
        return CheckResult(id=check_id, status=STATUS_ERROR, raw_output=raw,
                           reason="the verification command could not be executed")

    diagnostics = _parse_compiler_style_diagnostics(raw)
    # Status is decided from the exit code, NEVER from whether parsing succeeded --
    # a check that fails with output this parser doesn't recognise is still FAIL,
    # just with an empty diagnostics list and the raw text as the only evidence.
    if exit_code == 0:
        status = STATUS_PASS
    elif exit_code is None:
        status = STATUS_ERROR
    else:
        status = STATUS_FAIL
    return CheckResult(id=check_id, status=status, exit_code=exit_code,
                       diagnostics=diagnostics, raw_output=raw)


def _shell_quote(s: str) -> str:
    if re.fullmatch(r"[\w./-]+", s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


# ── generic diagnostic parsing (best-effort; not TypeScript-specific) ───────────────

# `path/to/file.ext(12,34): error TSxxxx: message` -- tsc's own format, but the shape
# itself (file(line,col): severity[ code]: message) is a common compiler-diagnostic
# convention, not something built to recognise TypeScript specifically. A tool this
# doesn't match simply yields no diagnostics; see _run_check for what happens then.
_DIAG_RE = re.compile(
    r"^(?P<file>[^\s(][^(]*?)\((?P<line>\d+),(?P<col>\d+)\):\s*"
    r"(?P<severity>error|warning)\s*(?P<code>[A-Z]+\d+)?:?\s*(?P<message>.+)$"
)


def _parse_compiler_style_diagnostics(raw: str) -> list[Diagnostic]:
    out = []
    for line in raw.splitlines():
        m = _DIAG_RE.match(line.strip())
        if not m:
            continue
        out.append(Diagnostic(
            file=m.group("file").strip(),
            severity=m.group("severity"),
            message=m.group("message").strip(),
            line=int(m.group("line")),
            column=int(m.group("col")),
            code=m.group("code"),
        ))
    return out


# ── the MCP-exposed tool ─────────────────────────────────────────────────────────────

def verify_project(checks: list[str], targets: list[str] | None = None) -> str:
    """
    Run one or more project-declared verification checks and return a structured
    result. Read-only with respect to source: this only RUNS what .hive-verify.json
    already declares, via the same read-only-guarded run_command() every other
    command tool uses.

    `checks` are NAMES only -- "typecheck", not a shell command. What actually runs
    is whatever the project's own committed .hive-verify.json declares for that name;
    this tool has no way to accept an arbitrary command string.

    `targets` is OPTIONAL, ADVISORY metadata describing which files changed. It does
    NOT narrow which command a check executes -- a project-wide typecheck stays
    project-wide no matter what targets are listed. What it DOES do (Phase 2A): for
    any target that has a `.hive_proposed` sibling staged by apply_diff/write_file,
    that proposed content is swapped in at the target's real path for the duration
    of the check, then restored -- unconditionally, on PASS, FAIL, exception, or
    timeout -- so the check sees the Coder's proposed change rather than stale
    on-disk content. A target outside the project root, or the protected
    .hive-verify.json manifest itself, is rejected and reported in
    `rejected_targets`; it does not fail the whole request. A target with no
    `.hive_proposed` sibling is simply left untouched -- it is not this mechanism's
    concern.

    If a target already has a leftover `<target>.hive_verify_backup` file, that is
    treated as evidence a PRIOR verification crashed mid-swap: the whole call
    returns ERROR immediately, without running any check and without touching any
    file. If restoring the original file back into place fails for any reason, that
    is also ERROR, with a diagnostic literally containing "source-tree integrity
    check failed" plus the target/backup/proposed paths and which restore step
    failed -- never reported as an ordinary PASS or FAIL.

    Args:
        checks: named verification capabilities to run, e.g. ["typecheck"]
        targets: optional list of project-relative paths. Any with a staged
                 .hive_proposed sibling are materialized for the duration of the
                 checks below; see above.
    """
    accepted_targets, rejected_targets = _validate_targets(targets)

    manifest, manifest_err = _load_manifest()
    if manifest is None:
        result = VerificationResult(
            status=STATUS_UNSUPPORTED,
            checks=[CheckResult(id=c, status=STATUS_UNSUPPORTED, reason=manifest_err)
                   for c in checks],
            verified_targets=accepted_targets, rejected_targets=rejected_targets)
        return _to_json(result)

    declared = manifest.get("checks", {})

    def _run_all() -> list[CheckResult]:
        results: list[CheckResult] = []
        for check_id in checks:
            cfg = declared.get(check_id)
            if cfg is None:
                results.append(CheckResult(
                    id=check_id, status=STATUS_UNSUPPORTED,
                    reason=f"{check_id!r} is not declared in {_MANIFEST_NAME}"))
                continue
            results.append(_run_check(check_id, cfg))
        return results

    try:
        with _materialize(accepted_targets):
            results = _run_all()
    except _StaleBackupError as exc:
        result = VerificationResult(
            status=STATUS_ERROR,
            checks=[CheckResult(
                id=c, status=STATUS_ERROR,
                reason=f"stale verification backup for {exc.target!r} "
                       f"({exc.backup}) -- a previous verification may have "
                       f"crashed; not overwritten, no check was run")
                   for c in checks],
            verified_targets=accepted_targets, rejected_targets=rejected_targets)
        return _to_json(result)
    except _RestorationFailedError as exc:
        detail = "; ".join(
            f"target={f['target']} backup={f['backup']} proposed={f['proposed']} "
            f"failed_step={f['failed_step']} error={f['error']}"
            for f in exc.failures)
        result = VerificationResult(
            status=STATUS_ERROR,
            checks=[CheckResult(
                id=c, status=STATUS_ERROR,
                diagnostics=[Diagnostic(
                    file=exc.failures[0]["target"], severity="error",
                    message=f"source-tree integrity check failed: {detail}")],
                reason="source-tree integrity check failed")
                   for c in checks],
            verified_targets=accepted_targets, rejected_targets=rejected_targets)
        return _to_json(result)
    except Exception as exc:  # noqa: BLE001
        # _run_check() itself never raises (it wraps run_command(), which already
        # catches TimeoutExpired/Exception internally and always returns a string) --
        # this exists so that IF something inside the materialized block raises
        # unexpectedly, restoration (the context manager's own finally) has already
        # run by the time we get here, and the result is still a well-formed ERROR
        # rather than an exception propagating out of the tool call. Per spec: a
        # verifier-internal exception with successful restoration is ERROR, not a
        # crash and not a silent PASS/FAIL.
        result = VerificationResult(
            status=STATUS_ERROR,
            checks=[CheckResult(id=c, status=STATUS_ERROR,
                                reason=f"verification aborted: "
                                       f"{type(exc).__name__}: {exc}")
                   for c in checks],
            verified_targets=accepted_targets, rejected_targets=rejected_targets)
        return _to_json(result)

    # Priority, most severe first. A mix of PASS and UNSUPPORTED (some requested
    # checks exist and passed, one was unknown) is reported as PASS -- something WAS
    # verified and found clean; the unsupported one is still visible per-check in
    # `checks`, just not enough on its own to override a real, clean result.
    if any(r.status == STATUS_ERROR for r in results):
        overall = STATUS_ERROR
    elif any(r.status == STATUS_FAIL for r in results):
        overall = STATUS_FAIL
    elif all(r.status == STATUS_UNSUPPORTED for r in results):
        overall = STATUS_UNSUPPORTED
    else:
        overall = STATUS_PASS

    result = VerificationResult(status=overall, checks=results,
                                verified_targets=accepted_targets,
                                rejected_targets=rejected_targets)
    return _to_json(result)
