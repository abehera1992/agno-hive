"""File write tools with WRITE_REVIEW support.

When WRITE_REVIEW=true every write is staged as a .hive_proposed file.
The hive CLI detects it, shows the diff, and applies/discards locally.
confirm_write and reject_write are NOT exposed — the CLI handles them.
"""
import difflib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import PROJECT_ROOT, WRITE_REVIEW

from .exclusions import is_excluded

_PROPOSED_SUFFIX = ".hive_proposed"
_IN_DOCKER = Path("/.dockerenv").exists()

# Detects a stuck retry loop: the exact same failing apply_diff call repeated
# verbatim against the same file. Module-level and intentionally coarse — hive-mcp
# runs as one long-lived process, and this only needs to catch "the identical call,
# again," not build a general call history. Measured 2026-08-01: a single task
# burned 30-40+ tool calls retrying against a file that kept returning the same
# generic "old_string not found" message with no information about why.
_last_failed_call: dict[str, tuple[str, str]] = {}

# Catches the same failure spiral even when EVERY call is a DISTINCT old_string,
# which the exact-repeat check above cannot see. Measured live 2026-08-05: a Coder
# mistook get_file_content's "LINENUM<TAB>content" citation-numbering for real file
# content and passed old_string values like '129\n}', '133\n}', '134\n}' -- a
# different string on every single call, so _last_failed_call never once matched --
# and ran past 90 consecutive failed apply_diff calls against the same file with
# zero progress before a human intervened. However the failures vary, N in a row
# against the same file means continuing to guess will not fix it.
_MAX_CONSECUTIVE_FAILURES = 5
_consecutive_failures: dict[str, int] = {}

# A leading bare integer immediately followed by either a newline OR whitespace-then-
# content on the SAME line is the strongest available signal that old_string was built
# by copying a "LINENUM<TAB>content" read result wholesale instead of stripping the
# line-number prefix get_file_content's own docstring already warns against. Measured
# live 2026-08-05: the original newline-only form of this regex caught 'old_string=
# "192\n\n193\t.empty {..."' but missed 'old_string="495  }"' in the very same run --
# same root-cause mistake, just rendered with the number and content on one line
# (the tab collapsed to spaces) instead of two. Both shapes get the same hint now.
_BARE_LINE_NUMBER_RE = re.compile(r"^\s*\d+(?:\s*\n|[ \t]+\S)")

# A top-level class selector opening a rule, e.g. ".statusBadge {" or
# ".statusBadge--warning {". Deliberately narrow -- line-anchored, no nested "&"
# selectors, no media queries -- this targets exactly one failure mode (see
# apply_diff's NAME-collision check below), not a general CSS parser.
_SCSS_SELECTOR_RE = re.compile(r"^\s*(\.[A-Za-z_][\w.\-]*)\s*\{", re.MULTILINE)


def _near_match_hint(content: str, old_string: str, context: int = 2) -> str:
    """Best-effort explanation of why old_string didn't match: the closest existing
    line in the file, so the model can see the actual mismatch (usually whitespace,
    quoting, or a line already changed by an earlier edit) instead of guessing via
    repeated re-reads. Returns "" when nothing is close enough to be useful — a
    weak hint that misleads is worse than no hint.
    """
    target = (old_string.splitlines() or [old_string])[0].strip()
    if not target:
        return ""
    file_lines = content.splitlines()
    best_idx, best_ratio = None, 0.0
    for i, line in enumerate(file_lines):
        ratio = difflib.SequenceMatcher(None, target, line.strip()).ratio()
        if ratio > best_ratio:
            best_idx, best_ratio = i, ratio
    if best_idx is None or best_ratio < 0.5:
        return ""
    lo, hi = max(0, best_idx - context), min(len(file_lines), best_idx + context + 1)
    snippet = "\n".join(f"  {n + 1}: {file_lines[n]}" for n in range(lo, hi))
    return (
        f"\nClosest existing text (line {best_idx + 1}, {best_ratio:.0%} similar to "
        f"your first line):\n{snippet}\n"
        f"Compare this against your old_string character-by-character — the mismatch "
        f"is usually whitespace, quoting, or a nearby edit already applied."
    )

