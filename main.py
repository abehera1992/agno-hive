"""AgnoHive entry point.
Usage:
  python main.py "your task here"                            # single task
  python main.py                                             # interactive loop
  python main.py --serve                                     # FastAPI server on AGNO_PORT (default 9001)
  python main.py --serve-lightrag                            # LightRAG MCP server on LIGHTRAG_MCP_PORT (default 9002)
  python main.py --index --path /repo --project-id <id>     # index a repo into LightRAG
  python main.py --index --path /repo --project-id <id> --force  # force full reindex
  python main.py --run-worker                                # internal: worker-process entrypoint for
                                                               # api/server.py's process-boundary cancellation
                                                               # (reads a run_task_async() payload from stdin,
                                                               # writes one JSON result line to stdout). Not
                                                               # meant to be invoked by hand -- see
                                                               # api/server.py's _run_worker_subprocess().
  python main.py --stream-worker                              # internal: same as --run-worker but for /stream --
                                                               # reads a run_task_stream() payload from stdin,
                                                               # writes one NDJSON line PER YIELDED CHUNK to
                                                               # stdout as they arrive. Not meant to be invoked
                                                               # by hand -- see api/server.py's
                                                               # _stream_worker_subprocess().
"""
import asyncio
import json
import sys
from swarm import model_routing
from swarm.team import run_task_async, run_task_stream
from observability.setup import setup_telemetry


async def _interactive() -> None:
    # Non-blocking diagnostic (2026-08-16), once per process — the CLI path
    # never runs api/server.py's startup event, so it needs its own call.
    # See model_routing.check_coordinator_readiness()'s docstring for the
    # onboarding gap this closes: a brand-new user with no Ollama/vLLM set up
    # otherwise gets a raw connection error mid-task instead of this.
    await model_routing.ensure_cache_loaded()
    warning = await model_routing.check_coordinator_readiness()
    if warning:
        print(f"[readiness] WARNING: {warning}\n")

    if len(sys.argv) > 1:
        result = await run_task_async(" ".join(sys.argv[1:]))
        print(result)
    else:
        print("AgnoHive — type 'exit' to quit.")
        while True:
            try:
                user_task = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if user_task.lower() in ("exit", "quit", ""):
                break
            result = await run_task_async(user_task)
            print(result)


# Substrings that mark an exception as describing HOW a run unwound, not WHY it failed.
_TEARDOWN_ERROR_MARKERS = (
    "cancel scope",
    "unhandled errors in a taskgroup",
    "during closing of asynchronous generator",
    "generatorexit",
)


