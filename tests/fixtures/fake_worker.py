"""Fixture worker process for tests/test_run_worker_subprocess.py.

Stands in for `python main.py --run-worker` (main.py's _run_worker_main/
_run_worker) without needing the real agno/MCP/vLLM stack -- reads the same
JSON-on-stdin, single-JSON-line-on-stdout contract, but its behavior is
driven by a `_test_mode` key in the input payload instead of actually running
a task:

  "success" (default) -- writes a canned {"content", "tokens", "clarification"}
      result and exits 0, mirroring a normal completed run.
  "error"   -- writes {"error": "..."} and exits 0, mirroring _run_worker's own
      except-Exception path (an internal failure, still a clean process exit).
  "crash"   -- writes non-JSON garbage to stdout and exits 1, mirroring an
      unhandled crash (e.g. an import error) that never reaches the try/except
      inside _run_worker at all.
  "hang"    -- blocks forever without ever writing to stdout, mirroring a
      genuinely wedged worker. Only killable via SIGKILL/SIGTERM from the
      parent -- proves _run_worker_subprocess's kill-on-disconnect path
      actually terminates a process that isn't cooperating, the entire point
      of the process-boundary design (see DOCS.md "Process-Boundary
      Cancellation").
  "stale"   -- writes an already-stale liveness snapshot to the path the
      parent computed and passed in (payload["liveness_path"]), then hangs
      exactly like "hang" -- mirrors a genuinely stalled real worker (whose
      _run_heartbeat keeps writing an aging snapshot) well enough to prove
      _run_worker_subprocess's liveness-based auto-kill path (see DOCS.md
      "Liveness-Based Auto-Kill") actually terminates it, without needing a
      real 300s wait or the real agno/MCP/vLLM stack.
"""
import json
import sys
import time


def main() -> None:
    payload = json.loads(sys.stdin.read())
    mode = payload.get("_test_mode", "success")

    if mode == "hang":
        while True:
            time.sleep(3600)
    elif mode == "stale":
        liveness_path = payload.get("liveness_path")
        if liveness_path:
            with open(liveness_path, "w") as f:
                json.dump({"stagnant_seconds": 999999, "max_stub_serve_count": 0}, f)
        while True:
            time.sleep(3600)
    elif mode == "crash":
        print("not valid json", flush=True)
        sys.exit(1)
    elif mode == "error":
        print(json.dumps({"error": "TestError: simulated internal failure"}), flush=True)
    else:
        print(
            json.dumps({
                "content": f"echo: {payload.get('task', '')}",
                "tokens": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                "clarification": None,
            }),
            flush=True,
        )


if __name__ == "__main__":
    main()