# Shell commands that write to files — blocked when WRITE_REVIEW=true
_WRITE_CMD_RE = re.compile(
    r"\s>>?\s"
    r"|\bsed\s+\S*-i"
    r"|\bperl\s+\S*-i"
    r"|\btee\b"
    r"|\btruncate\b"
    r"|\bdd\b.*of="
)


def _proposed_path(target: Path) -> Path:
    return target.with_name(target.name + _PROPOSED_SUFFIX)


def _inline_diff(original: Path, proposed: Path) -> str:
    """Unified diff for Docker mode (no VS Code available inside container)."""
    try:
        a = original.read_text(encoding="utf-8").splitlines(keepends=True)
        b = proposed.read_text(encoding="utf-8").splitlines(keepends=True)
        lines = list(difflib.unified_diff(
            a, b,
            fromfile=f"original/{original.name}",
            tofile=f"proposed/{original.name}",
            lineterm="",
        ))
        return "".join(lines) if lines else "(no differences)"
    except Exception as exc:
        return f"(diff unavailable: {exc})"


def write_file(relative_path: str, content: str) -> str:
    """
    Write content to a file. Use ONLY for brand-new files that do not exist yet.
    For existing files always use apply_diff() instead — this function is BLOCKED
    on existing files to prevent accidental full rewrites.
    Creates parent directories if needed.

    When WRITE_REVIEW=true the change is staged as a .hive_proposed file.
    STOP after receiving 'review_pending' — the human confirms via hive CLI.

    Args:
        relative_path: Path relative to project root (e.g. 'src/api/new_route.py')
        content:       Full file content to write
    """
    if is_excluded(relative_path):
        return (f"write_file blocked: '{relative_path}' is in an excluded path "
                f"(dependency tree, build output, or a project EXCLUDE_DIRS/EXCLUDE_GLOBS "
                f"entry). Writing there would be lost on the next install or build.")
    target = PROJECT_ROOT / relative_path
    if target.exists():
        return (
            f"write_file blocked: '{relative_path}' already exists. "
            f"Use apply_diff() to make surgical edits to existing files — "
            f"call get_file_content('{relative_path}') first to get the exact text to replace."
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)

        if WRITE_REVIEW:
            is_new = not target.exists()
            proposed = _proposed_path(target)
            proposed.write_text(content, encoding="utf-8")
            action = "new file" if is_new else "changes"
            diff = _inline_diff(target, proposed) if _IN_DOCKER and not is_new else ""
            diff_section = f"\n\nProposed diff:\n```diff\n{diff}\n```" if diff else ""
            return (
                f"review_pending: {relative_path} ({action}){diff_section}\n"
                f"The user will confirm or reject via the hive CLI."
            )

        target.write_text(content, encoding="utf-8")
        return f"written: {relative_path}"
    except Exception as e:
        return f"write_file failed: {e}"


