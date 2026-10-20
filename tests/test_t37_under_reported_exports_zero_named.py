"""T37 Priority A/B -- `_under_reported_exports` (swarm/team.py, 2026-08-28) is the
project's own existing, EVIDENCE-based (not live-search) mechanism built specifically
for the T13 shape: "the run opened the file, the file declares N sibling exports of
the same hook family, the answer names fewer than N." Its own docstring is explicit
that T13 "has failed in every battery" and that this is the fix: "symbol_index knows
inventoryApi.ts declares [5 real hooks]... so 'the answer names 1 of 5 siblings' is a
fact, not a judgement."

Traced during T37's evidence-lifecycle investigation (Priority A): the function's own
silencing condition is `if not named or len(named) == len(members): continue` --
collapsing two genuinely different cases into one "stay silent" branch:

  (a) the answer never discusses this file's exports at all (named == 0 because the
      topic never came up) -- correctly silent, nothing to compare.
  (b) the answer DISCUSSES the topic (the task asked about it, the file was read) but
      names ZERO of the real identifiers, e.g. "the frontend has 2 hook definitions"
      with no specific hook name anywhere -- T36.1's own T13a-LIVE1 live replay showed
      exactly this shape (0 hooks named by identifier, 'frontend hooks' discussed only
      vaguely) -- and the guard's docstring itself says it should be silent here too,
      which is the conflation this file closes.

Case (a) and case (b) both set `named = []`, so the SAME line silences both -- even
though only (a) is the "nothing to compare" case the guard's own reasoning describes.
The fix distinguishes them using a signal that was already available but unused at
this call site: whether the TASK itself asked about this family's domain (the regex
anchors on `use...Query|Mutation`, i.e. this project's RTK hook convention, so "the
task asks about hooks" is the natural, narrow, non-generic signal -- not a broad
semantic classifier). A task that never asked about hooks at all still gets case (a)'s
silence, unchanged; a task that DID ask about hooks, with the file read and real
exports on record, but zero named in the answer, now correctly fires.
"""
from swarm.team import _under_reported_exports

_REAL_VOUCHER_HOOKS = {
    "useGetVouchersQuery", "useGetVoucherQuery", "useCreateVoucherMutation",
    "usePostVoucherMutation", "useCancelVoucherMutation",
}

_HOOKS_TASK = (
    "Audit the vouchers module: list its endpoints, its database tables, and its "
    "frontend hooks, and identify anything present in the backend with no frontend "
    "counterpart."
)

_READ_STATE = {"exports": {"Client/.../inventoryApi.ts": set(_REAL_VOUCHER_HOOKS)}}


# ── The exact T13a-LIVE1 shape: file read, task asks about hooks, ZERO named ───

def test_zero_named_with_hooks_task_and_file_read_is_now_detected():
    """T36.1's T13a-LIVE1: 'The frontend hooks related to vouchers are... page.tsx
    (contains 2 top-level definitions)' -- discusses the topic, names no real
    identifier. Must fire now that the task asks about hooks and the file (with
    >= _EXPORT_FAMILY_MIN siblings) was actually read."""
    content = ("The frontend has a vouchers module with 2 top-level definitions "
               "related to hooks, but no specific frontend hooks were enumerated.")
    result = _under_reported_exports(content, _READ_STATE, task=_HOOKS_TASK)
    assert result is not None
    path, named, total, missing = result
    assert named == 0
    assert total == 5
    assert len(missing) <= 8


# ── Preserved: a task that never asks about hooks at all stays silent ──────────

def test_zero_named_with_a_task_that_never_asks_about_hooks_stays_silent():
    """The original author's own stated reasoning for silence -- 'a response that
    never discusses exports is untouched' -- still holds for a genuinely
    off-topic task. This must NOT regress: reading a .ts file as a side effect
    of an unrelated task must never manufacture a false export-coverage
    complaint."""
    content = "The vouchers module has 9 backend endpoints and 3 database tables."
    off_topic_task = "List the backend endpoints and database tables for the vouchers module."
    result = _under_reported_exports(content, _READ_STATE, task=off_topic_task)
    assert result is None


def test_zero_named_with_no_task_argument_stays_silent_backward_compatible():
    """Existing call sites or future callers that omit `task` entirely must see
    EXACTLY the pre-existing behavior (silent on zero-named) -- this is an
    additive widening, not a change to the default."""
    content = "No specific hooks are mentioned here."
    result = _under_reported_exports(content, _READ_STATE)
    assert result is None


# ── Preserved: the ORIGINAL, already-working case (named SOME but not all) ─────

def test_partial_naming_still_fires_exactly_as_before():
    content = "The frontend hook is useGetVouchersQuery."
    result = _under_reported_exports(content, _READ_STATE, task=_HOOKS_TASK)
    assert result is not None
    path, named, total, missing = result
    assert named == 1
    assert total == 5


# ── Preserved: naming ALL real exports is still correctly silent ───────────────

def test_naming_all_real_exports_stays_silent():
    content = ("Hooks: useGetVouchersQuery, useGetVoucherQuery, "
               "useCreateVoucherMutation, usePostVoucherMutation, "
               "useCancelVoucherMutation.")
    result = _under_reported_exports(content, _READ_STATE, task=_HOOKS_TASK)
    assert result is None


# ── Preserved: a file never read (no entry in read_state.exports) is untouched ─

def test_file_never_read_is_untouched():
    result = _under_reported_exports(
        "The frontend has no hooks for this module.", {"exports": {}}, task=_HOOKS_TASK)
    assert result is None


# ── Preserved: fewer than the family-size floor is still a coincidence, not a ──
# ── family, regardless of the task                                            ─

def test_below_family_size_floor_stays_silent_even_with_hooks_task():
    read_state = {"exports": {"x.ts": {"useGetWidgetsQuery", "useCreateWidgetMutation"}}}
    content = "No hooks are used here."
    result = _under_reported_exports(content, read_state, task=_HOOKS_TASK)
    assert result is None