def _is_teardown_artifact(exc: BaseException) -> bool:
    """True for an exception that only describes HOW a dying run unwound, never WHY.

    anyio raises these while tearing down a task group or finalizing an async
    generator whose owning task has already gone away. They carry no information
    about the failure that started the unwind, and because Python lets an exception
    raised during `__aexit__` REPLACE the in-flight one, they routinely end up being
    the only thing the caller ever sees.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _TEARDOWN_ERROR_MARKERS)


def _iter_related(exc: BaseException, _seen: set[int] | None = None):
    """Walk an exception's full related set: __cause__, __context__, and the
    sub-exceptions of any BaseExceptionGroup (anyio wraps failures in these)."""
    _seen = set() if _seen is None else _seen
    if exc is None or id(exc) in _seen:
        return
    _seen.add(id(exc))
    yield exc
    for sub in getattr(exc, "exceptions", None) or ():
        yield from _iter_related(sub, _seen)
    for nxt in (exc.__cause__, exc.__context__):
        if nxt is not None:
            yield from _iter_related(nxt, _seen)


def _describe_failure(exc: BaseException) -> str:
    """The most diagnostic description available for a failed run.

    A run that dies mid-flight tears down its MCP connections, and anyio's teardown
    can raise an exception that REPLACES the real cause -- so the worker's old
    `f"{type(exc).__name__}: {exc}"` reported the symptom and discarded the diagnosis.

    Measured 2026-08-20: a run hit
    `litellm.ContextWindowExceededError: ... maximum context length is 262144 tokens
    ... your prompt contains at least 258049 input tokens` after a run_docker('logs
    ekamapp-postgres-1') returned 1.7M chars. What reached the caller was
    `RuntimeError: Attempted to exit a cancel scope that isn't the current tasks's
    current cancel scope` -- true, useless, and with the actual cause nowhere in it.
    Confirmed from the same run's logs that no RunError/TeamRunError stream event was
    emitted, so _BackendRunError's fail-fast path never engaged and this teardown
    exception was genuinely the only one that escaped.

    Prefers the first related exception that is NOT a teardown artifact, searching
    __cause__/__context__ and BaseExceptionGroup members. When the chain holds nothing
    better -- which happens when the real error died in a different task and was never
    chained -- it still reports the teardown error, but LABELLED as such so the reader
    knows to look upstream in the logs rather than treating it as the diagnosis.
    """
    related = list(_iter_related(exc))
    informative = next((e for e in related if not _is_teardown_artifact(e)), None)

    if informative is not None and informative is not exc:
        return (f"{type(informative).__name__}: {informative} "
                f"(surfaced during teardown as {type(exc).__name__})")
    if informative is not None:
        return f"{type(informative).__name__}: {informative}"
    return (f"{type(exc).__name__}: {exc} — this is a teardown artifact, not the "
            f"root cause; the real failure is earlier in this run's logs")


async def _run_worker() -> dict:
    """Worker-process entrypoint for /run's process-boundary execution (see
    DOCS.md "Process-Boundary Cancellation" for the full design). Reads a
    pre-resolved run_task_async() kwargs payload from stdin (JSON — the parent,
    api/server.py's /run handler, does all team/session/MCP-URL resolution
    exactly as it always has; this process's only job is the actual
    run_task_async call), runs it, and returns the result as a plain dict for
    __main__ to print as the single JSON line on stdout.

    Returns rather than prints directly: __main__ redirects sys.stdout to
    stderr for the whole run (so every existing [team]/[api] print() call
    throughout the codebase, unchanged, lands on stderr instead) and only
    restores real stdout afterward, right before printing the result -- that
    keeps the swap-and-restore in ONE place instead of threading a "real
    stdout" handle through this function. The parent inherits this process's
    stderr straight through to its own stdout/journald, so
    `journalctl -u agno-api` keeps showing the exact same trace lines it
    always has; only the mechanism producing them moved one process over.

    Deliberately carries NO is_disconnected/cancellation-checking wiring at
    all -- this process is SIGKILLed outright by the parent on disconnect,
    never asked to cooperatively unwind. That is the entire point of this
    design: four rounds of cooperative cancellation (agno's own
    acancel_run/araise_if_cancelled, generic asyncio task.cancel(), a shared
    claimed-flag between two independent checkers) each closed one specific
    way cancellation could land wrong inside agno + MCP + anyio's nested
    async call graph, and each was followed by a new way it still didn't. A
    hard process kill doesn't depend on this process -- or anything it calls
    into -- behaving correctly under cancellation pressure; the OS reclaims
    every socket and every anyio scope unconditionally when the process dies.
    """
    from api.models import AgentSpec

    try:
        payload = json.loads(sys.stdin.read())
        agent_specs = (
            [AgentSpec(**d) for d in payload["agent_specs"]]
            if payload.get("agent_specs") else None
        )
        content, tokens, clarification = await run_task_async(
            task=payload["task"],
            agent_specs=agent_specs,
            coordinator_model=payload.get("coordinator_model"),
            coordinator_tools=payload.get("coordinator_tools"),
            mcp_url=payload.get("mcp_url"),
            mcp_urls=payload.get("mcp_urls"),
            project_id=payload.get("project_id", "default"),
            session_id=payload.get("session_id"),
            mode=payload.get("mode", "coordinate"),
            read_only=payload.get("read_only", False),
            liveness_path=payload.get("liveness_path"),
            team_name=payload.get("team_name"),
        )
        return {"content": content, "tokens": tokens, "clarification": clarification}
    except Exception as exc:
        return {"error": _describe_failure(exc)}


def _run_worker_main() -> None:
    """Redirects stdout -> stderr BEFORE anything (including setup_telemetry()'s
    own startup print) can write to it -- stdout must carry ONLY the final JSON
    result line, nothing else, or the parent's single-line parse breaks."""
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    setup_telemetry()
    result = asyncio.run(_run_worker())
    sys.stdout = real_stdout
    print(json.dumps(result), flush=True)


