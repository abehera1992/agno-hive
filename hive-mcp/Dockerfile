# ── Stage 1: grab docker CLI binary ──────────────────────────────────────────
FROM docker:27-cli AS docker-cli

# ── Stage 2: hive-mcp server ─────────────────────────────────────────────────
FROM python:3.12-slim

# System tools the agent can invoke via run_shell / run_command
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    wget \
    jq \
    procps \
    openssh-client \
    ca-certificates \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI from official image (no daemon, talks to host socket via mount)
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# Trust the bind-mounted project dir. The container runs as root but /project is
# owned by the host user, so git otherwise refuses with "dubious ownership" —
# which breaks scan_project_context's `git status`/`git diff` change detection
# (hive.md reports "git status failed" and incremental scans fall back to full
# rebuilds). --system writes to /etc/gitconfig, which is NOT masked by the
# runtime /root/.gitconfig bind mount (a --global entry would be).
RUN git config --system --add safe.directory /project

# Python deps
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Server code
COPY . .

# Project root mounted by the user at runtime
VOLUME ["/project"]

ENV PROJECT_ROOT=/project \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=9000 \
    WRITE_REVIEW=true

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('localhost', int(__import__('os').getenv('MCP_PORT','9000')))); s.close()" || exit 1

CMD ["python", "main.py"]
