---
name: path-not-found-recovery
description: When get_file_content() returns "File not found" with a ranked disambiguation list of real candidate paths — use the FIRST listed candidate verbatim, never retry the same guessed path or re-run find_files() first.
---
PATH-CORRECTION rule: if get_file_content() returns "File not found: <path>"
followed by "N files named X exist, sorted by how closely their directory
matches your guess (most likely match FIRST)" and a list of real candidate
paths, your VERY NEXT get_file_content() call for that file MUST use the
FIRST listed candidate verbatim, copied exactly — the list is already ranked
by relevance to your own guess, so the first entry is almost always the
right one; only pick a different one if you have a SPECIFIC, stated reason
(e.g. the task explicitly named a different app/service than the top
candidate's directory). Never retry the same guessed/truncated path again,
and never call find_files() again first if the disambiguation list already
contains the file you need.

Confirmed live 2026-08-11 (two distinct incidents): (1) a run called
get_file_content() with the same wrong truncated path 4 times in a row, each
time receiving the correct full path in the disambiguation list and each
time ignoring it, before giving up and pivoting to irrelevant web searches;
(2) a run with an AMBIGUOUS basename (e.g. 'index.tsx', which legitimately
exists in several unrelated parts of this monorepo) mechanically tried
candidates in whatever order they were listed, including an unrelated
mobile-app file, instead of recognizing which one actually matched the
web-frontend task — ranking-by-relevance plus "use the first one" fixes this
without requiring the model to reason about it.
