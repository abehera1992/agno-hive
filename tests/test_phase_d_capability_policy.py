"""Phase D: capability surface hardening.

A1/A2 correctly stripped the actual tool list, the Coordinator's roster, and instruction
lines that named a removed tool -- but two leaks remained, both confirmed live (2026-09-21
post-deployment battery, T8): a role DESCRIPTION could still tell a read-only member to use
a tool it no longer held ("Use apply_diff() for existing files, write_file() only for new
ones." on a read-only Coder), and a granted SKILL's own L1 catalog line could still describe
a capability family entirely removed under read_only (Executor's `bash-sessions` skill names
bash_session_start/bash_run/bash_job_status/bash_job_kill, all stripped) -- and the Executor
tried bash_session_start once as a direct result. A3 contained that one call; this closes
the leak that caused it.

One policy (_apply_capability_policy), four surfaces (tools, instructions, description,
skills), the same underlying fact per spec: which tools are `kept`. No independent
string-blacklist -- every check here still goes through _is_mutating.
"""
import re

import pytest

from api.models import AgentSpec
from swarm.team import (
    _apply_capability_policy, _description_without_unavailable_tool_mentions,
    _skills_without_unavailable_tool_refs, _strip_mutating, _team_roster_preamble,
    _unavailable_tool_tokens,
)

CODER_DESCRIPTION = (
    "Implementation specialist. Write clean, idiomatic code following existing patterns. "
    "Use apply_diff() for existing files, write_file() only for new ones.")
EXECUTOR_DESCRIPTION = (
    "Execution and validation specialist. Run commands and report exact stdout/stderr — "
    "never paraphrase errors.")
BASH_SESSIONS_DESC = (
    "How to use persistent bash sessions (bash_session_start/bash_run/bash_session_close) "
    "and background jobs (bash_run background=True, bash_job_status, bash_job_kill) -- when "
    "to use this vs run_command/run_shell.")
FILE_WRITE_REVIEW_DESC = (
    "How to edit files (apply_diff vs write_file), what review_pending means, and what "
    "run_command may and may not do -- load before making any file change.")
VERIFICATION_DESC = "How to check a claim before stating it as fact."
CODE_CONVENTIONS_DESC = "Project code style conventions."

CATALOG = [
    {"name": "bash-sessions", "description": BASH_SESSIONS_DESC},
    {"name": "file-write-review", "description": FILE_WRITE_REVIEW_DESC},
    {"name": "verification-discipline", "description": VERIFICATION_DESC},
    {"name": "code-conventions", "description": CODE_CONVENTIONS_DESC},
]


def _spec(name="Coder", tools=None, instructions=None, description=None, skills=None):
    return AgentSpec(name=name, role="r", model="m", tools=tools, skills=skills,
                     instructions=instructions or [], description=description)


CODER_TOOLS = ["get_file_content", "apply_diff", "write_file", "run_command", "lightrag_insert"]
EXECUTOR_TOOLS = ["get_file_content", "check_port", "bash_session_start", "bash_run",
                  "bash_session_close", "bash_job_status", "bash_job_kill", "run_command",
                  "run_shell", "run_docker"]


# ── 1. actual read-only tool roster remains correct (A1 unchanged) ──────────────────────

def test_read_only_tool_roster_is_unchanged_by_phase_d():
    spec = _spec(tools=CODER_TOOLS, description=CODER_DESCRIPTION,
                 skills=["code-conventions", "file-write-review"])
    (out,), _ = _strip_mutating([spec], None, CATALOG)
    assert out.tools == ["get_file_content"]


def test_normal_non_read_only_mode_never_calls_the_policy_at_all():
    """_strip_mutating is only ever invoked when read_only=True (both call sites in
    run_task_stream/run_task_async gate on `if read_only`); normal mode leaves specs
    untouched by construction, not by a second code path that could drift."""
    import inspect
    from swarm import team as team_mod
    for fn in (team_mod.run_task_stream, team_mod.run_task_async):
        src = inspect.getsource(fn)
        assert "_strip_mutating(agent_specs, coordinator_tools, skill_catalog)" in src
        i = src.index("_strip_mutating(agent_specs, coordinator_tools, skill_catalog)")
        assert "if read_only" in src[i:i + 120]


