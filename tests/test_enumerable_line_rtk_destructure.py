"""Phase AF, defect 1 -- _ENUMERABLE_LINE_RE / _enumerable_lines must recognize
RTK Query's standard destructured hook re-export:

    export const {
      useGetFooQuery,
      useBarMutation,
    } = api;

Phase AE found this shape unrecognized (root-caused directly against the real
businessApi.ts and inventoryApi.ts source, both of which use exactly this
export convention). `export\\s+(?:const|function|default)\\s+\\w+` requires a
bare identifier right after "const" -- "{" is not one -- so the destructured
block's opening line never matched, and none of the individual hook-name
lines inside it matched any other alternative either. The only line ever
captured from such a file was the unrelated `export const api = createApi({`
declaration, which fails _candidate_has_route_shape's own hook-pattern check
(it names no `endpoint:`/`useXQuery`/`useXMutation` construct itself) -- so
the frontend side of the comparison ledger was rejected every time, and
_computed_comparison's deterministic TOTALS-based backstop silently never ran
for T2/T13a's unnamed-file phrasing. (T13b's task explicitly NAMES the
frontend file, which resolves via the "named" fast path in
_pick_within_side and never reaches this shape check at all -- confirmed
unaffected below.)

The fix adds one narrow alternative: a bare `use<Name>Query`/`use<Name>Mutation`
identifier, alone on its line, trailing comma optional (covers both a middle
and the last, comma-less destructured entry). It must NOT match a real
invocation line ("const { data } = useGetFooQuery();"), which always carries
trailing "();" after the identifier that the end-anchor rejects.
"""
from swarm.team import (
    _candidate_has_route_shape,
    _computed_comparison,
    _enumerable_lines,
)

# The exact real shape at businessApi.ts:195-212 / inventoryApi.ts:915-... .
REAL_DESTRUCTURED_EXPORT = '''export const businessApi = createApi({
  reducerPath: "businessApi",
  baseQuery: ekamBaseQuery,
  endpoints: (builder) => ({
    getBusinessStatus: builder.query<BusinessStatusResponse, void>({
      query: () => ({ endpoint: "/api/businessservice/business/status" }),
    }),
  }),
});

export const {
  useGetEmailCredentialsQuery,
  useGetBusinessStatusQuery,
  useGetOndcConfigQuery,
  useGetMyBusinessesQuery,
  useVerifyAdminBusinessMutation,
  useAcknowledgeGstRateChangeMutation,
} = businessApi;
'''


# ── 1. Positive RTK destructured export extraction --------------------------

def test_destructured_hook_names_are_captured():
    lines = _enumerable_lines(REAL_DESTRUCTURED_EXPORT)
    for hook in (
        "useGetEmailCredentialsQuery,",
        "useGetBusinessStatusQuery,",
        "useGetOndcConfigQuery,",
        "useGetMyBusinessesQuery,",
        "useVerifyAdminBusinessMutation,",
    ):
        assert hook in lines


def test_last_entry_without_trailing_comma_is_captured():
    """The final destructured item, with no trailing comma before '}'."""
    lines = _enumerable_lines(REAL_DESTRUCTURED_EXPORT)
    assert "useAcknowledgeGstRateChangeMutation," in lines or \
        "useAcknowledgeGstRateChangeMutation" in lines


def test_bare_hook_line_with_no_trailing_comma_matches():
    src = "export const {\n  useGetFooQuery\n} = api;\n"
    assert "useGetFooQuery" in _enumerable_lines(src)


# ── 2. Multiline destructured export -----------------------------------------

def test_multiline_destructure_all_hooks_present_in_order_of_appearance():
    src = (
        "export const {\n"
        "  useFooQuery,\n"
        "  useBarMutation,\n"
        "  useBazQuery,\n"
        "} = api;\n"
    )
    lines = _enumerable_lines(src)
    assert lines == ["useFooQuery,", "useBarMutation,", "useBazQuery,"]


def test_cat_n_numbered_multiline_destructure():
    """get_file_content's real cat -n prefix, across several numbered lines."""
    src = (
        "   195\texport const {\n"
        "   196\t  useGetVouchersQuery,\n"
        "   197\t  usePostVoucherMutation,\n"
        "   198\t} = inventoryApi;\n"
    )
    lines = _enumerable_lines(src)
    assert "useGetVouchersQuery," in lines
    assert "usePostVoucherMutation," in lines


