"""Author the Axis D (guard) eval cases by hand, and carry a self-test in each.

WHY THIS EXISTS — the auto-extractor in `generate_cases.py` derived each case's
required/forbidden tokens as a set difference of code tokens between the guard's WRONG
and CORRECT blocks. That measures whether the model reproduced the reference
implementation's incidental vocabulary, not whether it followed the rule. Concretely,
in the 14 cases it produced:

  * D-guard30 / D-guard32 had an EMPTY rule ("Project rule:\n\n\nWrite code...") because
    those guards put their prose AFTER the code fence and the extractor only read before
    it. Unpassable by any model.
  * D-guard32 required 'False)' while forbidding 'False' — every string satisfying the
    requirement trips the prohibition. Logically unsatisfiable.
  * D-guard2 required the token `old_used`, a local variable name from the reference
    snippet that the prompt never states. D-guard10 required `db.add(invite)`; D-guard24
    required a 60-char literal naming a variable `stmt`.
  * Several rules were cut mid-sentence by a hard [:400] slice.

So Axis D's 21.4% was largely a property of the suite. These cases are hand-authored
instead: the rule is stated in full, every required token appears in the prompt the model
is given, and rules that are STRUCTURAL (ordering, positional-vs-keyword, kwarg presence)
use the AST checkers in harness.py rather than substring matching. `score_structural` had
been written for exactly this and was never wired to a single case.

Each case carries `selftest_pass` / `selftest_fail` — the guard's own CORRECT and WRONG
blocks. `validate_cases.py` asserts pass==1.0 and fail==0.0, so a case that cannot be
passed (or cannot be failed) is caught mechanically instead of showing up as a model score.

Guards 10, 12, 14, 16 are deliberately NOT cases: they are rules about apply_diff call
hygiene and about stopping after a failure, not about the shape of emitted code. Neither
substring nor AST scoring expresses them, and a case whose pass condition is not checkable
produces a confident-looking number with nothing behind it.

Run:  python author_guard_cases.py
"""
from __future__ import annotations

import io
import json
from pathlib import Path

CASES = Path(__file__).parent / "cases"

PREAMBLE = "Project rule:\n"

# The language has to be stated. GUARD 36's rule ("flush the pending changes, run the
# check, roll back and raise, then commit") reads perfectly well as raw SQL, and the model
# answered it in SQL -- a correct implementation of the stated rule that the Python AST
# checker scored 0.0. Naming the language does not leak the answer; leaving it out
# measures a guess about the question rather than adherence to the rule.
SUFFIX = "\n\nWrite {lang} code that follows this rule. Output only code."


def case(num, rule, *, required=None, forbidden=None, structural=None,
         selftest_pass="", selftest_fail="", lang="python"):
    scorers = ["structural"] if structural else ["guard"]
    # A composite passes a name starting with "-" instead of a guard number: it holds
    # several even-numbered rules at once rather than restating any single guard.
    composite = isinstance(num, str) and num.startswith("-")
    d = {
        "id": f"D{num}" if composite else f"D-guard{num}",
        "kind": "guard",
        "origin": "hand",
        "provenance": (
            (f"COMPOSITE of several EVAL-ONLY guards from "
             f"ekam-code-generation-guards.md - holds them simultaneously, which is the "
             f"condition real generated code has to meet and where adherence actually "
             f"breaks down. Added 2026-08-31 to restore discriminating power after the "
             f"Axis D repair took the single-rule cases to 100%.")
            if composite else
            (f"ekam-code-generation-guards.md GUARD {num} - EVAL-ONLY "
             f"(even-numbered guards are excluded from training via "
             f"patterns_md exclude_guards). Hand-authored; see "
             f"author_guard_cases.py for why the auto-extracted version "
             f"was replaced.")),
        "scorers": scorers,
        "prompt": PREAMBLE + rule.strip() + SUFFIX.format(lang=lang),
        "lang": lang,
    }
    if structural:
        d["structural"] = structural
    else:
        d["guard_required"] = required or []
        d["guard_forbidden"] = forbidden or []
    d["selftest_pass"] = selftest_pass.strip("\n")
    d["selftest_fail"] = selftest_fail.strip("\n")
    return d