# ── 2. apply_diff/write_file/run_command/bash_session_* not model-visible ───────────────

def test_removed_tools_do_not_appear_in_the_stripped_roster_text():
    spec = _spec(tools=CODER_TOOLS, description=CODER_DESCRIPTION,
                 skills=["code-conventions", "file-write-review"])
    (out,), _ = _strip_mutating([spec], None, CATALOG)
    roster = "\n".join(_team_roster_preamble([out]))
    for removed in ("apply_diff", "write_file", "run_command", "lightrag_insert"):
        assert removed not in roster, removed
    assert "get_file_content" in roster


def test_bash_family_not_model_visible_for_a_read_only_executor():
    spec = _spec(name="Executor", tools=EXECUTOR_TOOLS,
                 description=EXECUTOR_DESCRIPTION,
                 skills=["bash-sessions", "file-write-review", "verification-discipline"])
    (out,), _ = _strip_mutating([spec], None, CATALOG)
    assert out.tools == ["get_file_content", "check_port"]
    assert "bash-sessions" not in out.skills
    assert "file-write-review" not in out.skills
    assert out.skills == ["verification-discipline"]


# ── 3. role descriptions do not advertise unavailable capabilities ──────────────────────

def test_coder_description_drops_only_the_tool_sentence():
    out = _description_without_unavailable_tool_mentions(
        CODER_DESCRIPTION, kept={"get_file_content"})
    assert out == "Implementation specialist. Write clean, idiomatic code following existing patterns."


def test_description_with_no_tool_mention_is_untouched():
    out = _description_without_unavailable_tool_mentions(EXECUTOR_DESCRIPTION, kept=set())
    assert out == EXECUTOR_DESCRIPTION


def test_description_mixing_a_removed_and_a_kept_tool_survives():
    text = "Read with get_file_content, then apply this via apply_diff."
    out = _description_without_unavailable_tool_mentions(text, kept={"get_file_content"})
    assert out == text


def test_end_to_end_description_filtering_through_strip_mutating():
    spec = _spec(tools=CODER_TOOLS, description=CODER_DESCRIPTION)
    (out,), _ = _strip_mutating([spec], None, CATALOG)
    assert "apply_diff" not in out.description
    assert "write_file" not in out.description
    assert "Implementation specialist" in out.description
    # the caller's own spec is never mutated
    assert spec.description == CODER_DESCRIPTION


def test_empty_or_none_description_is_handled():
    for d in (None, ""):
        spec = _spec(tools=CODER_TOOLS, description=d)
        (out,), _ = _strip_mutating([spec], None, CATALOG)
        assert out.description == d


# ── 4. skills do not advertise unavailable capabilities ─────────────────────────────────

def test_skill_naming_only_unavailable_tools_is_dropped():
    out = _skills_without_unavailable_tool_refs(
        ["bash-sessions", "verification-discipline"], CATALOG, kept={"check_port"})
    assert out == ["verification-discipline"]


def test_skill_catalog_entry_mixing_a_kept_tool_survives():
    catalog = [{"name": "mixed-skill", "description": "Use get_file_content, then apply_diff."}]
    out = _skills_without_unavailable_tool_refs(
        ["mixed-skill"], catalog, kept={"get_file_content"})
    assert out == ["mixed-skill"]


def test_skill_absent_from_the_fetched_catalog_is_left_in_place():
    out = _skills_without_unavailable_tool_refs(
        ["some-skill-not-in-catalog"], CATALOG, kept=set())
    assert out == ["some-skill-not-in-catalog"]


def test_no_catalog_leaves_skills_unchanged_the_early_rebind_case():
    """The EARLY _strip_mutating pass in run_task_stream/async runs before the skill
    catalog is fetched (skill_catalog=None) -- skills must survive that pass untouched,
    to be filtered by the LATER pass once the catalog is known."""
    spec = _spec(name="Executor", tools=EXECUTOR_TOOLS,
                 skills=["bash-sessions", "verification-discipline"])
    (out,), _ = _strip_mutating([spec], None, None)
    assert out.skills == ["bash-sessions", "verification-discipline"]


def test_no_skills_granted_is_a_no_op():
    spec = _spec(tools=CODER_TOOLS, skills=None)
    (out,), _ = _strip_mutating([spec], None, CATALOG)
    assert out.skills is None


