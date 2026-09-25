"""Phase R (2026-09-24): controlled delegation intervention.

Tests the smallest attributable change made to test the hypothesis that an
overly open-ended Researcher continuation pattern contributes to the FORM-3
malformed-tool-call failure documented in Q9-Q18. Three changes, each
covered here:

  1. Coordinator instructions gain a bounded delegation contract
     (TARGET/OBJECTIVE/EVIDENCE REQUIRED/COMPLETION CRITERIA) for real
     investigative delegations.
  2. Researcher's own instructions (teams/engineering.yaml) gain an explicit
     evidence-first stop condition and a pseudo-tool-call prohibition.
  3. forward_member_answer's empty-store message becomes an explicit
     terminal-state signal instead of "Delegate first, then forward" (the
     phrasing Q13/Q14 traced to a 56-call retry loop).

These are prompt/config-level changes; correctness of the underlying model
BEHAVIOR is validated by the live run (Phase R section 14), not here. What
IS unit-testable, and tested below, is that the intended text actually
reaches the deployed configuration.
"""
from pathlib import Path

import pytest
import yaml

from swarm.team import _COORDINATOR_INSTRUCTIONS, _make_forward_member_answer

_ENGINEERING_YAML = Path(__file__).resolve().parent.parent / "teams" / "engineering.yaml"


def _researcher_instructions() -> list[str]:
    data = yaml.safe_load(_ENGINEERING_YAML.read_text(encoding="utf-8"))
    researcher = next(a for a in data["agents"] if a["name"] == "Researcher")
    return researcher["instructions"]


# ── Test 1: bounded delegation contract (Coordinator) ────────────────────

def test_coordinator_instructions_contain_bounded_delegation_contract():
    joined = "\n".join(_COORDINATOR_INSTRUCTIONS)
    assert "PHASE R" in joined
    for field in ("TARGET", "OBJECTIVE", "EVIDENCE REQUIRED", "COMPLETION CRITERIA"):
        assert field in joined, f"bounded delegation contract missing {field!r}"
    assert "One delegation = one bounded research objective" in joined


# ── Test 2: evidence-first / bounded-completion rule reaches Researcher ──

def test_researcher_yaml_contains_bounded_completion_rule():
    instructions = _researcher_instructions()
    joined = "\n".join(instructions)
    assert "BOUNDED COMPLETION rule" in joined
    assert "STOP investigating and write your result immediately" in joined


# ── Test 3: pseudo-tool-call prohibition reaches Researcher ──────────────

def test_researcher_yaml_contains_pseudo_tool_call_prohibition():
    instructions = _researcher_instructions()
    joined = "\n".join(instructions)
    assert "NO PSEUDO-TOOL-CALLS rule" in joined
    # The prohibition must name the exact FORM-3/FORM-1 markers this phase
    # is targeting, not just a generic "don't misbehave" statement.
    assert "<function_call>" in joined
    assert "[TOOL_CALLS]" in joined


def test_researcher_instructions_still_yaml_valid_and_well_formed():
    # Guards against a stray quote/escape breaking YAML parsing entirely --
    # a silent config-load failure would be far worse than a failing test.
    instructions = _researcher_instructions()
    assert isinstance(instructions, list)
    assert all(isinstance(line, str) for line in instructions)
    assert len(instructions) >= 30  # sanity: nothing was accidentally truncated


# ── Test 5: missing member result -> explicit terminal failure state ─────

@pytest.mark.asyncio
async def test_missing_member_result_is_a_terminal_state_not_a_retry_invitation():
    tool = _make_forward_member_answer({}, {})
    out = await tool.entrypoint(member_id="researcher")
    assert out.startswith("NO RESULT FOR")
    assert "terminal state" in out
    # The old wording explicitly invited another attempt ("Delegate first,
    # then forward") -- the new wording must not repeat that specific
    # retry-inviting phrase verbatim.
    assert "Delegate first, then forward." not in out
    # Calling it again with no new state must return the byte-identical
    # message -- this IS the "no hidden retry behavior" property Phase R
    # requires (section 8/10): repeated calls are deterministic, not
    # progressively different or silently softened.
    out2 = await tool.entrypoint(member_id="researcher")
    assert out == out2
