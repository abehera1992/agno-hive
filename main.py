"""AgnoHive entry point.
Usage:
  python main.py "your task here"                            # single task
  python main.py                                             # interactive loop
  python main.py --serve                                     # FastAPI server on AGNO_PORT (default 9001)
  python main.py --serve-lightrag                            # LightRAG MCP server on LIGHTRAG_MCP_PORT (default 9002)
  python main.py --index --path /repo --project-id <id>     # index a repo into LightRAG
  python main.py --index --path /repo --project-id <id> --force  # force full reindex
"""
import asyncio
import sys
from swarm.team import run_task_async
from observability.setup import setup_telemetry


async def _interactive() -> None:
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


if __name__ == "__main__":
    setup_telemetry()
    if "--serve" in sys.argv:
        import uvicorn
        from api.server import app
        from config.config import config
        print(f"[agno-hive] starting on 0.0.0.0:{config.api_port}")
        uvicorn.run(app, host="0.0.0.0", port=config.api_port)
    elif "--serve-lightrag" in sys.argv:
        from config.config import config
        from lightrag_mcp.server import mcp
        print(f"[agno-hive] lightrag-mcp starting on 0.0.0.0:{config.lightrag_mcp_port}")
        mcp.run(transport="streamable-http")
    elif "--index" in sys.argv:
        sys.argv.remove("--index")
        from indexer.cli import main as index_main
        index_main()
    else:
        asyncio.run(_interactive())