async def _run_stream_worker(real_stdout) -> None:
    """Worker-process entrypoint for /stream's process-boundary execution --
    the incremental counterpart to _run_worker() above (Phase 3, see DOCS.md
    "Process-Boundary Cancellation"). Reads the same shape of pre-resolved
    kwargs payload from stdin, but for run_task_stream() instead of
    run_task_async(), and writes ONE NDJSON line PER YIELDED CHUNK to stdout
    as they arrive -- /stream needs incremental delivery, /run doesn't.

    Each line is {"ok": true, "v": <chunk>} where <chunk> is exactly what
    run_task_stream() itself yielded (a str content delta, or a dict tool-
    event/done sentinel) -- json.dumps/json.loads round-trip str vs dict
    without needing an explicit type tag, so the parent's existing per-chunk
    SSE-formatting logic (api/server.py's /stream generate()) is reused
    completely unchanged; only where the chunks come from moves. A failure
    becomes {"ok": false, "error": "..."} as the last line instead of a
    crash, mirroring _run_worker's except-Exception behavior.

    real_stdout is threaded through explicitly (unlike _run_worker, which
    returns once at the end) because chunks must be written AS THEY ARRIVE,
    not accumulated and printed once -- __main__ redirects sys.stdout to
    stderr for the whole run exactly as it does for --run-worker, so every
    existing [team]/[api] print() call is unaffected, and this function
    prints straight to the stashed real handle for every chunk instead.
    """
    from api.models import AgentSpec

    payload = json.loads(sys.stdin.read())
    agent_specs = (
        [AgentSpec(**d) for d in payload["agent_specs"]]
        if payload.get("agent_specs") else None
    )
    try:
        async for chunk in run_task_stream(
            task=payload["task"],
            agent_specs=agent_specs,
            coordinator_model=payload.get("coordinator_model"),
            coordinator_tools=payload.get("coordinator_tools"),
            mcp_url=payload.get("mcp_url"),
            mcp_urls=payload.get("mcp_urls"),
            project_id=payload.get("project_id", "default"),
            session_id=payload.get("session_id"),
            mode=payload.get("mode", "coordinate"),
            read_only=payload.get("read_only", False),
            team_name=payload.get("team_name"),
        ):
            print(json.dumps({"ok": True, "v": chunk}), file=real_stdout, flush=True)
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
            file=real_stdout, flush=True,
        )


def _run_stream_worker_main() -> None:
    """Same stdout->stderr discipline as _run_worker_main, but the real handle
    is threaded into _run_stream_worker directly (see its docstring) instead
    of being restored once at the end, since chunks print incrementally."""
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    setup_telemetry()
    asyncio.run(_run_stream_worker(real_stdout))


if __name__ == "__main__":
    if "--run-worker" in sys.argv:
        _run_worker_main()
    elif "--stream-worker" in sys.argv:
        _run_stream_worker_main()
    elif "--serve" in sys.argv:
        setup_telemetry()
        import uvicorn
        from api.server import app
        from config.config import config
        print(f"[agno-hive] starting on 0.0.0.0:{config.api_port}")
        uvicorn.run(app, host="0.0.0.0", port=config.api_port)
    elif "--serve-lightrag" in sys.argv:
        setup_telemetry()
        from config.config import config
        from lightrag_mcp.server import mcp
        print(f"[agno-hive] lightrag-mcp starting on 0.0.0.0:{config.lightrag_mcp_port}")
        mcp.run(transport="streamable-http")
    elif "--index" in sys.argv:
        setup_telemetry()
        sys.argv.remove("--index")
        from indexer.cli import main as index_main
        index_main()
    else:
        setup_telemetry()
        asyncio.run(_interactive())
