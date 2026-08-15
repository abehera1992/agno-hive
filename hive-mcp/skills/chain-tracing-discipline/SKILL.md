---
name: chain-tracing-discipline
description: How to trace a chain across modules or services — event/message flows, publisher/consumer pairs, import or call chains, shared schemas — enumerate every participant before describing any link.
---
CHAIN rule: When a question spans a chain across modules or services —
event/message flows, publisher/consumer pairs, import or call chains, shared
schemas:
  Step 1 — enumerate ALL participants before answering: search_files for the
    shared identifier (topic name, event name, symbol) across '**/*', and
    find_files for sibling files following the same naming convention in
    other modules.
  Step 2 — read EVERY file found in Step 1 before describing any link of the
    chain. Files named in the user's prompt are a starting point, never the
    full chain.
  Step 3 — a chain answer is only as accurate as its least-read link. For
    any link you did not read, say "not verified — file not read" instead of
    inferring its role from naming or convention.
