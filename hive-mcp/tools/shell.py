"""Shell, Docker, and environment introspection tools.

These give the agent full terminal access to the user's machine:
shell commands, Docker operations, process inspection, port checks.
The Docker socket is mounted from the host so docker commands target
the host daemon (not a daemon inside this container).
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import PROJECT_ROOT


def _run(cmd: list[str], timeout: int = 60, cwd=None) -> str:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or PROJECT_ROOT,
        )
        out = r.stdout.strip()
        err = r.stderr.strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr] {err}")
        parts.append(f"[exit {r.returncode}]")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return f"command not found: {e}"
    except Exception as e:
        return f"failed: {e}"


def run_shell(command: str, timeout: int = 120) -> str:
    """
    Run any shell command in the project root directory.
    Use for: installing deps, running build scripts, starting services,
    checking environment, running arbitrary CLI tools.

    Unlike run_command this is not restricted to read-only operations — use
    it when you genuinely need to modify the environment (npm install,
    pip install, docker compose up, etc.).

    Args:
        command: Shell command string (e.g. 'npm install', 'pip install -r requirements.txt')
        timeout: Seconds before timeout (default 120)

    Examples:
        run_shell('npm install')
        run_shell('docker compose up -d')
        run_shell('python -m pytest tests/ -v --tb=short')
        run_shell('curl -s http://localhost:8000/health')
    """
    try:
        r = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=PROJECT_ROOT,
        )
        parts = []
        if r.stdout.strip():
            parts.append(r.stdout.strip())
        if r.stderr.strip():
            parts.append(f"[stderr]\n{r.stderr.strip()}")
        parts.append(f"[exit {r.returncode}]")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"
    except Exception as e:
        return f"run_shell failed: {e}"


def run_docker(command: str, timeout: int = 60) -> str:
    """
    Run a Docker CLI command against the host Docker daemon.
    The Docker socket is mounted from the host so commands see all host containers.

    Args:
        command: Docker subcommand (without the leading 'docker').
                 e.g. 'ps', 'ps -a', 'logs my-container --tail 50',
                      'compose up -d', 'images', 'inspect my-container'

    Examples:
        run_docker('ps')                         → list running containers
        run_docker('ps -a')                      → all containers
        run_docker('logs my-api --tail 100')     → recent container logs
        run_docker('compose up -d')              → start compose services
        run_docker('compose restart api')        → restart a service
        run_docker('exec my-api env')            → env vars in a container
        run_docker('images')                     → list images
        run_docker('system df')                  → disk usage
    """
    return _run(["docker"] + command.split(), timeout=timeout)


def get_env_info() -> str:
    """
    Return a summary of the runtime environment: OS, Python version,
    key CLI tools present, environment variables relevant to the project.
    Useful for diagnosing setup issues or understanding what's available.
    """
    import platform
    import shutil

    lines = [
        f"OS: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python: {platform.python_version()} at {sys.executable}",
        f"Project root: {PROJECT_ROOT}",
        "",
        "── Available tools ──────────────────────────────────",
    ]

    tools = ["git", "docker", "docker-compose", "node", "npm", "python3",
             "pip3", "curl", "jq", "make", "go", "cargo", "mvn", "gradle"]
    for t in tools:
        path = shutil.which(t)
        if path:
            lines.append(f"  {t:<16} {path}")

    lines += ["", "── Environment variables (non-sensitive) ────────────"]
    skip_keys = {"PATH", "LS_COLORS", "PS1", "PS2"}
    for k, v in sorted(os.environ.items()):
        if k in skip_keys:
            continue
        if any(s in k.upper() for s in ("SECRET", "PASSWORD", "TOKEN", "KEY", "PRIVATE")):
            lines.append(f"  {k}=<redacted>")
        else:
            lines.append(f"  {k}={v[:120]}")

    return "\n".join(lines)


def check_port(port: int) -> str:
    """
    Check whether a TCP port is listening on localhost.
    Useful for verifying services are up before testing.

    Args:
        port: Port number to check (e.g. 8000, 5432, 6379)

    Examples:
        check_port(8000)    → is the API server running?
        check_port(5432)    → is Postgres up?
        check_port(6379)    → is Redis running?
    """
    import socket
    try:
        with socket.create_connection(("localhost", port), timeout=2):
            return f"port {port}: OPEN — something is listening"
    except ConnectionRefusedError:
        return f"port {port}: CLOSED — nothing listening"
    except OSError as e:
        return f"port {port}: ERROR — {e}"


def list_processes(filter_str: str = "") -> str:
    """
    List running processes, optionally filtered by name or command.

    Args:
        filter_str: Optional substring to filter process list (e.g. 'python', 'node', 'uvicorn')

    Examples:
        list_processes()              → all processes
        list_processes('python')      → Python processes only
        list_processes('uvicorn')     → uvicorn processes
    """
    result = _run(["ps", "aux"], timeout=10, cwd="/")
    if not filter_str:
        return result
    lines = result.splitlines()
    header = lines[0] if lines else ""
    matched = [l for l in lines[1:] if filter_str.lower() in l.lower()]
    if not matched:
        return f"No processes matching '{filter_str}'"
    return header + "\n" + "\n".join(matched)
