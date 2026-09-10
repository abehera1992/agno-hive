"""ContextPack — Phase 2, Experiment 2.

Written against PRODUCTION SHAPES on purpose. Two fixes in this experiment series were
green in tests and inert in production, both because the test used a convenient shape:
one returned a bare str where production returns agno's ToolResult wrapper, the other
passed agent=None where production passes a real agent. So the injection tests below
drive the real interception hook with a real agent object, and assert on the delegation
`args` the hook actually forwards -- never on an internal flag.

The flag-off case is asserted first and hardest: the control arm of the experiment is
"production, unchanged", and if the pack leaks in with the flag off the whole
comparison is void.
"""
import asyncio

import pytest

from swarm import context_pack
from swarm.team import _make_tool_interception_hook

BE = "API/inventory-service/router/vouchers_api.py"
FE = "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts"

I4_TASK = (
    "Add a stock-reorder-report feature: a `GET /vouchers/reorder-report` endpoint in "
    f"`{BE}` and a matching `getReorderReport` query in `{FE}`. "
    "Follow each file's existing patterns. Do not modify any other file."
)


class _Agent:
    def __init__(self, name):
        self.name = name


class _Team:
    """Only the attributes the hook actually reads."""
    def __init__(self, pack=None, member_results=None):
        self._context_pack = pack
        self._member_results = member_results or {}


def _pack(multi=True):
    targets = [{"path": BE, "lines": 900, "declarations": ["create_voucher:248"],
                "declaration_total": 9}]
    if multi:
        targets.append({"path": FE, "lines": 965,
                        "declarations": ["getStockLevels:655"], "declaration_total": 48})
    return {"targets": targets, "failure_corrections": "", "multi_file": multi}


# ── target extraction ───────────────────────────────────────────────────────────────

def test_extracts_both_targets_from_the_frozen_i4_task():
    """The whole point of sourcing targets from the TASK: the control run's
    coordinator delegated only the backend, so a delegation-derived pack could never
    carry the frontend half."""
    got = context_pack.extract_targets(I4_TASK)
    assert BE in got and FE in got, got


def test_prose_and_code_identifiers_are_not_treated_as_targets():
    noise = ("Follow the existing patterns, e.g. use @router.get and builder.query, "
             "and sa.UniqueConstraint where needed.")
    assert context_pack.extract_targets(noise) == []


def test_bare_basename_is_not_a_target():
    """classify_citation returns 'basename' for models.py -- not resolvable to one
    file, so it must not become a target."""
    assert context_pack.extract_targets("update models.py please") == []


# ── rendering ───────────────────────────────────────────────────────────────────────

def test_render_names_every_target_and_states_that_all_are_required():
    out = context_pack.render(_pack())
    assert BE in out and FE in out
    assert "ALL of the files" in out


def test_render_is_capped_and_never_drops_the_targets():
    big = _pack()
    big["failure_corrections"] = "X" * 5000
    big["targets"][0]["declarations"] = [f"sym{i}:{i}" for i in range(40)]
    out = context_pack.render(big, member_results={"researcher": "Y" * 5000})
    assert len(out) <= context_pack.MAX_PACK_CHARS
    assert BE in out and FE in out, "targets must survive every drop tier"


def test_render_only_includes_findings_that_mention_a_target():
    out = context_pack.render(
        _pack(), member_results={"researcher": f"I read {BE} and found things",
                                 "reviewer": "unrelated commentary about nothing"})
    assert "researcher" in out
    assert "unrelated commentary" not in out


def test_render_carries_no_file_bodies_or_prior_task_text():
    """The three constraints load_success_context earned the hard way."""
    out = context_pack.render(_pack(), member_results={"researcher": "found it"})
    assert "import {" not in out and "def " not in out


# ── injection through the REAL hook, with a REAL agent ──────────────────────────────

def _run(hook, member, team, task="do the thing"):
    seen = {}

    async def fake_delegate(**kwargs):
        seen.update(kwargs)
        return "ok"

    args = {"member_id": member, "task": task}
    asyncio.run(hook("delegate_task_to_member", fake_delegate, args,
                     agent=_Agent("Coordinator"), team=team))
    return args, seen


def test_pack_is_injected_into_a_coder_delegation():
    args, seen = _run(_make_tool_interception_hook(), "coder", _Team(_pack()))
    assert "CONTEXT PACK" in args["task"]
    assert BE in args["task"] and FE in args["task"]
    # The forwarded call must carry it too -- injection happens before the real call.
    assert "CONTEXT PACK" in seen["task"]


def test_coordinator_framing_is_preserved_not_replaced():
    args, _ = _run(_make_tool_interception_hook(), "coder", _Team(_pack()),
                   task="ORIGINAL COORDINATOR TEXT")
    assert args["task"].startswith("ORIGINAL COORDINATOR TEXT")


def test_other_members_are_untouched():
    for member in ("researcher", "reviewer", "executor"):
        args, _ = _run(_make_tool_interception_hook(), member, _Team(_pack()))
        assert "CONTEXT PACK" not in args["task"], member


def test_no_pack_means_no_change():
    """The control arm. team._context_pack is None when the flag is off."""
    args, _ = _run(_make_tool_interception_hook(), "coder", _Team(None),
                   task="UNCHANGED")
    assert args["task"] == "UNCHANGED"


def test_injection_is_not_repeated_on_a_redelegation():
    hook = _make_tool_interception_hook()
    team = _Team(_pack())
    args, _ = _run(hook, "coder", team)
    once = args["task"]
    args2, _ = _run(hook, "coder", team, task=once)
    assert args2["task"].count("── CONTEXT PACK ──") == 1


def test_disabled_by_default():
    assert context_pack.enabled() is False
