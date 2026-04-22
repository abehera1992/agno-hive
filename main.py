"""AgnoHive entry point.
Usage:
  python3 main.py "your task here"   # single task
  python3 main.py                    # interactive loop
"""
import asyncio
import sys
from pathlib import Path
from swarm.team import run_task_async

PATTERNS_DIR = Path(__file__).parent / "patterns"


def load_preamble() -> str:
    """Load all pattern files and return as a single preamble block."""
    parts = []
    for md in sorted(PATTERNS_DIR.glob("*.md")):
        parts.append(md.read_text())
    return "\n\n---\n\n".join(parts) if parts else ""


def build_task(user_task: str) -> str:
    preamble = load_preamble()
    if preamble:
        return f"{preamble}\n\n---\n\n## TASK\n{user_task}"
    return user_task


async def main() -> None:
    if len(sys.argv) > 1:
        task = build_task(" ".join(sys.argv[1:]))
        result = await run_task_async(task)
        print(result)
    else:
        print("AgnoHive - type 'exit' to quit.")
        while True:
            try:
                user_task = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if user_task.lower() in ("exit", "quit", ""):
                break
            result = await run_task_async(build_task(user_task))
            print(result)


if __name__ == "__main__":
    asyncio.run(main())
