"""Regression test: the coordinator must be told to recognize when a question is
already answered and stop delegating, rather than following every related thread it
happens to notice while researching the real target.

Confirmed live 2026-08-11: a read-only research run correctly found and explained the
real component that answered the question -- grounded, accurate, complete, citing the
real openEdit function, real SCSS classes, real RTK Query hooks. It then kept
delegating for 8+ MORE rounds and 40,000+ characters, reading an entirely unrelated
API's (Vouchers) full CRUD implementation that nobody asked about. Nothing in that
extra research was fabricated -- it was simply unnecessary. The default
max_iterations=25 (swarm/team.py's _build_team, config.max_iterations) never came
close to catching this; a real multi-file write pipeline genuinely needs headroom
there (Coordinator -> ContextRouter -> Researcher -> Planner -> Coder -> Executor ->
Reviewer). Two complementary fixes: config.read_only_max_iterations (see
test_team_tool_call_limit.py for the hard, mechanical backstop -- a read-only run
structurally can't need the full pipeline's round budget, since writes are stripped
from every agent's tool surface) and this instruction (the softer, first-line nudge --
recognizing sufficiency BEFORE hitting any hard limit is the better outcome).
"""
from swarm import team


def _joined():
    return "\n".join(team._COORDINATOR_INSTRUCTIONS)


def test_stop_delegating_section_exists():
    text = _joined()
    assert "Stop delegating once the question is answered" in text


def test_section_names_the_concrete_failure_mode():
    text = _joined()
    assert "8+ MORE rounds" in text
    assert "unrelated API" in text


def test_section_gives_the_actual_sufficiency_check():
    # Normalize ALL whitespace (not just newlines) -- the source list wraps this
    # sentence across two list items with leading indentation, so a plain
    # replace("\n", " ") leaves extra spaces at the wrap point.
    normalized = " ".join(_joined().split())
    assert "does what I already have" in normalized
    assert "STOP delegating immediately and" in normalized


def test_section_distinguishes_grounded_from_necessary():
    """The failure mode being fixed is NOT fabrication -- it's important the
    instruction doesn't conflate 'ungrounded' with 'unnecessary', since the extra
    research in the live incident was entirely real, just not asked for."""
    text = _joined()
    assert "Nothing in that extra research was fabricated" in text.replace("\n", " ")


def test_section_mentions_the_mechanical_backstop_without_relying_on_it_alone():
    """The instruction should point at the hard max_iterations backstop as a safety
    net, but frame recognizing sufficiency as the better outcome -- consistent with
    this codebase's own established lesson that instructions alone are not a
    reliable enforcement mechanism."""
    text = _joined()
    joined_no_newlines = text.replace("\n", " ")
    assert "max_iterations" in joined_no_newlines
    assert "better outcome" in joined_no_newlines


def test_stop_delegating_section_precedes_the_clarification_section():
    """Placement matters for visibility -- this sits alongside the other
    high-priority delegation guidance near the top of the instruction block, not
    buried at the end."""
    text = _joined()
    idx_stop_delegating = text.index("Stop delegating once the question is answered")
    idx_clarification = text.index("Asking for clarification")
    assert idx_stop_delegating < idx_clarification
