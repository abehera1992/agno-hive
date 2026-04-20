"""Shared conscience — asyncpg-backed memory store against claude_flow.embeddings.

Agents call memory_store() to persist findings and memory_search() to recall
them. Knowledge accumulates across sessions and is shared across all swarm members.

Note: memory_search / memory_store are also available as MCP tools via the
project context server. This module provides direct DB access for lower latency
or when the MCP server is not running.
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from config.config import config

_pool: Optional[asyncpg.Pool] = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(config.db_url, min_size=1, max_size=5)
    return _pool


async def _store(key: str, value: str) -> str:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO claude_flow.embeddings (key, content, namespace, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $4)
            ON CONFLICT (key) DO UPDATE
              SET content    = EXCLUDED.content,
                  namespace  = EXCLUDED.namespace,
                  updated_at = EXCLUDED.updated_at
            """,
            key,
            value,
            config.memory_namespace,
            datetime.now(timezone.utc),
        )
    return f"stored: {key}"


async def _search(query: str, limit: int = 5) -> str:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key, content, updated_at
            FROM claude_flow.embeddings
            WHERE namespace = $1
              AND (content ILIKE $2 OR key ILIKE $2)
            ORDER BY updated_at DESC
            LIMIT $3
            """,
            config.memory_namespace,
            f"%{query}%",
            limit,
        )
    if not rows:
        return "no results found"
    return "\n---\n".join(
        f"[{r['key']}] ({r['updated_at'].date()})\n{r['content']}" for r in rows
    )


def memory_store(key: str, value: str) -> str:
    """Persist a finding to the shared conscience. key should be descriptive."""
    return asyncio.get_event_loop().run_until_complete(_store(key, value))


def memory_search(query: str) -> str:
    """Search the shared conscience for prior findings related to query."""
    return asyncio.get_event_loop().run_until_complete(_search(query))
