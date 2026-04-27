"""AgnoHive entry point.
Usage:
  python main.py "your task here"   # single task
  python main.py                    # interactive loop
  python main.py --serve            # start FastAPI server on AGNO_PORT (default 9001)
"""
import asyncio
import sys
from swarm.team import run_task_async


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
    if "--serve" in sys.argv:
        import uvicorn
        from api.server import app
        from config.config import config
        print(f"[agno-hive] starting on 0.0.0.0:{config.api_port}")
        uvicorn.run(app, host="0.0.0.0", port=config.api_port)
    else:
        asyncio.run(_interactive())