def apply_diff(relative_path: str, old_string: str, new_string: str, preserve_indent: bool = False) -> str:
    """
    Surgical edit — replace an exact string in an existing file.
    Always call get_file_content() first to obtain the exact text to replace.
    Fails safely if old_string is not found or appears more than once.

    When WRITE_REVIEW=true the proposed result is staged for human review.
    You MAY make multiple apply_diff calls to the SAME file — each call
    accumulates into the same .hive_proposed file so the human sees one
    combined diff. STOP and report 'review_pending' only after ALL changes
    to a file are complete, or before modifying a DIFFERENT file.

    IMPORTANT rules:
    - To APPEND after a line: include the anchor in BOTH old_string AND new_string.
      old_string = 'last_line'
      new_string = 'last_line\\nnew_content'
    - To REPLACE a line: put only that line in old_string.
    - Never omit content from new_string unless intentionally deleting it.
    - When adding a new symbol (class, function, variable): update the import
      line FIRST, then add the usage — both as separate apply_diff calls.
    - get_file_content() output is numbered "LINENUM<TAB>content" so citations can
      be copied exactly. That numbering is NOT part of the file — old_string must
      contain only the real code after the tab, never the line number itself.

    Args:
        relative_path: Path relative to project root
        old_string:    Exact text to replace (must appear exactly once)
        new_string:    Replacement text
    """
    # Guard: models sometimes pass the staged path — strip the suffix silently
    if relative_path.endswith(_PROPOSED_SUFFIX):
        relative_path = relative_path[: -len(_PROPOSED_SUFFIX)]

    if is_excluded(relative_path):
        return (f"apply_diff blocked: '{relative_path}' is in an excluded path "
                f"(dependency tree, build output, or a project EXCLUDE_DIRS/EXCLUDE_GLOBS "
                f"entry). Edit the source that generates it instead.")
    target = PROJECT_ROOT / relative_path
    if not target.exists():
        return f"File not found: {relative_path}"
    try:
        proposed = _proposed_path(target)

        # If a staged version already exists, apply the diff on top of it
        # so multiple apply_diff calls accumulate into one proposed file.
        source = proposed if (WRITE_REVIEW and proposed.exists()) else target
        raw = source.read_text(encoding="utf-8")

        # Normalize CRLF → LF so Windows-hosted files match Unix-style old_string.
        # The final write uses the normalized form to keep the file LF-clean.
        content = raw.replace("\r\n", "\n")
        old_string = old_string.replace("\r\n", "\n")
        new_string = new_string.replace("\r\n", "\n")

        count = content.count(old_string)
        if count == 0:
            fail_count = _consecutive_failures.get(relative_path, 0) + 1
            _consecutive_failures[relative_path] = fail_count
            if fail_count > _MAX_CONSECUTIVE_FAILURES:
                return (
                    f"apply_diff HARD STOP: {fail_count} consecutive apply_diff calls against "
                    f"{relative_path} have all failed to find their old_string — even though "
                    f"they were not all the same string, so varying the guess is not the fix. "
                    f"Stop retrying against this file. Report to the coordinator that this file "
                    f"needs a single fresh get_file_content('{relative_path}') read, and that "
                    f"old_string must be an exact copy of the real code AFTER the line-number "
                    f"tab — never the line number itself."
                )
            prev = _last_failed_call.get(relative_path)
            if prev == (old_string, new_string):
                _last_failed_call.pop(relative_path, None)  # reset — a later distinct retry isn't blocked
                return (
                    f"apply_diff STOPPED: this exact old_string/new_string was just retried "
                    f"against {relative_path} and failed again with no change. Repeating it "
                    f"again will not help. Call get_file_content('{relative_path}') ONE more "
                    f"time, read the ENTIRE relevant function, and construct a DIFFERENT, "
                    f"smaller, uniquely-anchored old_string — do not resubmit this one."
                )
            _last_failed_call[relative_path] = (old_string, new_string)
            hint = _near_match_hint(content, old_string)
            if _BARE_LINE_NUMBER_RE.match(old_string):
                hint = (
                    "\nold_string starts with a bare number followed by a newline, which "
                    "looks like a copied 'LINENUM<TAB>content' prefix from get_file_content() "
                    "output. That line number and the tab after it are NOT part of the file's "
                    "real content — strip everything up to and including the tab, and match "
                    "only the actual code that follows it."
                ) + hint
            return (
                f"apply_diff failed: old_string not found in {relative_path}. "
                f"Call get_file_content('{relative_path}') to read the current exact text, "
                f"then retry with the correct old_string.{hint}"
            )
        if count > 1:
            return f"apply_diff failed: old_string appears {count} times — be more specific"

        # Detect a likely-duplicate APPEND before applying it. The append convention
        # (documented above) is old_string = anchor, new_string = anchor + new content
        # -- so when old_string is an UNTOUCHED neighboring symbol (e.g. appending
        # after updateParty's own block, which the insertion never modifies), that
        # anchor still matches exactly once even after a successful insertion. The
        # count>1 check above cannot catch this: the anchor's own count never
        # changes. Measured live 2026-08-05: a model reused the same untouched
        # anchor across 5 separate apply_diff calls, each one succeeding and each
        # one prepending another copy of the same block -- 5 duplicate insertions,
        # 2 of them with mismatched braces from where the model's own wording
        # drifted slightly between calls. Checking whether the NET addition already
        # exists in the file catches this before it lands, regardless of whether
        # the model rechecks get_file_content first.
        if old_string and new_string.startswith(old_string) and new_string != old_string:
            net_addition = new_string[len(old_string):].strip()
            if net_addition and net_addition in content:
                return (
                    f"apply_diff REFUSED: the content you are about to append to "
                    f"{relative_path} already appears in the file — this looks like a "
                    f"repeat of an already-successful insertion, not a new distinct "
                    f"change. Call get_file_content('{relative_path}"
                    f"{_PROPOSED_SUFFIX if (WRITE_REVIEW and proposed.exists()) else ''}') "
                    f"to see the current state. If you genuinely need to insert this "
                    f"again elsewhere, anchor old_string somewhere else."
                )

        # A NAME collision, not a TEXT collision. The check above only catches
        # re-appending BYTE-IDENTICAL content after an untouched anchor; it does
        # nothing when new_string defines a DIFFERENT rule under the SAME selector.
        # Measured live 2026-08-06: a citation-correction retry staged a second
        # ".statusBadge { ... }" using different color variables (index.$accent-bg-
        # muted instead of the first, already-correct index.$success-bg) right after
        # an existing one -- two rules, same selector, neither textually identical to
        # the other so the check above never fired. Compares against content with
        # old_string removed first, so a genuine "replace this exact block with an
        # edited version of itself" edit (old_string already contains the selector
        # being replaced) is never mistaken for a collision.
        if relative_path.endswith((".scss", ".sass")):
            content_without_old = content.replace(old_string, "", 1) if old_string else content
            existing_selectors = set(_SCSS_SELECTOR_RE.findall(content_without_old))
            collisions = [s for s in _SCSS_SELECTOR_RE.findall(new_string) if s in existing_selectors]
            if collisions:
                return (
                    f"apply_diff REFUSED: {', '.join(collisions)} already exists as a "
                    f"rule in {relative_path} — a selector cannot be defined twice. If "
                    f"you meant to modify the existing rule, call get_file_content("
                    f"'{relative_path}{_PROPOSED_SUFFIX if (WRITE_REVIEW and proposed.exists()) else ''}') "
                    f"to see its current content and target THAT block with old_string, "
                    f"instead of appending a new one."
                )

        _last_failed_call.pop(relative_path, None)
        _consecutive_failures.pop(relative_path, None)

        proposed_content = content.replace(old_string, new_string, 1)

        if WRITE_REVIEW:
            proposed.write_text(proposed_content, encoding="utf-8")
            diff = _inline_diff(target, proposed) if _IN_DOCKER else ""
            diff_section = f"\n\nProposed diff:\n```diff\n{diff}\n```" if diff else ""
            return (
                f"review_pending: {relative_path} — this change is now staged.{diff_section}\n"
                f"If you have MORE changes for this file: call get_file_content('{relative_path}.hive_proposed') "
                f"to see the current staged state, then apply ONLY the NEXT distinct change not yet staged.\n"
                f"DO NOT re-apply a change that is already in the staged file.\n"
                f"STOP and report 'review_pending: {relative_path}' only when ALL changes to this file are complete."
            )

        target.write_text(proposed_content, encoding="utf-8")
        return f"applied: {relative_path}"
    except Exception as e:
        return f"apply_diff failed: {e}"


def run_command(command: str, timeout: int = 120) -> str:
    """
    Run a shell command in the project root. Returns stdout + stderr + exit code.

    READ-ONLY use only: tests, linters, build checks, grep, git status.
    NEVER use to write files — no >, >>, sed -i, tee, perl -i.
    Use apply_diff() or write_file() for ALL file modifications.

    Args:
        command: Shell command (e.g. 'pytest tests/ -v', 'npm run lint', 'git status')
        timeout: Seconds before timeout (default 120)
    """
    import subprocess

    if WRITE_REVIEW and _WRITE_CMD_RE.search(command):
        return (
            "blocked: run_command cannot write files when WRITE_REVIEW is enabled. "
            "Use apply_diff() to edit an existing file or write_file() to create a new one."
        )
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=timeout,
        )
        parts = []
        if result.stdout.strip():
            parts.append(result.stdout.strip())
        if result.stderr.strip():
            parts.append(f"[stderr]\n{result.stderr.strip()}")
        parts.append(f"[exit {result.returncode}]")
        return "\n".join(parts)
    except __import__("subprocess").TimeoutExpired:
        return f"run_command timed out after {timeout}s"
    except Exception as e:
        return f"run_command failed: {e}"
