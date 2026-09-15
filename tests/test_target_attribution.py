"""Phase S -- deterministic evidence target attribution.

Forensics (this phase): re-pulled the real ZGX journal for the T13a wrong-file
comparison Phase R's own battery reports named ("model.py vs page.tsx",
"vouchers_api.py vs page.tsx" -- see docs/guide/deferred-limitations-ledger.md
item #45) and confirmed it directly: `[team] computed the comparison for
API/inventory-service/models.py vs .../vouchers/page.tsx`, repeated across
multiple fresh T13a sessions in the live journal.

Root cause, traced to exactly one place: swarm.team._computed_comparison's own
`_pick_within_side` closure, on its max-count FALLBACK path only (used when
the task never names its files -- T13a's own deliberate shape; T13b, which
always names them, hits the "named" branch and is untouched by anything in
this file). Raw declaration COUNT has no relationship to whether a candidate
is even the right SHAPE for compare_enumerations to extract anything from:
models.py (SQLAlchemy models) and page.tsx (a Next.js page component living
under a path that literally contains "vouchers") both out-count the
genuinely correct files on a bad day, yet neither would ever produce a real
MATCHED/LEFT-ONLY/RIGHT-ONLY line in compare_enumerations' own output.

This is a tool/path-resolution defect (confirmed, not guessed): the
Coordinator never picks these paths itself -- _computed_comparison does,
deterministically, from the run's own already-recorded enumeration ledger.
compare_enumerations itself, once given a real operand pair, has always been
exact (Phase Q's own direct verification of the PUT-vs-"post" case, left
untouched here by construction).

Fix: swarm.team._candidate_has_route_shape, a read-only structural check
against an enumerations[] entry's OWN already-captured `lines` (no new tool
call, no new model turn) for the same constructs compare_enumerations' own
extractor requires (@router.<verb>( for .py, endpoint:/use<Name>Query|Mutation
for .ts) -- mirrored locally, never modifying hive-mcp/tools/compare.py.
Rejects (prefers an alternative, same-side, already-enumerated candidate;
and if none pass, declines the comparison entirely) ONLY on positive evidence
of a shape mismatch -- never merely because shape data (`lines`) is absent,
which is exactly what every PRE-EXISTING test in tests/test_computed_comparison.py
already relies on (its own `_enum()` fixture helper always sets `lines: []`)
and must stay fully unaffected by this phase, confirmed by the unchanged
regression suite.

An explicitly task-NAMED file is NEVER shape-checked -- the task naming it
IS the authoritative signal (Phase S's own objective), and this is what
keeps T13b's behavior byte-for-byte unchanged.
"""
import asyncio
from types import SimpleNamespace

import pytest

from swarm.team import (
    _candidate_has_route_shape,
    _computed_comparison,
)


def _run(coro):
    return asyncio.run(coro)


def _enum(path: str, count: int, lines: list[str] | None = None) -> dict:
    return {"path": path, "tool": "get_file_content", "lines": lines or [], "count": count}


class _FakeSession:
    def __init__(self, text_by_pair=None, default_text=None):
        self.text_by_pair = text_by_pair or {}
        self.default_text = default_text
        self.calls: list[tuple[str, str]] = []

    async def call_tool(self, name, args):
        pair = (args["left_path"], args["right_path"])
        self.calls.append(pair)
        text = self.text_by_pair.get(pair, self.default_text)
        if text is None:
            text = (f"compare_enumerations — {pair[0]}  vs  {pair[1]}\n"
                    f"TOTALS: left 1, right 1, matched 1, left-only 0, right-only 0.")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class _FakeMCPTools:
    def __init__(self, session):
        self.session = session

    async def get_session_for_run(self, **kwargs):
        return self.session


T13A_TASK = (
    "Audit the vouchers module: list its endpoints, its database tables, and "
    "its frontend hooks, and identify anything present in the backend with "
    "no frontend counterpart."
)
T13B_TASK = (
    "Audit the vouchers module: list its endpoints, its database tables, and "
    "its frontend hooks, and identify anything present in the backend with "
    "no frontend counterpart. Read API/inventory-service/router/vouchers_api.py "
    "for the endpoints, API/inventory-service/models.py for the tables, and "
    "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts "
    "for the frontend hooks."
)

