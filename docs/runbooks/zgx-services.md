# ZGX services — how AGNOHive actually runs

The two long-running AGNOHive servers on ZGX are **systemd user units**, not backgrounded
`nohup` processes. This file is tracked (unlike the machine-local `CLAUDE.md` / `DOCS.md`)
so the correct procedure travels with the repo and is visible on ZGX after a `git pull`.

Written 2026-07-31 after the unit names turned out to be documented nowhere: `agno-api`
looks like an unfamiliar tool, but it is simply `main.py --serve` under a service name.

## The units

| Unit | What it is | Port | ExecStart |
|---|---|---|---|
| `agno-api.service` | AgnoHive API — the swarm orchestrator that `agno_run` posts `POST /run` to | 9001 | `~/miniforge3/envs/zgx/bin/python main.py --serve` |
| `lightrag.service` | AgnoHive LightRAG MCP server | 9002 | `~/miniforge3/envs/zgx/bin/python main.py --serve-lightrag` |

Both live in `~/.config/systemd/user/`, are `enabled`, and run with `Restart=always` /
`RestartSec=5`. The user has `Linger=yes`, so they start at boot and keep running after
logout.

Note the separation of hosts: `agno-api` runs on **ZGX** (`100.96.86.82:9001`), while the
MCP servers it connects back to run on the **Windows workstation**
(`100.87.159.86:9000` project MCP, `:9003` hive-mcp). LightRAG is the exception — it is
ZGX-local, which is why agents reach it at `localhost:9002`.

## Managing them

```bash
systemctl --user restart agno-api.service     # after a git pull
systemctl --user status  agno-api.service
systemctl --user cat     agno-api.service     # read the unit definition
journalctl --user -u agno-api.service -f      # live logs
journalctl --user -u agno-api.service -n 50   # recent
```

Deploy loop for a code change (never edit on ZGX directly):

```bash
# on the workstation
git push origin main
# on ZGX
cd ~/agno-hive && git pull origin main && systemctl --user restart agno-api.service
```

## Three things that will mislead you

**1. `pkill` does not stop these services — and the failure is silent.**

Older docs in this repo show:

```bash
pkill -f "python3 main.py --serve$" && cd ~/agno-hive && nohup python3 main.py --serve &
```

That recipe predates the systemd conversion and is now actively harmful. `Restart=always`
brings the process back within 5 seconds, so the kill *appears* to succeed while systemd
silently replaces it; the follow-up `nohup` copy then races the systemd one for port 9001
or fails to bind. You end up believing you restarted a server you did not. Use
`systemctl --user restart`.

**2. An empty `journalctl` tail does not mean nothing is running.**

The server's stdout is buffered under systemd, so a run in flight can show no new
`[team] MCP connected` lines for minutes. Check liveness from the GPU instead:

```bash
nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader
docker logs vllm-coord --since 60s 2>&1 | grep -oP "Running: \d+ reqs" | tail -2
```

This mis-read produced several wrong diagnoses on 2026-07-31 — absent logs were taken as
evidence of an inactive guard when the guard was fine and only the logging was missing.

**3. Restarting drops in-flight runs.**

Since `b70ade5` the API cancels a run when its HTTP client disconnects, so an abandoned
client no longer orphans GPU work. A restart still kills whatever is executing, though.
Before restarting during a measurement or a long task, confirm the GPU is idle
(`utilization.gpu` near 0 and `Running: 0 reqs`).

## Clearing orphaned work

If runs were abandoned by a client that predates `b70ade5` — or the GPU is busy with work
nobody is waiting for — restart the API and wait for idle:

```bash
systemctl --user restart agno-api.service
until [ "$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits)" -lt 15 ]; do sleep 10; done
```

Orphans compound: a handful of abandoned runs held the GPU at 96% on 2026-07-31 and made
every subsequent measurement look pathological — a one-file read that completes in 74s on
an idle box timed out at 600s. Always confirm the GPU is idle before trusting a timing.