# ── structural cases (Python; the rule is a shape, not a token) ───────────────

G2 = case(
    2,
    "When building a diff/changelog, always snapshot the current value into a local\n"
    "variable BEFORE you change it. Reading the attribute back off the object after\n"
    "mutation gives you the new value, so the recorded delta comes out as zero.\n"
    "Use any names you like -- what matters is that the read happens before the write.",
    structural=[{"type": "assign_before_mutate"}],
    selftest_pass="""
old_used = quota.used_bytes
quota.used_bytes = actual
diffs.append(Diff(old_used_bytes=old_used, new_used_bytes=actual,
                  delta_bytes=actual - old_used))
""",
    selftest_fail="""
quota.used_bytes = actual
diffs.append(Diff(old_used_bytes=quota.used_bytes, new_used_bytes=actual,
                  delta_bytes=actual - quota.used_bytes))
""",
)

G20 = case(
    20,
    "A Pydantic request schema often carries fields that are not ORM columns (used only\n"
    "for validation, never persisted). Before unpacking `model_dump()` into a SQLAlchemy\n"
    "model constructor you MUST drop those fields, by passing `exclude={...}` to\n"
    "`model_dump()`. SQLAlchemy's declarative __init__ raises\n"
    "`TypeError: invalid keyword argument` for any kwarg that is not a mapped column.",
    structural=[{"type": "kwarg_present", "call": "model_dump", "kwargs": ["exclude"]}],
    selftest_pass="""
new_item = Item(
    tenant_id=tenant_id,
    **item.model_dump(exclude_unset=True, exclude={"override_reason", "attested"}),
)
""",
    selftest_fail="""
new_item = Item(tenant_id=tenant_id, **item.model_dump(exclude_unset=True))
""",
)

G28 = case(
    28,
    "`tenant_id` and `business_id` are separate identifiers in this schema -- one tenant\n"
    "account can own several businesses. A `tenant_id` column must therefore NEVER carry a\n"
    "ForeignKey to any business table. Declare it as a plain UUID column, not null.\n"
    "Tenant identity is a cross-service reference enforced at the application layer.",
    structural=[{"type": "absent_call", "call": "ForeignKey"}],
    selftest_pass="""
sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False)
""",
    selftest_fail="""
sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
          sa.ForeignKey("business.business_profiles.business_id"), nullable=False)
""",
)

G30 = case(
    30,
    "In Alembic, `op.create_foreign_key(...)` does not accept a bare `schema=` kwarg -- it is\n"
    "silently absorbed into **dialect_kw, so the FK targets the wrong schema or fails when the\n"
    "migration runs. Pass `source_schema=` for the table being altered and `referent_schema=`\n"
    "for the table being referenced. Both are required when the two tables live outside public.",
    structural=[{"type": "kwarg_present", "call": "create_foreign_key",
                 "kwargs": ["source_schema", "referent_schema"]}],
    selftest_pass="""
op.create_foreign_key(
    "fk_name", "source_table", "referent_table", ["col"], ["ref_col"],
    source_schema="business", referent_schema="business",
)
""",
    selftest_fail="""
op.create_foreign_key(
    "fk_name", "source_table", "referent_table", ["col"], ["ref_col"],
    schema="business",
)
""",
)

G32 = case(
    32,
    "`ForeignKey(...)` is a POSITIONAL argument to `Column(...)`, like any Column construct\n"
    "(`CheckConstraint`, `Sequence`). `nullable=`, `default=`, `primary_key=`,\n"
    "`server_default=` are keywords. Python requires every positional argument to come\n"
    "before the first keyword argument, so a ForeignKey placed after `nullable=` is a\n"
    "SyntaxError that stops the whole models file from importing.",
    structural=[{"type": "positional_before_keyword", "call": "Column",
                 "token": "ForeignKey"}],
    selftest_pass="""
item_id = Column(UUID(as_uuid=True), ForeignKey("inventory.items.item_id"), nullable=False)
""",
    # This WRONG form is a genuine SyntaxError -- which is the rule. score_structural
    # returns 0.0 on a parse failure, so the self-test still holds.
    selftest_fail="""
item_id = Column(UUID(as_uuid=True), nullable=False, ForeignKey("inventory.items.item_id"))
""",
)

