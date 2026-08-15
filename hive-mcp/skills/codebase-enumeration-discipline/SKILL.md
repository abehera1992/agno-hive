---
name: codebase-enumeration-discipline
description: How to answer "list all APIs", "what services exist", "show routes under X" — enumerate every subdirectory first, then read exactly one anchor file per directory, covering all of them before answering.
---
COVERAGE rule: For structure and overview questions, you MUST cover every
top-level directory.
  Stopping at the first interesting directory is a failure. If a directory
  has subdirectories, list them too.

ENUMERATION rule: ONLY when the question itself is about MANY things across
the codebase — "list all APIs", "what services exist", "show routes under X"
(X being the whole scope to enumerate, not one feature inside it). When it
applies, always:
  Step 1 — call list_directory(target_dir) to get the COMPLETE list of all
    subdirectories in one call.
    Do not assume the directory name — derive it from list_directory_tree()
    or find_files() first if unsure.
  Step 2 — read the list from Step 1 and note EVERY subdirectory name before
    reading any files.
  Step 3 — for EACH subdirectory, read exactly ONE anchor file (prefer
    README.md, then main.py, then __init__.py).
    Do not read multiple files per subdirectory for listing/overview
    questions — one file per service is enough.
  Step 4 — only write the final answer after processing EVERY subdirectory
    in the Step 1 list.
  Never stop at the first N services because you have "enough". If Step 1
  listed 8 services, answer must cover all 8.
  NOT a case for this rule: a task scoped to ONE specific feature, file,
  endpoint, or service — e.g. "add caching to the plan-limits lookup in
  business-service" names exactly one service and one feature; it is not
  asking what services exist. Stay inside the service/feature actually named
  in the task. Reading every OTHER service's __init__.py to be thorough is
  not thoroughness here, it is answering a question nobody asked — confirmed
  live 2026-08-10: this exact over-application swept unrelated services'
  __init__.py files (inventory-service, email-worker, email-service) on a
  task that named only business-service, contributing to a run that took
  18+ minutes instead of finishing quickly.
