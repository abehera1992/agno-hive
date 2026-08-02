# Groundedness Test Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three independent, root-caused gaps found during a live groundedness test of the deployed hive swarm on 2026-08-01: a no-evidence guard blind spot on bare numeric claims, a fabricated-code-attribute blind spot in `verify_claims`, and an unproductive `apply_diff` retry loop with no diagnostic feedback.

**Architecture:** Each fix widens or extends an existing, already-proven mechanism rather than adding a new one — matching the "mechanisms constrain, instructions don't" pattern already established this session (the `read_only` flag, the no-evidence guard itself). No new files, no new tools; three surgical, independently testable and independently deployable changes.

**Tech Stack:** Python 3.12, pytest (`asyncio_mode = auto`), `difflib` (stdlib, already imported in `files.py`), ripgrep (`rg`, already a hard dependency of `verify.py`'s `_rg`).

## Global Constraints

- Every fix is grep/regex/stdlib-based — no new external dependencies.
- Follow the existing test-location convention: `tests/` (pytest, path-agnostic, `asyncio_mode=auto`) for `swarm/team.py`-level code; `hive-mcp/tests/` (pytest, path-inserts `hive-mcp/` onto `sys.path`, monkeypatches module-local names, not `config.X` — see Task 3 note) for `hive-mcp/tools/*.py`-level code. Both conventions were established earlier this session for the skills-architecture work — do not invent a third pattern.
- `hive-mcp/tools/files.py` imports `PROJECT_ROOT`/`WRITE_REVIEW` via `from config import PROJECT_ROOT, WRITE_REVIEW` (direct name import, not `import config`). Monkeypatching `config.PROJECT_ROOT` in a test has **no effect** on this file — you must monkeypatch `files.PROJECT_ROOT` / `files.WRITE_REVIEW` directly, the module-local bound names. `hive-mcp/tools/verify.py` does `import config` then `config.PROJECT_ROOT` (attribute access) — the opposite convention — so monkeypatching `config.PROJECT_ROOT` works there. Check which style a file uses before writing its test.
- After all three tasks land: redeploy hive-mcp via the established cycle — commit, push to `agno-hive` `main`, wait for the GHCR Action (`ghcr.io/abehera1992/hive-mcp:latest`) to finish building, then from `EkamApp/` (NOT `agno-hive/hive-mcp/`) run `docker compose -f docker-compose.hive.yml pull hive-mcp && docker rm -f hive-mcp && docker compose -f docker-compose.hive.yml up -d hive-mcp`. **Must use `EkamApp/docker-compose.hive.yml`**, not agno-hive's own copy — agno-hive's copy only passes 7 named env vars through `--env-file`, while EkamApp's uses `env_file: hive-mcp.env` (injects the whole file) — using the wrong one silently drops `CODE_LINT_*`/`HIVE_DB_URL`/etc., a mistake already made and caught once this session.

---

## File Structure

```
swarm/
  team.py                          # MODIFIED — widen _CLAIMY_RE (Task 1)

tests/
  test_claimy_re.py                # NEW — Task 1 regex unit tests

hive-mcp/
  tools/
    verify.py                      # MODIFIED — extract dotted idents from fenced code (Task 2)
    files.py                       # MODIFIED — near-match hint + repeat-failure circuit breaker (Task 3)
  skills/
    file-write-review/SKILL.md     # MODIFIED — instruct verify_claims on code, not just prose (Task 2)
  tests/
    test_verify.py                 # NEW — Task 2 tests
    test_files.py                  # NEW — Task 3 tests
```

---

### Task 1: Widen the no-evidence guard's claim detector

**Files:**
- Modify: `swarm/team.py` (the `_CLAIMY_RE` definition, currently right after the `_READ_TOOLS` set)
- Test: `tests/test_claimy_re.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `_CLAIMY_RE` (same name, same module, wider pattern) — used unchanged by `_verified_answer` in the same file; no caller-side change needed

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claimy_re.py
from swarm.team import _CLAIMY_RE


def test_matches_existing_backticked_symbol():
    assert _CLAIMY_RE.search("The function is `getUser`.")


def test_matches_existing_file_line_citation():
    assert _CLAIMY_RE.search("See items_api.py:209 for the signature.")


def test_matches_bare_count_claim_with_trailing_noun():
    assert _CLAIMY_RE.search("There are 3 active, 3 inactive, 6 total items.")


def test_matches_count_of_phrasing():
    assert _CLAIMY_RE.search("The count of active users is 42.")


def test_does_not_match_plain_conversational_reply():
    assert not _CLAIMY_RE.search("Sure, that makes sense — go ahead.")


def test_does_not_match_could_not_verify():
    assert not _CLAIMY_RE.search("I could not verify this without reading the file.")
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `python -m pytest tests/test_claimy_re.py -v`
Expected: the first two (existing backtick / file:line cases) PASS already; `test_matches_bare_count_claim_with_trailing_noun` and `test_matches_count_of_phrasing` FAIL (current regex doesn't cover them)

- [ ] **Step 3: Widen the regex**

In `swarm/team.py`, find:

```python
# Claims that need evidence: a backticked identifier, or a path:line citation.
_CLAIMY_RE = re.compile(r"`[A-Za-z_][A-Za-z0-9_.]{2,}`|[\w./-]+\.\w{1,6}:\d+")
```

Replace with:

```python
# Claims that need evidence: a backticked identifier, a path:line citation, or a
# bare quantitative claim ("3 active, 3 inactive, 6 total items", "count of X is 42").
# The first two forms were the only ones covered until 2026-08-01: a live-DB question
# was answered "3 active / 3 inactive / 6 total" with ZERO tool calls (confirmed via
# hive-mcp AND project-MCP trace logs across a 15-minute window) and this guard never
# fired, because a bare number next to a count-flavoured noun matched neither pattern.
# The answer happened to be correct by luck; the guard exists to not depend on luck.
_CLAIMY_RE = re.compile(
    r"`[A-Za-z_][A-Za-z0-9_.]{2,}`"
    r"|[\w./-]+\.\w{1,6}:\d+"
    r"|\b\d+\b[^.\n]{0,40}\b(rows?|records?|items?|entries|count|total)\b"
    r"|\b(count|total|number) of\b[^.\n]{0,40}\b\d+\b",
    re.IGNORECASE,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_claimy_re.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full existing suite to confirm no regression**

Run: `python -m pytest tests/ -v --deselect tests/test_bootstrap.py::test_load_from_session_discovers_patterns --deselect tests/test_bootstrap.py::test_load_from_session_skips_failed_file_reads`
Expected: PASS (the two deselected tests are pre-existing failures unrelated to this repo area, confirmed earlier this session by stashing all changes and re-running)

- [ ] **Step 6: Commit**

```bash
git add swarm/team.py tests/test_claimy_re.py
git commit -m "fix(swarm): widen no-evidence guard to catch bare quantitative claims"
```

---

### Task 2: `verify_claims` checks code blocks, not just prose

**Files:**
- Modify: `hive-mcp/tools/verify.py`
- Modify: `hive-mcp/skills/file-write-review/SKILL.md`
- Test: `hive-mcp/tests/test_verify.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `_code_idents(answer: str) -> list[str]` (new function in `verify.py`) — internal, called only from `verify_claims` in the same module; no external caller needs it

- [ ] **Step 1: Write the failing tests**

```python
# hive-mcp/tests/test_verify.py
from tools import verify


def test_extracts_dotted_identifier_from_fenced_code_block(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])  # nothing found anywhere
    answer = "Here is the code:\n```python\nx = item.stock_quantity\n```"

    report = verify.verify_claims(answer)

    assert "stock_quantity" in report
    assert "NOT FOUND" in report


def test_finds_dotted_identifier_when_rg_returns_a_hit(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:12:    sku = Column(String)"])
    answer = "```python\nx = item.sku\n```"

    report = verify.verify_claims(answer)

    assert "FOUND" in report
    assert "NOT FOUND" not in report


def test_skips_stdlib_prefixes_in_code_blocks(monkeypatch):
    calls = []
    def fake_rg(tok, **k):
        calls.append(tok)
        return []
    monkeypatch.setattr(verify, "_rg", fake_rg)
    answer = "```python\nimport csv\nw = csv.writer(f)\noutput = io.StringIO()\n```"

    verify.verify_claims(answer)

    assert not any("csv.writer" in c for c in calls)
    assert not any("io.StringIO" in c for c in calls)


def test_prose_backtick_extraction_still_works(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "The function is `doTheThing`."

    report = verify.verify_claims(answer)

    assert "doTheThing" in report
    assert "NOT FOUND" in report


def test_code_block_and_prose_idents_are_deduplicated(monkeypatch):
    calls = []
    def fake_rg(tok, **k):
        calls.append(tok)
        return []
    monkeypatch.setattr(verify, "_rg", fake_rg)
    answer = "Uses `item.stock_quantity`.\n```python\nx = item.stock_quantity\n```"

    verify.verify_claims(answer)

    assert calls.count("item.stock_quantity") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hive-mcp && python -m pytest tests/test_verify.py -v`
Expected: FAIL — `stock_quantity`/`item.sku` never appear in the report (code blocks aren't scanned yet); the stdlib-skip and dedup tests will pass vacuously or fail depending on exact assertions, but the first two are the real signal

- [ ] **Step 3: Implement `_code_idents` and wire it into `verify_claims`**

In `hive-mcp/tools/verify.py`, add after the existing `_NOISE` set and before `_FENCE_RE`:

```python
# Module/package prefixes common enough that `prefix.attribute` inside generated code
# is almost always a legitimate stdlib/framework call, not a project symbol worth
# checking. Without this, csv.writer / io.StringIO / datetime.now flood the report
# with NOT FOUND false positives — observed directly in a live test, 2026-08-01.
_STDLIB_PREFIXES = {
    "os", "sys", "io", "csv", "json", "re", "time", "logging", "subprocess",
    "pathlib", "datetime", "typing", "asyncio", "functools", "itertools",
    "collections",
}
_CODE_DOTTED_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b")
```

Then, after `_FENCE_RE` (which already exists for `_lint_code`), add:

```python
def _code_idents(answer: str) -> list[str]:
    """Dotted attribute-access tokens (item.stock_quantity, Model.field) found INSIDE
    fenced code blocks — not the prose summary around them.

    verify_claims previously only read backticked spans in prose (see the "no
    checkable claims found" message below), so a fabricated attribute that only
    appeared in the generated code — never restated in backticks in the summary —
    passed every check. Measured 2026-08-01: two independent write tasks both used
    `item.stock_quantity` / `Item.stock_quantity`, a field that does not exist
    anywhere in the project, and neither was caught, because the fabricated name
    never appeared outside the ```python block.
    """
    idents: list[str] = []
    for block in _FENCE_RE.findall(answer or ""):
        for tok in _CODE_DOTTED_RE.findall(block):
            left = tok.split(".", 1)[0].lower()
            if left in _STDLIB_PREFIXES or left in _NOISE:
                continue
            if tok not in idents:
                idents.append(tok)
    return idents
```

Then modify `verify_claims`'s ident-collection block (currently):

```python
    idents: list[str] = []
    for span in _BACKTICK_RE.findall(answer):
        tok = span.strip().rstrip("()").strip()
        if (_IDENT_RE.match(tok) or _DOTTED_RE.match(tok)) and tok.lower() not in _NOISE:
            if tok not in idents:
                idents.append(tok)
```

to:

```python
    idents: list[str] = []
    for span in _BACKTICK_RE.findall(answer):
        tok = span.strip().rstrip("()").strip()
        if (_IDENT_RE.match(tok) or _DOTTED_RE.match(tok)) and tok.lower() not in _NOISE:
            if tok not in idents:
                idents.append(tok)
    for tok in _code_idents(answer):
        if tok not in idents:
            idents.append(tok)
```

(The `_rg(tok, fixed=True, glob_filter=glob_filter, whole_word=not dotted)` call later in `verify_claims` already handles any token containing a `.` as a dotted, fixed-string search — no change needed there; code-derived tokens flow through the exact same SYMBOLS reporting section as prose-derived ones.)

Finally, update the "nothing to check" message for accuracy (currently says "no backticked symbols" but code blocks are now also checked):

```python
    if not (idents or file_lines or routes) and not _lint_code(answer):
        return ("verify_claims: no checkable claims found (no backticked symbols, "
                "code-block attribute references, file:line citations, API routes, "
                "or convention violations).")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hive-mcp && python -m pytest tests/test_verify.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Update the file-write-review skill to point verify_claims at the actual code**

In `hive-mcp/skills/file-write-review/SKILL.md`, add a new paragraph after the existing "File editing rules:" section and before "run_command is READ-ONLY (CRITICAL):":

```markdown
Before staging ANY code via apply_diff or write_file: call verify_claims on the
CODE you are about to write (the new_string / file content itself), not just the
prose summary you plan to return. verify_claims checks fenced code blocks for
fabricated attribute access (e.g. `item.stock_quantity` when no such field exists)
in addition to backticked symbols in prose — but only if you give it the code to
check. Restating a symbol in your summary is not the same as checking the code
that will actually be applied.
```

- [ ] **Step 6: Run the full hive-mcp test suite to confirm no regression**

Run: `cd hive-mcp && python -m pytest tests/ -v`
Expected: PASS (11 tests: 6 from `test_skills.py` + 5 from `test_verify.py`)

- [ ] **Step 7: Commit**

```bash
git add hive-mcp/tools/verify.py hive-mcp/skills/file-write-review/SKILL.md hive-mcp/tests/test_verify.py
git commit -m "fix(hive-mcp): verify_claims checks fenced code blocks, not just prose"
```

---

### Task 3: `apply_diff` gives a diagnostic hint and stops repeat failures

**Files:**
- Modify: `hive-mcp/tools/files.py`
- Test: `hive-mcp/tests/test_files.py`

**Interfaces:**
- Consumes: nothing new (`difflib` already imported at module top for `_inline_diff`)
- Produces: `_near_match_hint(content: str, old_string: str, context: int = 2) -> str` and module-level `_last_failed_call: dict[str, tuple[str, str]]` — both internal to `files.py`, no external caller needs them

- [ ] **Step 1: Write the failing tests**

```python
# hive-mcp/tests/test_files.py
from tools import files


def _setup(tmp_path, monkeypatch, initial_text):
    # files.py does `from config import PROJECT_ROOT, WRITE_REVIEW` (direct name
    # import) — monkeypatching config.PROJECT_ROOT has NO effect here. Patch the
    # module-local bound names directly.
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "WRITE_REVIEW", False)
    f = tmp_path / "sample.py"
    f.write_text(initial_text, encoding="utf-8")
    files._last_failed_call.clear()
    return f


def test_apply_diff_failure_includes_near_match_hint(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    status_filter = 'active'\n    return status_filter\n")

    result = files.apply_diff("sample.py", "status_filter = 'inactive'", "status_filter = 'archived'")

    assert "old_string not found" in result
    assert "status_filter = 'active'" in result  # the near-match hint shows the real line


def test_apply_diff_no_hint_when_nothing_close(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    result = files.apply_diff("sample.py", "completely unrelated text with no resemblance", "x")

    assert "old_string not found" in result
    assert "Closest existing text" not in result


def test_apply_diff_second_identical_failure_hard_stops(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    first = files.apply_diff("sample.py", "does not exist", "replacement")
    second = files.apply_diff("sample.py", "does not exist", "replacement")

    assert "old_string not found" in first
    assert "STOPPED" in second


def test_apply_diff_success_clears_failure_tracking(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    files.apply_diff("sample.py", "does not exist", "replacement")       # fails, tracked
    files.apply_diff("sample.py", "return 1", "return 2")                # succeeds, clears tracking
    third = files.apply_diff("sample.py", "does not exist", "replacement")  # same failure again

    assert "STOPPED" not in third


def test_apply_diff_different_failure_after_first_is_not_treated_as_repeat(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    first = files.apply_diff("sample.py", "does not exist", "replacement")
    second = files.apply_diff("sample.py", "also does not exist", "other replacement")

    assert "STOPPED" not in second
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hive-mcp && python -m pytest tests/test_files.py -v`
Expected: FAIL — no near-match hint exists yet, no `_last_failed_call` tracking, `STOPPED` never appears

- [ ] **Step 3: Implement `_near_match_hint` and the repeat-failure circuit breaker**

In `hive-mcp/tools/files.py`, add after the existing `_IN_DOCKER` constant:

```python
# Detects a stuck retry loop: the exact same failing apply_diff call repeated
# verbatim against the same file. Module-level and intentionally coarse — hive-mcp
# runs as one long-lived process, and this only needs to catch "the identical call,
# again," not build a general call history. Measured 2026-08-01: a single task
# burned 30-40+ tool calls retrying against a file that kept returning the same
# generic "old_string not found" message with no information about why.
_last_failed_call: dict[str, tuple[str, str]] = {}


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
```

Then modify `apply_diff`'s `count == 0` branch (currently):

```python
        count = content.count(old_string)
        if count == 0:
            return (
                f"apply_diff failed: old_string not found in {relative_path}. "
                f"Call get_file_content('{relative_path}') to read the current exact text, "
                f"then retry with the correct old_string."
            )
        if count > 1:
            return f"apply_diff failed: old_string appears {count} times — be more specific"
```

to:

```python
        count = content.count(old_string)
        if count == 0:
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
            return (
                f"apply_diff failed: old_string not found in {relative_path}. "
                f"Call get_file_content('{relative_path}') to read the current exact text, "
                f"then retry with the correct old_string.{hint}"
            )
        if count > 1:
            return f"apply_diff failed: old_string appears {count} times — be more specific"
        _last_failed_call.pop(relative_path, None)
```

(The final `_last_failed_call.pop(relative_path, None)` sits right before `proposed_content = content.replace(...)` — on the success path, so a later genuine failure against this file is never mistaken for a repeat of an old one.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hive-mcp && python -m pytest tests/test_files.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full hive-mcp test suite to confirm no regression**

Run: `cd hive-mcp && python -m pytest tests/ -v`
Expected: PASS (16 tests: 6 `test_skills.py` + 5 `test_verify.py` + 5 `test_files.py`)

- [ ] **Step 6: Commit**

```bash
git add hive-mcp/tools/files.py hive-mcp/tests/test_files.py
git commit -m "fix(hive-mcp): apply_diff gives a near-match hint and stops repeat failures"
```

---

### Task 4: Deploy and re-verify against the live swarm

**Files:** none (operational task)

**Interfaces:**
- Consumes: all of Tasks 1-3's committed changes
- Produces: nothing further downstream — this is the plan's terminal task

- [ ] **Step 1: Push to origin**

```bash
git push origin main
```

- [ ] **Step 2: Wait for the GHCR build to complete**

Check via the GitHub API (public, no auth needed for a public repo):
`https://api.github.com/repos/abehera1992/agno-hive/actions/runs?per_page=1`
Expected: the most recent "Build and push hive-mcp image" run shows `"conclusion":"success"` with a `head_sha` matching the commit just pushed.

- [ ] **Step 3: Redeploy hive-mcp from EkamApp's compose file**

```bash
cd /path/to/EkamApp
docker compose -f docker-compose.hive.yml pull hive-mcp
docker rm -f hive-mcp
docker compose -f docker-compose.hive.yml up -d hive-mcp
```

Verify: `docker inspect hive-mcp --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'` shows the EkamApp path mounted to `/project`, and `docker ps` shows `hive-mcp` as `healthy`.

- [ ] **Step 4: Re-run the DB-check case that originally caught Task 1's bug**

Via `agno_run` (or the raw `/run` endpoint), ask the same live-DB question used in the original test: *"Check the live EkamApp database: how many rows exist in the items table right now, and what is the breakdown by is_active status? You must use the live database (db_query/db_schema), not a file grep or a guess."*

Then check `docker logs hive-mcp --since 5m 2>&1 | grep -i "db_query\|db_schema"`.
Expected: at least one `db_query` or `db_schema` call now appears — the no-evidence guard forced a retry that used the real tool, rather than accepting a bare numeric claim.

- [ ] **Step 5: Re-run the min_stock case that originally caught Task 2's bug**

Ask the swarm to repeat the `min_stock` filter task from the original test (same prompt: add an optional `min_stock` query parameter to `GET /items` filtering by stock quantity). Read the resulting `.hive_proposed` diff.
Expected: either (a) the diff no longer references a nonexistent `Item.stock_quantity` field — it correctly finds/uses the real stock-tracking path — or (b) `verify_claims` flags the fabricated field in its own output before the answer is returned, which the response should surface (via the existing `_verified_answer` correction round in `swarm/team.py`). Either outcome is a pass; silently repeating the identical fabrication with no flag is not.

Clean up: `rm -f <path>/items_api.py.hive_proposed` after reviewing — do not leave test artifacts staged, matching the cleanup discipline from the original test.

- [ ] **Step 6: Report the before/after comparison**

Summarize: guard trigger rate on the DB-check case (0 tool calls before -> N after), whether the fabricated-field case is now caught, and whether the retry-loop case (if reproducible) now terminates faster with a hint instead of 30+ blind retries.

---

## Self-Review

**Spec coverage:**
- Issue 1 (no-evidence guard blind spot on bare numeric claims) → Task 1 ✓
- Issue 2 (verify_claims never scans fenced code blocks; stdlib false positives) → Task 2 ✓
- Issue 2's file-write-review skill instruction (call verify_claims on code, not just prose) → Task 2 Step 5 ✓
- Issue 3a (no diagnostic info on apply_diff failure) → Task 3 (`_near_match_hint`) ✓
- Issue 3b (circuit breaker for repeated identical failures) → Task 3 (`_last_failed_call`) ✓
- Redeployment + live re-verification → Task 4 ✓

**Placeholder scan:** no TBD/TODO; every step has real code, a real regex, or a real shell command.

**Type consistency:** `_code_idents(answer: str) -> list[str]` (Task 2) feeds directly into the existing `idents: list[str]` in `verify_claims` — same element type (plain `str` tokens), verified against the existing backtick-derived tokens' type. `_near_match_hint(content: str, old_string: str, context: int = 2) -> str` (Task 3) is called with exactly the two required positional args at its one call site; `context` keeps its default. `_last_failed_call: dict[str, tuple[str, str]]` keys on `relative_path` (already a function parameter of the same name and type throughout `apply_diff`) and stores exactly the `(old_string, new_string)` pair compared against on the next call — types match at both write and read sites.

**Known honesty note carried into the plan itself** (matching this codebase's existing documentation style, e.g. `verify.py`'s own docstring on what it does and doesn't catch): Task 3's circuit breaker only catches a **byte-for-byte identical** repeat of `(old_string, new_string)`. The original investigation could not fully determine, from the truncated 60-char argument previews in the trace log, whether the observed retries were textually identical or slowly growing (the `get_file_content` read window grew by 10 lines each retry). The near-match hint (the other half of Task 3) is the fix that helps regardless of which case actually occurred; the circuit breaker is a targeted backstop for the identical-repeat sub-case specifically, not a claim that it fixes every possible retry pattern.

---

**Plan complete and saved to `docs/superpowers/plans/2026-08-01-groundedness-test-fixes.md`.**