G36 = case(
    36,
    "In a SQLAlchemy async backfill script, when the task says to verify something and abort\n"
    "instead of committing, the check must run BEFORE the data is made permanent. Call\n"
    "`db.flush()` on the AsyncSession so the verification query can see the pending changes,\n"
    "run the check, `db.rollback()` and raise if it fails, and only `db.commit()` once the\n"
    "check has passed. A commit followed by a check has nothing left to prevent.",
    structural=[{"type": "call_order", "first": "flush", "second": "commit"}],
    selftest_pass="""
item.sku = new_sku
await db.flush()
duplicates = await check_for_duplicates(db)
if duplicates:
    await db.rollback()
    raise ValueError("found dupes")
await db.commit()
""",
    selftest_fail="""
item.sku = new_sku
await db.commit()
duplicates = await check_for_duplicates(db)
if duplicates:
    raise ValueError("found dupes")
""",
)

# ── token cases (the rule genuinely IS an API name, and the prompt states it) ──

G4 = case(
    4,
    "This project is SQLAlchemy 2.x async throughout. Every DB dependency is declared as\n"
    "`AsyncSession = Depends(get_async_db)` and queries are awaited via `db.execute(select(...))`.\n"
    "A synchronous `Session` with `Depends(get_db)` and `db.query(...)` blocks the event loop\n"
    "and deadlocks uvicorn. Write the endpoint's dependency and query in the async form.",
    required=["AsyncSession", "get_async_db"],
    forbidden=["Depends(get_db)", "db.query("],
    selftest_pass="""
from sqlalchemy.ext.asyncio import AsyncSession

async def my_endpoint(db: AsyncSession = Depends(get_async_db)):
    result = await db.execute(select(Model))
    rows = result.scalars().all()
""",
    selftest_fail="""
from sqlalchemy.orm import Session

def my_endpoint(db: Session = Depends(get_db)):
    rows = db.query(Model).all()
""",
)

G6 = case(
    6,
    "Use the exact class name defined in `schemas.py` when adding a schema import to a router.\n"
    "Do not guess or paraphrase it. The class in this project's `schemas.py` is spelled\n"
    "`SeaweedFSCapacity` -- note the `FS`. Write the import statement for it.",
    required=["SeaweedFSCapacity"],
    forbidden=["SeaweedCapacity"],
    selftest_pass="""
from schemas import SeaweedFSCapacity
""",
    selftest_fail="""
from schemas import SeaweedCapacity
""",
)

G18 = case(
    18,
    "SQLAlchemy 2.x's `AsyncSession.execute()` does not accept a bare Python string -- it\n"
    "raises `ObjectNotExecutableError`. Any hand-written SQL that is not built via\n"
    "`select()`/`update()` must be wrapped in `text()`, imported from sqlalchemy, before\n"
    "being passed to `db.execute()`.",
    required=["text("],
    forbidden=[],
    selftest_pass="""
from sqlalchemy import text
result = await db.execute(
    text("SELECT task_id FROM inventory.gst_compliance_tasks WHERE tenant_id = :tid"),
    {"tid": tenant_id},
)
""",
    selftest_fail="""
result = await db.execute(
    "SELECT task_id FROM inventory.gst_compliance_tasks WHERE tenant_id = :tid",
    {"tid": tenant_id},
)
""",
)

