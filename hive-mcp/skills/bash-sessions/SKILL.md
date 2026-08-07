---
name: bash-sessions
description: How to use persistent bash sessions (bash_session_start/bash_run/bash_session_close) and background jobs (bash_run background=True, bash_job_status, bash_job_kill) -- when to use this vs run_command/run_shell, session_id chaining, what persists and what doesn't, and background-job polling cadence. Load before a multi-step shell workflow or a command expected to run long.
---
When to use vs run_command / run_shell:

- Use bash_run only when a task needs the working directory to persist across
  MULTIPLE calls (e.g. cd into a subpackage, then run several commands there), or
  a command shouldn't block the whole turn (background=True).
- For a single one-off command, run_command or run_shell is simpler and needs no
  session bookkeeping or cleanup.

session_id chaining:

- Call bash_session_start() once, keep the returned session_id, pass it to every
  later bash_run() call in the same task -- exactly like chaining agno_run's
  session_id across calls. The session_id is the ONLY thing carrying state between
  calls; do not guess or reuse an id from a different task.
- Call bash_session_close(session_id) when the multi-step workflow is done,
  especially if more than one session was opened in one task -- don't leave
  sessions open past when they're needed.

What persists and what doesn't:

- ONLY the working directory persists (via a bare `cd <path>` as the entire
  command). Nothing else does -- `export FOO=bar`, activated venvs, shell
  functions, aliases: none of it survives to the next bash_run call. Each call is
  still its own subprocess.
- A `cd` chained inside a larger command (`cd sub && pytest`) runs in that
  command's own subshell and does NOT change the session's persisted cwd -- same
  as real shell scoping.
- A bare `cd` to a path that doesn't exist does not change the session's cwd
  either (the command fails, and the failure is visible in the returned output).

Background jobs:

- bash_run(..., background=True) returns a job_id immediately instead of waiting.
  `timeout` becomes the job's max RUNTIME in this mode, not a wait -- it is killed
  and marked timed_out if it runs past that.
- Poll it LATER with bash_job_status(job_id) -- do not sleep-poll-retry in a tight
  loop. There is no push/streaming channel here, so polling is a deliberate,
  spaced-out check, not a busy-wait.
- Do not re-run the same command to "check" whether a background job finished --
  poll the same job_id instead.
- bash_job_status returns status (running/exited/timed_out/killed), exit_code once
  finished, and the most recent output. A very chatty background job's early
  output can fall off the capped output buffer before you poll it. If the full
  record matters, have the command itself redirect verbose output to a file, and
  read that file with get_file_content rather than relying on bash_job_status's
  accumulated buffer.
- bash_job_kill(job_id) terminates a running job. It's a no-op with a clear
  message if the job already finished or was already killed -- safe to call
  defensively.
- A `cd` inside a background command does NOT update the session's persisted cwd
  -- cwd tracking only applies to blocking bash_run calls.

Expiry:

- Sessions die on server restart and after an idle timeout (no activity at all).
  A RUNNING background job is only ever reaped by its own `timeout` (max runtime),
  never by idle/no-output alone -- a legitimately slow job can go quiet for a
  while without being stuck. A FINISHED job (exited/timed_out/killed) is removed
  once nobody has polled it for a while, to free the slot.
- A "unknown session_id" / "unknown job_id" error means start over -- don't
  assume an id is durable across a long gap.

Safety (same model as run_shell/run_command, not a new one):

- The same WRITE_REVIEW-gated write-command blocklist applies to bash_run (both
  blocking and background) as to run_shell/run_command -- it's a heuristic, not a
  hardened sandbox. Use apply_diff()/write_file() for actual file edits, same rule
  as always.
- bash_session_start, bash_run, bash_session_close, and bash_job_kill are
  mutating tools -- unavailable to read-only teams by design, same as
  run_shell/run_command. bash_job_status is read-only and not gated the same way,
  but a read-only team can never obtain a job_id in the first place since bash_run
  itself is unavailable to them.
