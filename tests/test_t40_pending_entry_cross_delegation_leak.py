"""T40 Track A (F3) -- the duplicate-delegation gate served WRONG content for a
repeated target.

Live incident (run d6f4d4a1b3cc, session 3deacd12-..., T13a-T39-LIVE1): the
Coordinator re-asked the Researcher about `API/inventory-service/router/
vouchers_api.py` (the backend endpoints question) a 2nd time, and the gate's own
"ALREADY DONE" response served back text that began "The frontend hooks used by
the vouchers module i[s...]" -- content from a COMPLETELY DIFFERENT delegation
(the frontend-hooks question, against a DIFFERENT target file), not the backend
endpoints answer the Coordinator actually asked about again.

Root cause, traced to one exact boundary (swarm/team.py):

  - Dispatch time (~line 16013): `team._pending_log_entry_by_member[member] =
    log[-1]` -- a SINGLE dict slot keyed by MEMBER ONLY (no target, no objective,
    no delegation id). Every new delegate_structured_task call for the same
    member UNCONDITIONALLY OVERWRITES this one slot with the newest log entry,
    regardless of whether an EARLIER dispatch's own result has been tagged yet.

  - Finalize time (`_finalise_member_chunks`, ~line 17960-17964): pops
    WHATEVER is currently sitting in that one slot and tags the completing
    member's text onto it -- not necessarily the entry that dispatch actually
    belongs to, if a later dispatch overwrote the slot first.

  - Read time (the duplicate gate's own `prior` resolution, ~line 15789-15791):
    prefers `entry.get("result")` (the per-entry tag) but falls back to
    `team._member_results.get(member_id)` -- a dict keyed PURELY by member,
    holding only the member's single MOST RECENT completion, with no target
    association at all -- whenever the per-entry tag is missing or wrong.

Net effect: when a member handles more than one delegation in a run (the
normal case), an EARLIER delegation's own result can get tagged onto a LATER
delegation's log entry (or lost entirely), and a later "ALREADY DONE"/STOP
lookup for the EARLIER delegation's own target falls back to whatever the
member most recently finished -- which may be for a totally different target.

Fix: `_pending_log_entry_by_member[member]` becomes a FIFO queue (a list) of
pending entries rather than a single slot -- each dispatch appends; each
finalize pops the OLDEST still-pending entry for that member. A member
processes its own delegations one at a time internally, so completions resolve
in dispatch order even though dispatch itself returns immediately (an async
generator placeholder) -- FIFO matches that invariant exactly, and no entry is
ever tagged with a different dispatch's text.
"""
from types import SimpleNamespace

from swarm.team import _make_duplicate_delegation_gate_hook, _finalise_member_chunks


async def _fake_delegate(**kwargs):
    return f"delegated: {kwargs}"


def _args(target, objective):
    return {
        "member_id": "researcher", "target": target, "objective": objective,
        "evidence_required": "evidence", "completion_criteria": "done",
    }