G24 = case(
    24,
    "In a paginated list endpoint that accepts optional filters, the COUNT query must apply\n"
    "the SAME filters as the list query. Build the filtered `select()` statement once, then\n"
    "derive the count from that same statement with `select_from(...)` over its `subquery()`,\n"
    "BEFORE appending `.order_by()/.offset()/.limit()`. Do not issue a separate unfiltered\n"
    "`func.count(Model.id)` query -- the returned total is wrong whenever a filter is used.",
    required=["select_from", "subquery()"],
    forbidden=["func.count(Model.id)"],
    selftest_pass="""
stmt = select(Model)
if status:
    stmt = stmt.where(Model.status == status)
count_result = await db.execute(select(func.count()).select_from(stmt.subquery()))
total = count_result.scalar_one()
stmt = stmt.order_by(Model.created_at).offset(offset).limit(limit)
""",
    selftest_fail="""
stmt = select(Model)
if status:
    stmt = stmt.where(Model.status == status)
count_result = await db.execute(select(func.count(Model.id)))
total = count_result.scalar_one()
stmt = stmt.order_by(Model.created_at).offset(offset).limit(limit)
""",
)

# ── token cases, TypeScript (AST checkers are Python-only, so these stay token) ─

G8 = case(
    8,
    "React hooks are not globally available. Any hook a component uses -- `useState`,\n"
    "`useEffect`, `useCallback` -- must be destructured in the import at the top of the file,\n"
    "e.g. `import { useState } from \"react\"`. A default React import alone leaves the hook\n"
    "undefined at runtime. Write a small component that holds a string in state.",
    required=["{ useState }"],
    forbidden=[],
    lang="typescript",
    selftest_pass="""
import { useState } from "react";

const MyComponent = () => {
  const [value, setValue] = useState("");
  return <input value={value} onChange={(e) => setValue(e.target.value)} />;
};
""",
    selftest_fail="""
import React from "react";

const MyComponent = () => {
  const [value, setValue] = useState("");
  return <input value={value} onChange={(e) => setValue(e.target.value)} />;
};
""",
)

G26 = case(
    26,
    "In a React handler that mutates then refetches, `await` the refetch completely before the\n"
    "`finally` block flips the loading/disabled state back off. A fire-and-forget\n"
    "`fetch(...).then(...)` lets the button re-enable and the UI claim it is done before the\n"
    "displayed data has refreshed, so the user can double-click into a stale state.\n"
    "Write the refetch as `await fetch(...)`, then await reading its json, then set state.",
    required=["await fetch("],
    forbidden=[").then("],
    lang="typescript",
    selftest_pass="""
const handleUpdateAll = async () => {
  setUpdating(true);
  try {
    const res = await fetch(POST_URL, { method: "POST" });
    if (res.ok) {
      const refreshed = await fetch(GET_URL);
      setItems(refreshed.ok ? await refreshed.json() : []);
    }
  } finally {
    setUpdating(false);
  }
};
""",
    selftest_fail="""
const handleUpdateAll = async () => {
  setUpdating(true);
  try {
    const res = await fetch(POST_URL, { method: "POST" });
    if (res.ok) {
      fetch(GET_URL).then(r => r.json()).then(setItems);
    }
  } finally {
    setUpdating(false);
  }
};
""",
)

# ── recovered by the structural checkers added 2026-08-31 ────────────────────
# These three were dropped as "unscoreable" when the only instrument was substring
# matching. Each expresses a RELATIONSHIP (one argument inside another, an argument
# spanning lines, a suffix on one specific call's own path) that substring matching cannot
# see but the AST can. Recovering them is also the honest way to make Axis D discriminate
# again after the repair took it to 100%: harder cases, not more easy ones.

