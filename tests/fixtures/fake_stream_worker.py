"""Fixture worker process for tests/test_stream_worker_subprocess.py.

Stands in for `python main.py --stream-worker` (main.py's
_run_stream_worker_main/_run_stream_worker) without needing the real
agno/MCP/vLLM stack -- reads the same JSON-on-stdin, NDJSON-per-chunk-on-
stdout contract, but its behavior is driven by a `_test_mode` key in the
input payload instead of actually running a task:

  "success" (default) -- writes three chunks (a str, a tool-event dict, a
      done dict) as {"ok": true, "v": ...} lines, then exits 0, mirroring a
      normal streamed run.
  "error"   -- writes one chunk, then {"ok": false, "error": "..."} and
      exits 0, mirroring _run_stream_worker's own except-Exception path.
  "hang"    -- blocks forever without ever writing to stdout, mirroring a
      genuinely wedged worker. Only killable via SIGKILL/SIGTERM from the
      parent -- proves _stream_worker_subprocess's kill-on-disconnect path
      actually terminates a process that isn't cooperating.
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
    elif mode == "error":
        print(json.dumps({"ok": True, "v": "partial chunk"}), flush=True)
        print(json.dumps({"ok": False, "error": "TestError: simulated mid-stream failure"}), flush=True)
    else:
        print(json.dumps({"ok": True, "v": f"echo: {payload.get('task', '')}"}), flush=True)
        print(json.dumps({"ok": True, "v": {"__tool_event__": "start", "name": "search_files", "args": {}}}), flush=True)
        print(json.dumps({
            "ok": True,
            "v": {"__done__": True, "content": "final answer", "tokens": {"total_tokens": 5}, "clarification": None},
        }), flush=True)


if __name__ == "__main__":
    main()
