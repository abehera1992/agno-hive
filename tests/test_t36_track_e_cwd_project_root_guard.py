"""T36 Track E -- preserve and strengthen T9's first-ever PASS.

T9 asks for OS, Python version, and current working directory. It failed all
3 previously recorded runs by relabeling get_env_info's "Project root" field
(a configured constant, PROJECT_ROOT) as "Current working directory" (the
real os.getcwd() value) -- a tool-level defect already fixed and tested in
hive-mcp/tools/shell.py (see hive-mcp/tests/test_get_env_info_cwd.py, Phase
7/7-followup). The 2026-10-19 battery re-run (session 4d262171-380d-484c-
8d9a-4d8bf8048b4e, recovered verbatim from this conversation's own
transcript) passed for the first time ever: the delivered answer correctly
reported "Current Working Directory: /app" while the run's own captured
`get_env_info` evidence read (verbatim, from the answer's own "WHAT THE
TOOLS ACTUALLY RETURNED" disclosure block):

    OS: Linux 6.6.87.2-microsoft-standard-WSL2 (x86_64) Python: 3.12.15 at
    /usr/local/bin/python Project root: /project Current working directory:
    /app

-- /app (cwd) and /project (project root) are genuinely different values,
and the delivered answer used the correct one.

Investigation finding: the underlying tool-level fix (hive-mcp's
get_env_info) and its test coverage already exist and are unrelated to this
track's scope. What does NOT exist anywhere is a SWARM-layer deterministic
backstop for this specific historical relabeling failure -- unlike OS and
Python version, which `_integrity_env_fact_contradiction` already checks
against get_env_info's own captured preview
(test_evidence_integrity.py::test_env_python_version_contradiction_detected
/ test_env_os_contradiction_detected), the cwd/project-root distinction had
zero coverage at this layer. A relapse of the 3-of-4 historical failure
would have shipped silently even after this battery's own PASS.

Per this track's own instruction ("if investigation identifies a genuine
remaining weakness, implement only a separately justified change and
demonstrate it does not regress the previously passing case"): added one
narrow, same-pattern cwd check to `_integrity_env_fact_contradiction`
(mirrors the existing OS/Python checks exactly) rather than leaving this
unprotected. This is NOT a change to delegation, labeling, or tool
behavior -- it is a disclosure-only backstop, identical in shape and risk
to the sibling checks these tests also exercise for non-regression.
"""
from types import SimpleNamespace

from swarm.team import _integrity_env_fact_contradiction

# The real get_env_info preview text from the T9 live run, verbatim.
_T9_REAL_PREVIEW = (
    "OS: Linux 6.6.87.2-microsoft-standard-WSL2 (x86_64) Python: 3.12.15 at "
    "/usr/local/bin/python Project root: /project Current working "
    "directory: /app"
)


def _team(preview=_T9_REAL_PREVIEW):
    return SimpleNamespace(_tool_evidence=[
        {"name": "get_env_info", "agent": "Executor", "preview": preview, "chars": 200},
    ])


# ── 1. Raw environment values map to the correct labels ────────────────────

def test_the_real_t9_delivered_answer_is_not_flagged():
    """The actual delivered T9 answer this battery produced: all three
    fields correct, 'Current Working Directory: /app' labeled correctly."""
    answer = (
        "- Operating System: Linux 6.6.87.2-microsoft-standard-WSL2 (x86_64)\n"
        "- Python Version: 3.12.15\n"
        "- Current Working Directory: /app\n"
    )
    assert _integrity_env_fact_contradiction(answer, _team()) is None


# ── 2. Project root and current working directory remain distinct ─────────

def test_relabeling_project_root_as_cwd_is_caught():
    """The exact historical defect (3 of 4 prior runs): the answer states the
    PROJECT ROOT value under the "Current working directory" label."""
    answer = "The current working directory is: /project"
    found = _integrity_env_fact_contradiction(answer, _team())
    assert found == ("current working directory", "/project", "/app")


def test_correct_cwd_value_is_silent_even_when_project_root_also_mentioned():
    answer = (
        "The project root is `/project`, and the current working directory "
        "is: /app"
    )
    assert _integrity_env_fact_contradiction(answer, _team()) is None


# ── 3. Missing/unavailable values are not silently substituted ─────────────

def test_no_cwd_claim_in_the_answer_is_silent():
    """A task that never states a cwd value at all must not be flagged --
    this is a contradiction check, not a completeness requirement."""
    answer = "Operating System: Linux. Python Version: 3.12.15."
    assert _integrity_env_fact_contradiction(answer, _team()) is None


def test_no_get_env_info_evidence_this_run_is_silent():
    team = SimpleNamespace(_tool_evidence=[])
    answer = "The current working directory is: /project"
    assert _integrity_env_fact_contradiction(answer, team) is None


# ── 4. Delegation to Executor does not alter the meaning of these fields ───

def test_cwd_check_reads_delegated_executor_evidence_the_same_way():
    """_tool_evidence's own "agent" field is Executor here (as in the real
    T9 run, which delegated get_env_info to it) -- the check must not require
    the coordinator to have called the tool directly."""
    team = SimpleNamespace(_tool_evidence=[
        {"name": "get_env_info", "agent": "Executor", "preview": _T9_REAL_PREVIEW,
         "chars": 200},
    ])
    answer = "The current working directory is: /project"
    found = _integrity_env_fact_contradiction(answer, team)
    assert found == ("current working directory", "/project", "/app")


# ── Sibling OS/Python checks keep working alongside the new cwd check ──────

def test_os_and_python_checks_unaffected_by_the_new_cwd_field():
    answer = "OS: Ubuntu 22.04.4 LTS, Python 3.11.6, cwd is: /app"
    found = _integrity_env_fact_contradiction(answer, _team())
    # Python is checked before OS/cwd in source order; a genuinely wrong
    # Python claim is reported even with a correct cwd value alongside it --
    # the new cwd check added nothing that suppresses the existing ones.
    assert found is not None
    assert found[0] == "Python version"