G10 = case(
    10,
    "An `apply_diff` call's `old_string` must appear EXACTLY ONCE in the target file. A\n"
    "short anchor like `await db.commit()` or `return result` occurs in several functions,\n"
    "so the call either fails or patches the wrong place. Include enough surrounding\n"
    "context to be unique: the anchor should span several consecutive lines of the file,\n"
    "not a single statement. Write one `apply_diff(path, old_string=..., new_string=...)`\n"
    "call that adds an audit-log write after an existing commit.",
    structural=[{"type": "kwarg_multiline", "call": "apply_diff", "kwarg": "old_string"}],
    selftest_pass="""
apply_diff(
    path,
    old_string="    db.add(invite)\\n    await db.commit()\\n    await db.refresh(invite)",
    new_string="    db.add(invite)\\n    await db.commit()\\n    await db.refresh(invite)\\n    db.add(AuditLog())\\n    await db.commit()",
)
""",
    selftest_fail="""
apply_diff(path, old_string="await db.commit()", new_string="await db.commit()\\n    db.add(AuditLog())")
""",
)

G14 = case(
    14,
    "The path argument to `apply_diff()` must always be the ORIGINAL file path, e.g.\n"
    "`src/components/MyComponent.tsx`. Never pass the `.hive_proposed` path: the MCP server\n"
    "already reads from the staged file when one exists, and targeting it directly creates\n"
    "a `.hive_proposed.hive_proposed`. Reading the staged file first is fine and expected.\n"
    "Write the read of the staged file followed by the `apply_diff` call.",
    structural=[{"type": "arg_not_suffix", "call": "apply_diff",
                 "suffix": ".hive_proposed", "kwarg": "relative_path"}],
    selftest_pass="""
get_file_content("src/components/MyComponent.tsx.hive_proposed")
apply_diff("src/components/MyComponent.tsx", old_string="a", new_string="b")
""",
    selftest_fail="""
get_file_content("src/components/MyComponent.tsx.hive_proposed")
apply_diff("src/components/MyComponent.tsx.hive_proposed", old_string="a", new_string="b")
""",
)

G16 = case(
    16,
    "To INSERT a line after an existing one with `apply_diff` (rather than replace it), the\n"
    "anchor line must be present identically in BOTH `old_string` and `new_string`, with the\n"
    "new content following it inside `new_string`. Omit the anchor from `new_string` and the\n"
    "anchor is replaced instead of inserted after. Write one `apply_diff(path,\n"
    "old_string=..., new_string=...)` call that adds a second hook line after an existing\n"
    "hook line.",
    structural=[{"type": "kwarg_substring", "call": "apply_diff",
                 "inner": "old_string", "outer": "new_string"}],
    selftest_pass="""
apply_diff(
    path,
    old_string="  const [verify, { isLoading }] = useVerifyAdminSellerMutation();",
    new_string="  const [verify, { isLoading }] = useVerifyAdminSellerMutation();\\n  const [adminUpdate] = useAdminUpdateUserStatusMutation();",
)
""",
    selftest_fail="""
apply_diff(
    path,
    old_string="  const [verify, { isLoading }] = useVerifyAdminSellerMutation();",
    new_string="  const [adminUpdate] = useAdminUpdateUserStatusMutation();",
)
""",
)

# ── composite cases: several even-numbered rules held at once ────────────────
# A single-rule case is passed by recalling one convention. Real generated code has to
# satisfy several simultaneously, and that is where adherence actually breaks down.
#
# Sized as a LADDER by constraint count (3 / 4 / 5+), because the interesting question is
# not "does multi-constraint adherence degrade" but WHERE it starts. The first version of
# this section had n=2 and read 65%; six ad-hoc probes at varying widths then read 87.8%,
# with both failures at 5 constraints. n=2 could not tell those apart, and a training-data
# decision taken on it would have repeated the 3/14-vs-4/14 mistake that made the last two
# promotion gates unsound.

