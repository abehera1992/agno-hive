"""AgnoHive entry point.
Usage:
  python3 main.py "your task here"   # single task
  python3 main.py                    # interactive loop
"""
import asyncio
import sys
from swarm.team import run_task_async


async def main() -> None:
    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
        result = await run_task_async(task)
        print(result)
    else:
        print("AgnoHive - type 'exit' to quit.")
        while True:
            try:
                task = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if task.lower() in ("exit", "quit", ""):
                break
            result = await run_task_async(task)
            print(result)


if __name__ == "__main__":
    asyncio.run(main())
