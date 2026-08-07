"""Confirms the new persistent-bash tools (hive-mcp/tools/bash.py) are registered
as mutating tools -- required so read-only teams (planning, parallel-review) can
never obtain shell access via bash_run, mirroring run_command/run_shell/run_docker's
existing treatment. See swarm/team.py's _MUTATING_TOOLS / _is_mutating for how this
single set drives both _strip_mutating and _scope_coordinator_tools."""
from swarm.team import _MUTATING_TOOLS, _is_mutating


def test_bash_mutating_tools_are_registered():
    assert {"bash_session_start", "bash_run", "bash_session_close", "bash_job_kill"} <= _MUTATING_TOOLS


def test_bash_job_status_is_not_mutating():
    """bash_job_status (Phase 2, read-only poll) must stay excluded -- a read-only
    team can never obtain a job_id anyway since bash_run itself is stripped for
    them, so marking the poll itself mutating would only be unnecessarily strict."""
    assert not _is_mutating("bash_job_status")


def test_bash_run_and_session_tools_are_mutating():
    assert _is_mutating("bash_session_start")
    assert _is_mutating("bash_run")
    assert _is_mutating("bash_session_close")
    assert _is_mutating("bash_job_kill")