# Rule fragments, each lifted from one EVAL-ONLY (even-numbered) guard. Keyed so a
# composite is declared as a list of keys and the prompt is assembled from them, which
# keeps every composite's wording identical to its single-rule counterpart.
_RULES = {
    "async": ("The DB dependency is `AsyncSession = Depends(get_async_db)` and queries are "
              "awaited via `db.execute(select(...))`. A synchronous `Session` with "
              "`Depends(get_db)` and `db.query(...)` blocks the event loop and deadlocks "
              "uvicorn.",
              ["AsyncSession", "get_async_db"], ["Depends(get_db)", "db.query("]),
    "text": ("Any hand-written SQL string passed to `db.execute()` is wrapped in `text()`; "
             "SQLAlchemy 2.x raises ObjectNotExecutableError on a bare string.",
             ["text("], []),
    "count": ("A paginated COUNT applies the same filters as the list query: derive it from "
              "the filtered statement with `select_from(...)` over its `subquery()`, before "
              "appending `.order_by()/.offset()/.limit()`. Never a separate "
              "`func.count(Model.id)`.",
              ["select_from", "subquery()"], ["func.count(Model.id)"]),
    "dump": ("Before unpacking `model_dump()` into a SQLAlchemy model constructor, pass "
             "`exclude={...}` to drop fields that are not mapped columns; the declarative "
             "`__init__` raises TypeError for any kwarg that is not a column.",
             ["exclude="], []),
    "name": ("Import the schema class by its exact name as defined in `schemas.py`. In this "
             "project that class is spelled `SeaweedFSCapacity` -- note the `FS`. Do not "
             "guess or paraphrase it.",
             ["SeaweedFSCapacity"], ["SeaweedCapacity"]),
}

# Each composite: (suffix, [rule keys], what to write).
_COMPOSITES = [
    ("async-text", ["async", "text"], "an async endpoint that runs one hand-written SQL statement"),
    ("async-dump", ["async", "dump"], "an async create endpoint that builds an ORM row from a Pydantic schema"),
    ("text-count", ["text", "count"], "a paginated list endpoint that also runs one hand-written SQL statement"),
    ("dump-count", ["dump", "count"], "a paginated list endpoint plus the create handler beside it"),
    ("async-name", ["async", "name"], "an async endpoint returning the storage capacity schema"),
    ("async-text-dump", ["async", "text", "dump"], "an async create endpoint that also runs one hand-written SQL statement"),
    ("async-text-count", ["async", "text", "count"], "an async paginated list endpoint that also runs one hand-written SQL statement"),
    ("text-dump-count", ["text", "dump", "count"], "a paginated list endpoint plus the create handler beside it"),
    ("async-dump-count", ["async", "dump", "count"], "an async paginated list endpoint plus the create handler beside it"),
    ("async-text-dump-count", ["async", "text", "dump", "count"], "an async paginated list endpoint plus the create handler beside it"),
    ("async-text-count-name", ["async", "text", "count", "name"], "an async paginated list endpoint returning the storage capacity schema"),
    ("all-five", ["async", "text", "dump", "count", "name"], "an async paginated list endpoint plus the create handler beside it, returning the storage capacity schema"),
]


def _composite(suffix, keys, what):
    rules = "\n".join(f"  {i}. {_RULES[k][0]}" for i, k in enumerate(keys, 1))
    req = [t for k in keys for t in _RULES[k][1]]
    forb = [t for k in keys for t in _RULES[k][2]]
    # The self-test here verifies the case's SCORING VOCABULARY, not realism: that the
    # required set is satisfiable, that the forbidden set is detectable, and that no
    # required token contains a forbidden one -- the D-guard32 bug ('False)' required
    # while 'False' was forbidden, so satisfying the rule guaranteed the violation).
    return case(
        f"-composite-{suffix}",
        f"Write {what}, holding ALL of these rules at once:\n{rules}",
        required=req, forbidden=forb,
        selftest_pass="\n".join(f"# {t}" for t in req),
        selftest_fail="\n".join(f"# {t}" for t in forb) or "# nothing",
    )


COMPOSITES = [_composite(*c) for c in _COMPOSITES]

