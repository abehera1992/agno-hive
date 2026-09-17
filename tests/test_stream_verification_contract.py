"""Phase AB -- characterization of /stream's verification contract.

Non-invasive: these tests assert what the current implementation actually
does, not what it should do. Phase AB found this is an intentional
architectural boundary, not a defect -- /stream's own docstring
(api/server.py::stream_endpoint) documents a "chunk"/"tool_start"/
"tool_end"/"done"/"error" event schema where "done" carries only session
metadata and token counts, never a re-sent, re-verified final answer text.
docs/guide/api.md says the same thing explicitly for a different guard:
"On /stream specifically, the raw fenced block is *not* hidden from the
streamed chunk events... only /run/plan get a fully clean strip."

Do NOT read these tests as asserting streaming SHOULD stay unverified --
only that it currently, demonstrably, does. See Phase AB's own final
report for the classification (Model C) and the explicit decision this
phase deliberately did NOT make (whether to redesign it).
"""
import inspect

import swarm.team as team_mod


def test_run_task_stream_never_calls_verify_claims():
    src = inspect.getsource(team_mod.run_task_stream)
    assert "_verify_claims(" not in src


def test_run_task_stream_never_calls_verified_answer():
    src = inspect.getsource(team_mod.run_task_stream)
    assert "_verified_answer(" not in src


def test_run_task_stream_never_calls_evidence_integrity_check():
    src = inspect.getsource(team_mod.run_task_stream)
    assert "_evidence_integrity_check(" not in src


def test_done_event_never_carries_the_final_answer_text_to_the_client():
    """api/server.py's stream_endpoint builds `done_event` from `chunk` (the
    __done__ sentinel run_task_stream yields) but never includes
    chunk["content"] in it -- only session metadata and token counts. The
    client only ever has the raw chunks it already received live; there is
    no separate, re-verifiable "final answer" delivery."""
    import inspect as _inspect
    from api import server as server_mod
    src = _inspect.getsource(server_mod.stream_endpoint)
    # The __done__ branch reads chunk["content"] only to persist it
    # (append_message) and to derive `clarification` -- never to place it
    # into `done_event`.
    done_branch = src[src.index('chunk.get("__done__")'):]
    done_event_literal = done_branch[
        done_branch.index("done_event = {"):done_branch.index("clarification = chunk")]
    assert '"content"' not in done_event_literal
    assert "chunk[" not in done_event_literal or 'chunk.get("tokens"' in done_event_literal
