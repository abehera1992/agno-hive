---
name: verification-discipline
description: How to check a claim before stating it as fact — required before any answer that names a symbol, file:line, or claims something is done/removed/verified.
---
Verification & completion-claim discipline (MANDATORY — applies to ALL claims):

When you state whether something is implemented / done / removed / present / fixed,
base it ONLY on code you actually READ this run (get_file_content / search_files)
and cite the exact file path + line + the literal code as evidence.

BEFORE returning any answer that names a symbol, a file:line, or an API route, call
verify_claims(your_draft_answer). It greps every claim against the repo and reports
what does not exist. If it returns NOT FOUND or BAD, the claim is fabricated — fix
the answer, do not return it. The most common failure is naming a symbol that
merely RESEMBLES the answer: a single-item function offered when asked about a
batch operation, or a neighbouring symbol from the same file. Existing is not the
same as doing what was asked, and verify_claims cannot catch that — it only proves
the name exists.

NEVER claim something was removed/added/completed unless the CURRENT code shows
that state: if the code still calls or contains X, it is NOT removed — say "still
present at <file>:<line>". Do NOT infer "done" from a task title, a filename, a
plausible assumption, or what you expected. If you did not read decisive evidence,
answer "could not verify" — never guess DONE.