# Realistic captured lines -- the actual shape _enumerable_lines would return
# for each file, matching the real journal-confirmed failure.
MODELS_PY_LINES = [
    "class Voucher(Base):",
    "class VoucherSeries(Base):",
    "class VoucherVersion(Base):",
]
VOUCHERS_API_PY_LINES = [
    '@router.get("/vouchers")',
    '@router.get("/vouchers/{voucher_id}")',
    '@router.post("/vouchers")',
    '@router.put("/vouchers/{voucher_id}/post")',
]
PAGE_TSX_LINES = [
    "export default function VouchersPage() {",
    "export const VoucherRow = () => {",
]
INVENTORY_API_TS_LINES = [
    'getVouchers: builder.query<Voucher[], void>({',
    "endpoint: '/api/inventoryservice/vouchers',",
    "useGetVouchersQuery,",
    "usePostVoucherMutation,",
]


# ── 1-6. _candidate_has_route_shape (direct, structural) ---------------------

def test_py_file_with_router_decorator_passes():
    assert _candidate_has_route_shape(
        _enum("API/inventory-service/router/vouchers_api.py", 30, VOUCHERS_API_PY_LINES))


def test_py_file_with_only_class_declarations_fails():
    """The exact live-journal shape: models.py has real declarations (classes)
    but none of them are @router.<verb>(...) decorators."""
    assert not _candidate_has_route_shape(
        _enum("API/inventory-service/models.py", 50, MODELS_PY_LINES))


def test_ts_file_with_endpoint_and_hooks_passes():
    assert _candidate_has_route_shape(
        _enum("Client/.../inventoryApi.ts", 20, INVENTORY_API_TS_LINES))


def test_tsx_file_with_only_component_exports_fails():
    """The exact live-journal shape: page.tsx has real declarations (a
    component, a sub-component) but none of them are RTK Query endpoint/hook
    constructs."""
    assert not _candidate_has_route_shape(
        _enum("Client/.../vouchers/page.tsx", 25, PAGE_TSX_LINES))


def test_empty_lines_is_trusted_not_rejected():
    """No captured lines to judge shape from at all is NOT evidence of a bad
    shape -- this is what keeps every pre-existing test.test_computed_comparison
    fixture (which always sets lines=[]) unaffected by this phase."""
    assert _candidate_has_route_shape(
        _enum("API/business-service/router/business_api.py", 13, []))


def test_non_source_extension_defaults_true():
    assert _candidate_has_route_shape(_enum("README.md", 10, ["# heading"]))


# ── 7-12. _computed_comparison end-to-end (the real reported failure) -------

