---
name: file-write-review
description: How to edit files (apply_diff vs write_file), what review_pending means, and what run_command may and may not do — load before making any file change.
---
File editing rules:

- For EXISTING files: ALWAYS use apply_diff(), NEVER write_file(). apply_diff makes
  surgical line-level changes; write_file rewrites the whole file.
- Use write_file() ONLY when creating a brand-new file that does not exist yet.
- Read the file first (get_file_content) to get the exact old_string to replace.
- To APPEND content: include the anchor line in BOTH old_string AND new_string,
  then add the new content after it:
      old_string = "last_line"
      new_string = "last_line\nnew_content"
  Never drop existing lines from new_string unless intentionally deleting them.

run_command is READ-ONLY (CRITICAL):
- run_command is for tests, linters, grep, git status ONLY.
- NEVER use run_command to modify files — no >, >>, sed -i, tee, perl -i.
- "add a line", "update a comment", "change X to Y" → use apply_diff().
- Attempting to write via run_command will be BLOCKED by the server.
- For full shell access (npm install, docker compose, etc.) use run_shell().

File write review (CRITICAL):
- If write_file() or apply_diff() returns "review_pending", the proposed change is
  staged for human review. STOP immediately — do not call any other tool.
- For apply_diff on the SAME file: you MAY continue calling apply_diff on that
  file — each call accumulates into the same .hive_proposed file. AFTER each
  apply_diff, read the staged file (<path>.hive_proposed) via get_file_content to
  verify what is already applied. Then apply ONLY the NEXT distinct change not yet
  in the staged file. NEVER repeat a change already staged.
  Correct pattern (import + function body):
    1st call: update import line  → read .hive_proposed → verify import added
    2nd call: add usage in body   → review_pending (now STOP)
- STOP and report "review_pending: <path>" ONLY when: (a) all changes to the
  current file are staged, OR (b) you are about to write a DIFFERENT file.
- confirm_write and reject_write do NOT exist — you cannot approve writes. The
  human selects confirm/reject in their CLI — your job ends when you report.
- If the user asks to "delete", "undo", or "reject" a .hive_proposed file: do NOT
  call run_command, run_shell, or any tool. Reply: "Type /reject <path> or
  /cleanup in your hive CLI to discard the pending change."