def test_unrelated_useful_skill_content_is_not_removed():
    """A skill whose description names no tool at all -- or only kept ones -- is never
    touched, regardless of how many other skills got filtered for this same member."""
    spec = _spec(name="Executor", tools=EXECUTOR_TOOLS,
                 skills=["bash-sessions", "verification-discipline", "code-conventions"])
    (out,), _ = _strip_mutating([spec], None, CATALOG)
    assert "verification-discipline" in out.skills
    assert "code-conventions" in out.skills
    assert "bash-sessions" not in out.skills


# ── 5. normal read/write mode is unchanged ───────────────────────────────────────────────

def test_strip_mutating_is_never_called_outside_read_only_but_direct_calls_still_filter():
    """_strip_mutating itself has no read_only parameter -- callers gate on it. Calling it
    directly always filters (that is its job); the guarantee that NORMAL mode is unaffected
    lives in the call sites (see test 2 above), not in this function refusing to act."""
    spec = _spec(tools=CODER_TOOLS, description=CODER_DESCRIPTION)
    (out,), _ = _strip_mutating([spec], None, CATALOG)
    assert out.tools != spec.tools


def test_a_spec_with_no_tools_list_is_never_touched_on_any_surface():
    spec = _spec(tools=None, description=CODER_DESCRIPTION, skills=["bash-sessions"])
    (out,), _ = _strip_mutating([spec], None, CATALOG)
    assert out.description == CODER_DESCRIPTION
    assert out.skills == ["bash-sessions"]


# ── 6. A3 remains the fallback if a model attempts an unavailable tool anyway ───────────

def test_a3_unavailable_tool_mixin_is_unmodified_by_phase_d():
    """Phase D only touches what is ADVERTISED (swarm/team.py's capability policy);
    A3's runtime containment (swarm/tool_fix.py) is untouched and still catches whatever
    a model attempts regardless of what it was told."""
    from swarm.tool_fix import _UnavailableToolMixin
    assert hasattr(_UnavailableToolMixin, "get_function_calls_to_run")


@pytest.mark.asyncio
async def test_a3_still_contains_a_call_to_a_skill_advertised_tool_if_one_somehow_occurs():
    """Even with Phase D fixing the advertisement, prove the fallback still works for a
    tool this member's surface never included in the first place -- the exact shape T8 hit
    (Executor called bash_session_start despite not holding it)."""
    from agno.models.message import Message
    from agno.tools.function import Function
    from swarm.tool_fix import VLLMToolFix, reset_unavailable_tool_state

    def _known(name):
        def _f() -> str:
            return "ok"
        return Function(name=name, entrypoint=_f)

    reset_unavailable_tool_state()
    m = VLLMToolFix(id="m")
    m._unavailable_owner = "Executor"
    m._on_repeated_unavailable_tool = None
    msgs = []
    call = {"id": "c1", "type": "function",
           "function": {"name": "bash_session_start", "arguments": "{}"}}
    assistant = Message(role="assistant", tool_calls=[call])
    msgs.append(assistant)
    run = m.get_function_calls_to_run(assistant, msgs, {"get_file_content": _known("get_file_content")})
    assert run == []
    reply = [x for x in msgs if x.role == "tool"][0]
    assert reply.tool_call_id == "c1"
    assert reply.content.startswith("TOOL_UNAVAILABLE")
    reset_unavailable_tool_state()


# ── policy wrapper itself ────────────────────────────────────────────────────────────────

def test_apply_capability_policy_is_idempotent():
    spec = _spec(tools=CODER_TOOLS, description=CODER_DESCRIPTION,
                 skills=["code-conventions", "file-write-review"], instructions=[
                     "Use apply_diff() for existing files.", "Answer plainly."])
    once, _ = _strip_mutating([spec], None, CATALOG)
    twice, _ = _strip_mutating(once, None, CATALOG)
    assert once[0].description == twice[0].description
    assert once[0].skills == twice[0].skills
    assert once[0].instructions == twice[0].instructions


def test_unavailable_tool_tokens_ignores_kept_and_non_mutating_words():
    assert _unavailable_tool_tokens(
        "Use apply_diff and get_file_content.", kept={"get_file_content"}) == {"apply_diff"}
    assert _unavailable_tool_tokens("Write clean, idiomatic code.", kept=set()) == set()
