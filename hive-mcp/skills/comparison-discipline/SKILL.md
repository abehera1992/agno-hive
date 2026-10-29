---
name: comparison-discipline
description: How to answer "which of these have no matching X" / "what's covered vs not" / gap-analysis questions — enumerate both sides explicitly, then diff item-by-item; never state a coverage conclusion from memory or a plausible-sounding default.
---
Comparison / gap-analysis discipline (MANDATORY when asked "which X have no
matching Y", "what's covered vs not", "what's missing", or any question that
requires comparing two enumerated things and reporting the difference):

Measured 2026-08-03: asked to list backend API endpoints then say which had no
corresponding frontend hook, the swarm correctly enumerated BOTH sides — 7 real
endpoints, 3 real hooks, matching the actual code exactly — then its own summary
line claimed a DELETE endpoint was "✅ fully covered", contradicting the hook list
it had just written one paragraph above, which contained no delete hook at all.
Both lists were fully grounded; the coverage CONCLUSION drawn from them was not.
verify_claims did not catch this — it only checks whether a cited symbol exists,
never whether a stated conclusion follows from evidence the answer already gave.

Confirmed again, T13a (2026-10-25): asked to compare backend vouchers endpoints
against frontend hooks, the swarm enumerated side B from the vouchers PAGE
component alone (2 hooks imported there) and never looked at the API-slice file
that actually DEFINES the hooks (5 real hooks, 3 of them unused by any page).
The page is a CONSUMER of side B, not its source of truth — a consumer only
shows what one caller happens to use, never the full inventory. The same shape
applies beyond hooks: a route handler that calls a service function is a
consumer of that service, not its definition; a migration that creates a column
is evidence of a table, but the model class is the table's own definition.

When answering this class of question:
1. Enumerate side A completely first (e.g. every backend endpoint), one per line.
2. Before enumerating side B, locate its DEFINING source, not just the first
   place it is used. A frontend hook is DEFINED in the API-slice/service file
   that creates it (e.g. a `createApi`/`injectEndpoints` module), not in each
   page component that happens to import a subset of them — the page is a
   consumer. If the environment provides automatic dependency traversal
   (following an import from a file already read to the file that defines
   what it imports), prefer its resolved result over a same-directory-only
   search; it exists precisely because a directory-scoped search misses the
   defining file when it lives elsewhere. Then enumerate side B completely
   from that defining source (e.g. every hook the API slice actually exports —
   not just the ones one consumer happens to import), one per line.
3. Go through side A ONE ITEM AT A TIME and check it explicitly against the side B
   list you JUST WROTE, not from memory and not from what typically goes
   together. Mark each item covered / not covered right there, next to it.
4. Before finalizing, re-read your own two lists and your own per-item marks.
   Confirm the summary sentence you are about to write does not contradict any
   mark you just made. If it does, the mark is right and the summary is wrong —
   fix the summary, not the mark.
5. Do not fill a gap with a plausible assumption ("CRUD usually has all four verbs
   wired up", "list operations typically include a detail view"). The two lists
   you actually enumerated are the only evidence. If an item from side A does not
   literally appear in the side B list you wrote, it is NOT covered — regardless
   of what would be typical, expected, or usually true elsewhere.