async def test_interleaved_dispatches_do_not_cross_tag_each_others_result():
    """Reproduces the exact incident shape: two delegations to the same member
    for the SAME target but genuinely different objectives (endpoints vs
    tables -- real wording from the incident), dispatched before either
    finalizes, then finalized in dispatch order. A third ask repeating the
    FIRST delegation's own objective must be served the FIRST delegation's
    own text, not the second's."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    target = "API/inventory-service/router/vouchers_api.py"
    endpoints_objective = "List all endpoints in the vouchers module backend."
    tables_objective = (
        "Identify the database tables used by the vouchers module backend by "
        "searching for table references in the file."
    )

    # Both dispatches happen before either finalizes -- the real incident's own
    # shape (both delegate_structured_task calls return an async generator
    # placeholder immediately; the member's own streamed text materializes later).
    first = await hook("delegate_structured_task", _fake_delegate,
                       _args(target, endpoints_objective), run_context=None, team=team)
    second = await hook("delegate_structured_task", _fake_delegate,
                       _args(target, tables_objective), run_context=None, team=team)
    assert first.startswith("delegated:")
    assert second.startswith("delegated:")

    # Now the member's streamed text for EACH dispatch arrives and finalizes,
    # in dispatch order (the member handles its own delegations one at a time).
    team._member_chunks = {"researcher": "ENDPOINTS TEXT: 9 real routes"}
    _finalise_member_chunks(team, "Researcher")
    team._member_chunks = {"researcher": "TABLES TEXT: 31 real tables"}
    _finalise_member_chunks(team, "Researcher")

    # A third ask repeating the FIRST delegation's own objective (reworded, same
    # shape as the real incident's seq5: "List all the endpoints in the vouchers
    # API.") must be served the ENDPOINTS text, never the tables text.
    third = await hook("delegate_structured_task", _fake_delegate,
                       _args(target, "List all the endpoints in the vouchers API."),
                       run_context=None, team=team)

    assert "ENDPOINTS TEXT" in third, (
        "the repeat ask for ENDPOINTS was served the wrong delegation's text -- "
        f"got: {third!r}"
    )
    assert "TABLES TEXT" not in third, (
        "the endpoints repeat must never be served the tables delegation's text"
    )


async def test_a_repeat_of_the_second_delegation_also_gets_its_own_text():
    """The inverse check -- a repeat of the SECOND delegation must be served
    its OWN text, not the first delegation's, closing the loop on both
    directions of the cross-tagging bug.

    Uses objectives that differ by a genuine identifier-shaped token
    (snake_case/CamelCase, per _objective_names_new_entity's own documented
    scope -- it only recognizes a NEW symbol, never a plain-English subject
    difference like "endpoints" vs "tables", by design). This isolates the
    pending-entry bridge fix under test from that SEPARATE, pre-existing,
    out-of-scope matching limitation: with plain-English-only wording, the
    gate's own entry-selection loop always matches the FIRST entry sharing a
    target (since it cannot tell the two questions apart), regardless of
    which one a later repeat is actually asking about -- a real, separate
    gap this phase does not fix (see the T40 report's own scope note)."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    target = "API/inventory-service/router/vouchers_api.py"
    endpoints_objective = "List the list_vouchers and create_voucher route functions."
    tables_objective = "List the VoucherSeries and StockLedger table columns."

    await hook("delegate_structured_task", _fake_delegate,
              _args(target, endpoints_objective), run_context=None, team=team)
    await hook("delegate_structured_task", _fake_delegate,
              _args(target, tables_objective), run_context=None, team=team)

    team._member_chunks = {"researcher": "ENDPOINTS TEXT: 9 real routes"}
    _finalise_member_chunks(team, "Researcher")
    team._member_chunks = {"researcher": "TABLES TEXT: 31 real tables"}
    _finalise_member_chunks(team, "Researcher")

    repeat_tables = await hook(
        "delegate_structured_task", _fake_delegate,
        _args(target, "List the VoucherSeries and StockLedger columns again."),
        run_context=None, team=team)

    assert "TABLES TEXT" in repeat_tables
    assert "ENDPOINTS TEXT" not in repeat_tables


async def test_three_interleaved_dispatches_each_keep_their_own_text():
    """Extends to three in-flight delegations (matching the real incident's own
    seq1/seq2/seq3 shape: endpoints, tables, then a third target for hooks) --
    the fix must hold for a queue deeper than two.

    Only the first (target_a) entry is repeated here, since a repeat of the
    SECOND target_a entry runs into the separate, out-of-scope entity-matching
    limitation covered explicitly by the dedicated test above -- this test's
    own job is the queue depth, not entry selection among same-target peers."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    target_a = "API/inventory-service/router/vouchers_api.py"
    target_b = "Client/EcommClient-Web/ekamweb/src/app/(portal)/business/inventory/vouchers/page.tsx"

    await hook("delegate_structured_task", _fake_delegate,
              _args(target_a, "List all endpoints in the vouchers module backend."),
              run_context=None, team=team)
    await hook("delegate_structured_task", _fake_delegate,
              _args(target_a, "Identify the database tables used by the vouchers module backend."),
              run_context=None, team=team)
    await hook("delegate_structured_task", _fake_delegate,
              _args(target_b, "Identify the frontend hooks used by the vouchers module."),
              run_context=None, team=team)

    team._member_chunks = {"researcher": "ENDPOINTS TEXT"}
    _finalise_member_chunks(team, "Researcher")
    team._member_chunks = {"researcher": "TABLES TEXT"}
    _finalise_member_chunks(team, "Researcher")
    team._member_chunks = {"researcher": "HOOKS TEXT"}
    _finalise_member_chunks(team, "Researcher")

    repeat_endpoints = await hook(
        "delegate_structured_task", _fake_delegate,
        _args(target_a, "List all the endpoints in the vouchers API."),
        run_context=None, team=team)
    repeat_hooks = await hook(
        "delegate_structured_task", _fake_delegate,
        _args(target_b, "What hooks does the vouchers frontend use?"),
        run_context=None, team=team)

    assert "ENDPOINTS TEXT" in repeat_endpoints and "TABLES TEXT" not in repeat_endpoints \
        and "HOOKS TEXT" not in repeat_endpoints
    assert "HOOKS TEXT" in repeat_hooks and "ENDPOINTS TEXT" not in repeat_hooks \
        and "TABLES TEXT" not in repeat_hooks


async def test_finalizing_with_no_pending_entry_is_a_safe_no_op():
    """A member completion with nothing pending (e.g. a delegation tool other
    than delegate_structured_task/delegate_task_to_member, or a stray
    finalize call) must not crash."""
    team = SimpleNamespace(_member_results={})
    team._member_chunks = {"researcher": "SOME TEXT"}
    _finalise_member_chunks(team, "Researcher")  # must not raise
    assert team._member_results["researcher"] == "SOME TEXT"


async def test_single_dispatch_still_tags_correctly():
    """The common, non-interleaved case (one dispatch, one finalize) must keep
    working exactly as before the fix."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})
    target = "API/inventory-service/models.py"

    await hook("delegate_structured_task", _fake_delegate,
              _args(target, "List the Party model's fields."),
              run_context=None, team=team)
    team._member_chunks = {"researcher": "PARTY FIELDS TEXT"}
    _finalise_member_chunks(team, "Researcher")

    repeat = await hook("delegate_structured_task", _fake_delegate,
                        _args(target, "List all fields on the Party model."),
                        run_context=None, team=team)
    assert "PARTY FIELDS TEXT" in repeat