@pytest.mark.asyncio
async def test_t13a_shape_backend_models_py_loses_to_vouchers_api_py():
    """The exact first live-journal failure: models.py (higher count, no
    route shape) vs vouchers_api.py (lower count, real route shape) -- the
    shape-safe candidate must win regardless of raw count."""
    enumerations = {
        "models": _enum("API/inventory-service/models.py", 50, MODELS_PY_LINES),
        "vouchers_api": _enum(
            "API/inventory-service/router/vouchers_api.py", 9, VOUCHERS_API_PY_LINES),
        "inventory_ts": _enum(
            "Client/.../inventoryApi.ts", 20, INVENTORY_API_TS_LINES),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    await _computed_comparison(T13A_TASK, enumerations, "http://x/mcp", tools)
    assert session.calls == [
        ("API/inventory-service/router/vouchers_api.py", "Client/.../inventoryApi.ts")
    ]


@pytest.mark.asyncio
async def test_t13a_shape_frontend_page_tsx_loses_to_inventory_api_ts():
    """The exact second live-journal failure: page.tsx (higher count, no
    RTK Query shape, path literally containing 'vouchers') vs inventoryApi.ts
    (lower count, real endpoint/hook shape) -- the shape-safe candidate must
    win despite page.tsx's more topically-plausible path."""
    enumerations = {
        "vouchers_api": _enum(
            "API/inventory-service/router/vouchers_api.py", 9, VOUCHERS_API_PY_LINES),
        "page": _enum("Client/.../vouchers/page.tsx", 40, PAGE_TSX_LINES),
        "inventory_ts": _enum(
            "Client/.../inventoryApi.ts", 12, INVENTORY_API_TS_LINES),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    await _computed_comparison(T13A_TASK, enumerations, "http://x/mcp", tools)
    assert session.calls == [
        ("API/inventory-service/router/vouchers_api.py", "Client/.../inventoryApi.ts")
    ]


@pytest.mark.asyncio
async def test_ambiguous_no_shape_safe_candidate_on_either_side_declines():
    """Never guess: when NEITHER candidate on a side passes the shape check,
    the comparison must decline entirely rather than pair two shape-unsafe
    files (or one safe, one unsafe)."""
    enumerations = {
        "models": _enum("API/inventory-service/models.py", 50, MODELS_PY_LINES),
        "page": _enum("Client/.../vouchers/page.tsx", 40, PAGE_TSX_LINES),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    out = await _computed_comparison(T13A_TASK, enumerations, "http://x/mcp", tools,
                                      content="no other file named here")
    assert out == ""
    assert session.calls == []


@pytest.mark.asyncio
async def test_t13b_named_file_never_shape_checked_even_if_it_would_fail():
    """T13b (files always named) must be byte-for-byte unaffected: an
    explicitly task-named file is chosen on that basis alone, never
    second-guessed by the shape check -- confirmed here with a named file
    that would otherwise FAIL the shape check (deliberately given page.tsx-
    shaped lines), proving the named branch truly never reaches the check."""
    enumerations = {
        "vouchers_api": _enum(
            "API/inventory-service/router/vouchers_api.py", 9, VOUCHERS_API_PY_LINES),
        "models": _enum("API/inventory-service/models.py", 50, MODELS_PY_LINES),
        "inventory_ts": _enum(
            "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts",
            12, PAGE_TSX_LINES),  # deliberately shape-FAILING lines
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    await _computed_comparison(T13B_TASK, enumerations, "http://x/mcp", tools)
    assert session.calls == [
        ("API/inventory-service/router/vouchers_api.py",
         "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts")
    ]


@pytest.mark.asyncio
async def test_valid_normal_selection_unaffected_when_both_sides_pass_shape():
    enumerations = {
        "vouchers_api": _enum(
            "API/inventory-service/router/vouchers_api.py", 9, VOUCHERS_API_PY_LINES),
        "inventory_ts": _enum(
            "Client/.../inventoryApi.ts", 20, INVENTORY_API_TS_LINES),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    out = await _computed_comparison(T13A_TASK, enumerations, "http://x/mcp", tools)
    assert session.calls == [
        ("API/inventory-service/router/vouchers_api.py", "Client/.../inventoryApi.ts")
    ]
    assert "THE COMPARISON, COMPUTED" in out


@pytest.mark.asyncio
async def test_shape_fallback_prefers_lower_count_shape_safe_same_side_candidate():
    """Bounded, deterministic reconciliation using only already-enumerated
    candidates (never a new tool call or model turn): when the top-count
    candidate on a side fails shape, a LOWER-count same-side candidate that
    passes is used instead of declining outright."""
    enumerations = {
        "models": _enum("API/inventory-service/models.py", 50, MODELS_PY_LINES),
        "vouchers_api": _enum(
            "API/inventory-service/router/vouchers_api.py", 5, VOUCHERS_API_PY_LINES),
        "schemas": _enum("API/inventory-service/schemas.py", 45,
                          ["class VoucherCreate(BaseModel):", "class VoucherOut(BaseModel):"]),
        "inventory_ts": _enum(
            "Client/.../inventoryApi.ts", 20, INVENTORY_API_TS_LINES),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    await _computed_comparison(T13A_TASK, enumerations, "http://x/mcp", tools)
    # models.py (50) and schemas.py (45) both outrank vouchers_api.py (5) by
    # raw count and both fail the shape check -- vouchers_api.py must still
    # be the one chosen.
    assert session.calls == [
        ("API/inventory-service/router/vouchers_api.py", "Client/.../inventoryApi.ts")
    ]


# ── 13. Never modifies compare_enumerations / hive-mcp ----------------------

def test_source_never_touches_hive_mcp_or_compare_py():
    import inspect
    import swarm.team as team_mod
    src = inspect.getsource(team_mod._candidate_has_route_shape)
    src += inspect.getsource(team_mod._computed_comparison)
    for forbidden in ("hive-mcp/tools/compare.py", "import compare", "from tools import compare"):
        assert forbidden not in src