# Two structural composites: the AST checkers hold several shapes at once the same way.
C_MIGRATION = case(
    "-composite-migration",
    "Write one Alembic migration body that holds both of these rules at once:\n"
    "  1. `ForeignKey(...)` is a POSITIONAL argument to `Column(...)` and must come before\n"
    "     any keyword argument such as `nullable=` -- a positional after a keyword is a\n"
    "     SyntaxError that stops the models file importing.\n"
    "  2. `op.create_foreign_key(...)` takes no bare `schema=` kwarg. Pass `source_schema=`\n"
    "     for the table being altered and `referent_schema=` for the one referenced;\n"
    "     a bare `schema=` is absorbed into **dialect_kw and targets the wrong schema.",
    structural=[{"type": "positional_before_keyword", "call": "Column",
                 "token": "ForeignKey"},
                {"type": "kwarg_present", "call": "create_foreign_key",
                 "kwargs": ["source_schema", "referent_schema"]}],
    selftest_pass="""
supplier_id = Column(UUID(as_uuid=True), ForeignKey("inventory.suppliers.id"), nullable=False)

op.create_foreign_key(
    "fk_items_supplier", "items", "suppliers", ["supplier_id"], ["id"],
    source_schema="inventory", referent_schema="inventory",
)
""",
    selftest_fail="""
supplier_id = Column(UUID(as_uuid=True), nullable=False)

op.create_foreign_key(
    "fk_items_supplier", "items", "suppliers", ["supplier_id"], ["id"],
    schema="inventory",
)
""",
)

C_APPLY_DIFF = case(
    "-composite-apply-diff",
    "Write one `apply_diff` call that inserts a new hook line after an existing one,\n"
    "holding all three of these rules at once:\n"
    "  1. The path argument is the ORIGINAL file path, never the `.hive_proposed` one --\n"
    "     the server already reads the staged file, and targeting it makes a doubled suffix.\n"
    "  2. `old_string` must be unique in the target file, so it spans several consecutive\n"
    "     lines rather than one bare statement.\n"
    "  3. Because this is an INSERT, the anchor lines must appear identically in BOTH\n"
    "     `old_string` and `new_string`, with the new line following them in `new_string`.",
    structural=[{"type": "arg_not_suffix", "call": "apply_diff",
                 "suffix": ".hive_proposed", "kwarg": "relative_path"},
                {"type": "kwarg_multiline", "call": "apply_diff", "kwarg": "old_string"},
                {"type": "kwarg_substring", "call": "apply_diff",
                 "inner": "old_string", "outer": "new_string"}],
    selftest_pass='''
apply_diff(
    "src/hooks/useAdmin.ts",
    old_string="  const [verify] = useVerifyAdminSellerMutation();\\n  const [reject] = useRejectAdminSellerMutation();",
    new_string="  const [verify] = useVerifyAdminSellerMutation();\\n  const [reject] = useRejectAdminSellerMutation();\\n  const [update] = useAdminUpdateUserStatusMutation();",
)
''',
    selftest_fail='''
apply_diff(
    "src/hooks/useAdmin.ts.hive_proposed",
    old_string="  const [verify] = useVerifyAdminSellerMutation();",
    new_string="  const [update] = useAdminUpdateUserStatusMutation();",
)
''',
)

ALL = [G2, G4, G6, G8, G10, G14, G16, G18, G20, G24, G26, G28, G30, G32, G36,
       *COMPOSITES, C_MIGRATION, C_APPLY_DIFF]
# Guards deliberately not represented, and why:
DROPPED = {
    12: "behavioural rule about stopping after a tool failure - not a code-shape task",
    22: "no WRONG/CORRECT code pair - process rule about revision-id collision checks",
    34: "no WRONG/CORRECT code pair - process rule about down_revision heads",
    38: "no WRONG/CORRECT code pair - process rule about session chaining",
}


def main() -> None:
    for old in sorted(CASES.glob("D-guard*.json")):
        old.unlink()
    for c in ALL:
        p = CASES / f"{c['id']}.json"
        io.open(p, "w", encoding="utf-8").write(
            json.dumps(c, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(ALL)} Axis D cases to {CASES}")
    n_struct = sum(1 for c in ALL if "structural" in c)
    print(f"  structural (AST): {n_struct}   token: {len(ALL) - n_struct}")
    print(f"  guards dropped as unscoreable: {sorted(DROPPED)}")


if __name__ == "__main__":
    main()
