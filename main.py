"""AgnoHive entry point.

Usage:
  python main.py "describe the seller verification flow"   # single task
  python main.py                                           # interactive loop
"""
import sys

from config.config import config
from swarm.team import build_swarm


def run(task: str) -> None:
    swarm = build_swarm()
    swarm.run(task, stream=config.stream)


def interactive() -> None:
    print("AgnoHive — type 'exit' to quit.")
    swarm = build_swarm()
    while True:
        try:
            task = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if task.lower() in ("exit", "quit", ""):
            break
        swarm.run(task, stream=config.stream)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run(" ".join(sys.argv[1:]))
    else:
        interactive()
