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

When answering this class of question:
1. Enumerate side A completely first (e.g. every backend endpoint), one per line.
2. Enumerate side B completely (e.g. every frontend hook actually imported — not
   what you'd expect to be imported), one per line.
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