# ── 3. Existing normal export regression -------------------------------------

def test_normal_export_const_name_still_matches():
    assert "export const businessApi = createApi({" in \
        _enumerable_lines(REAL_DESTRUCTURED_EXPORT)


def test_normal_export_function_and_default_still_match():
    src = (
        "export function helper() {}\n"
        "export default function Page() {}\n"
    )
    lines = _enumerable_lines(src)
    assert "export function helper() {}" in lines
    assert "export default function Page() {}" in lines


def test_no_false_positive_on_real_hook_invocation_line():
    """A component USING a hook (not exporting it) must not be miscounted as
    a destructured re-export -- it always carries a trailing call."""
    src = "  const { data } = useGetFooQuery();\n"
    assert _enumerable_lines(src) == []


def test_no_false_positive_on_hook_name_inside_prose():
    src = "The hook useGetFooQuery is used throughout the dashboard.\n"
    assert _enumerable_lines(src) == []


def test_deduplication_unaffected_python_declarations_still_match():
    src = (
        "@router.get(\"/vouchers\")\n"
        "async def list_vouchers(\n"
        "class VoucherCreate(BaseModel):\n"
    )
    lines = _enumerable_lines(src)
    assert lines == [
        '@router.get("/vouchers")',
        "async def list_vouchers(",
        "class VoucherCreate(BaseModel):",
    ]


# ── Route-shape filtering + comparison backstop, end-to-end ------------------

def test_candidate_now_passes_shape_check_with_real_captured_lines():
    """The exact downstream consequence: _candidate_has_route_shape now
    accepts a ledger entry built from REAL extraction (not a hand-typed
    fixture -- test_target_attribution.py already proved the check itself
    is correct given the right lines; this proves extraction now PRODUCES
    them)."""
    entry = {
        "path": "Client/.../business/businessApi.ts",
        "lines": _enumerable_lines(REAL_DESTRUCTURED_EXPORT),
        "count": len(_enumerable_lines(REAL_DESTRUCTURED_EXPORT)),
    }
    assert _candidate_has_route_shape(entry)


def test_the_exact_t2_live_failure_no_longer_rejects_the_frontend_side():
    """Phase AE's own T2 finding, reproduced end-to-end: a ledger holding the
    backend file (already shape-verified) and a REAL-extraction frontend
    entry must now resolve a comparison pair instead of rejecting the
    frontend side with 'no shape-safe candidate'."""
    import asyncio
    from types import SimpleNamespace

    class _FakeSession:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, args):
            self.calls.append((args["left_path"], args["right_path"]))
            text = (f"compare_enumerations — {args['left_path']}  vs  "
                    f"{args['right_path']}\n"
                    "TOTALS: left 13, right 16, matched 7, left-only 6, right-only 9.")
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    class _FakeMCPTools:
        def __init__(self, session):
            self.session = session

        async def get_session_for_run(self, **kwargs):
            return self.session

    backend_lines = ['@router.post("/register")', "async def register(",
                     '@router.get("/status")', "async def get_status("]
    enumerations = {
        "backend": {
            "path": "API/business-service/router/business_api.py",
            "lines": backend_lines, "count": len(backend_lines),
        },
        "frontend": {
            "path": "Client/.../business/businessApi.ts",
            "lines": _enumerable_lines(REAL_DESTRUCTURED_EXPORT),
            "count": len(_enumerable_lines(REAL_DESTRUCTURED_EXPORT)),
        },
    }
    task = ("List every endpoint defined in "
            "API/business-service/router/business_api.py, then list every "
            "RTK Query hook exported by the frontend's business API slice, "
            "and state which endpoints have no corresponding hook. "
            "Enumerate both sides in full before comparing.")
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    out = asyncio.run(_computed_comparison(task, enumerations, "http://x/mcp", tools))
    assert session.calls == [
        ("API/business-service/router/business_api.py", "Client/.../business/businessApi.ts")
    ]
    assert "THE COMPARISON, COMPUTED" in out
