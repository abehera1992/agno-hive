"""LightRAG instance factory — one instance per project_id, cached for the lifetime of the server."""
import os
from urllib.parse import urlparse

import numpy as np
import tiktoken
from openai import AsyncOpenAI

from lightrag import LightRAG
from lightrag.llm.ollama import ollama_model_complete, ollama_embed
from lightrag.llm.openai import openai_complete_if_cache
from lightrag.utils import EmbeddingFunc, Tokenizer

# ── Disable LightRAG's rebuild-on-delete (deadlock workaround, 2026-06-13) ────
# Re-indexing a changed file deletes the old doc version then re-inserts the new
# one. LightRAG's adelete_by_doc_id runs rebuild_knowledge_from_chunks() to scrub
# the deleted chunks' contributions from every affected entity/relationship. For
# high-degree entities (e.g. tenant_id in ~294 chunks, user_id in ~193) that
# parallel rebuild wedges in LightRAG 1.4.16's keyed-lock layer
# (operate.rebuild_knowledge_from_chunks -> get_storage_keyed_lock): tasks block
# on lock acquisition and never reach the LLM, hanging the whole bootstrap with
# Ollama idle, Postgres idle, and the event loop parked. Reproduced on every
# bootstrap that touched those entities.
#
# The rebuild is redundant for our workflow: we always delete-then-REINSERT, and
# the reinsert re-extracts those same entities from the new chunks. So we no-op
# the rebuild. (Only a pure delete with no following insert would skip the
# scrub, which the indexer never does.) lightrag.lightrag binds the symbol at
# module load (from .operate import rebuild_knowledge_from_chunks) and calls it
# by bare name, so the patch must target THAT module's namespace.
import logging as _logging
import lightrag.lightrag as _lr_module

_rebuild_log = _logging.getLogger("lightrag_mcp.rag")


async def _skip_rebuild_knowledge_from_chunks(*args, **kwargs):
    ents = kwargs.get("entities_to_rebuild") or {}
    rels = kwargs.get("relationships_to_rebuild") or {}
    if ents or rels:
        _rebuild_log.info(
            "rebuild_knowledge_from_chunks skipped (reindex no-op): "
            "%d entities, %d relationships",
            len(ents), len(rels),
        )
    return None


_lr_module.rebuild_knowledge_from_chunks = _skip_rebuild_knowledge_from_chunks


# ── Tokenizer that tolerates LLM special tokens appearing in source text ──────
# agno-hive source files (e.g. swarm/team.py, lightrag_mcp/server.py — the tool-
# call parsing code) contain literal strings like "<|endoftext|>". LightRAG's
# chunker calls tiktoken.encode() with the default disallowed_special check, which
# RAISES "disallowed special token" on those and fails the WHOLE document — exactly
# why those two files failed every bootstrap. Encode special tokens as normal text.
class _SpecialSafeTiktoken:
    def __init__(self, model_name: str = "gpt-4o-mini"):
        self._enc = tiktoken.encoding_for_model(model_name)

    def encode(self, content: str):
        return self._enc.encode(content, disallowed_special=())

    def decode(self, tokens):
        return self._enc.decode(tokens)


_SAFE_TOKENIZER = Tokenizer("gpt-4o-mini", _SpecialSafeTiktoken())

_cache: dict[str, LightRAG] = {}


def get_rag(project_id: str) -> LightRAG:
    if project_id not in _cache:
        _cache[project_id] = _build(project_id)
    return _cache[project_id]


def _build(project_id: str) -> LightRAG:
    from config.config import config

    _set_pg_env(config.postgres_uri)
    # POSTGRES_WORKSPACE must NEVER be set. LightRAG's PG storage classes
    # contain `if self.db.workspace: self.workspace = self.db.workspace` —
    # i.e. the shared ClientManager singleton's env-derived workspace
    # OVERRIDES the per-instance `workspace=` constructor field whenever it
    # is set, pinning every project in the process to whichever initialized
    # first after a restart (caused cross-project contamination twice:
    # 2026-06-11 and 2026-06-12). With the env unset, each instance keeps
    # its own workspace from the constructor below.
    os.environ.pop("POSTGRES_WORKSPACE", None)

    working_dir = os.path.join(
        os.getenv("LIGHTRAG_WORKING_DIR", os.path.expanduser("~/.agno-hive/lightrag")),
        project_id,
    )
    os.makedirs(working_dir, exist_ok=True)

    embed_dim = config.lightrag_embed_dim
    backend = (config.inference_backend or "ollama").lower()
    _rebuild_log.info("LightRAG[%s] inference backend = %s", project_id, backend)

    if backend == "vllm":
        # OpenAI-compatible vLLM endpoints (Ollama->vLLM migration, EK-105). Extraction
        # LLM + embeddings are served by vLLM behind an OpenAI API; both stay 1024-dim
        # so the existing Qdrant/AGE index is reused (verified: cosine 0.999 vs Ollama).
        llm_model = config.vllm_llm_model

        async def _llm(prompt, system_prompt=None, history_messages=None, **kwargs):
            kwargs.pop("model", None)
            kwargs.pop("host", None)
            kwargs.pop("options", None)
            return await openai_complete_if_cache(
                config.vllm_llm_model,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages or [],
                base_url=config.vllm_llm_base_url,
                api_key="EMPTY",
                **kwargs,
            )

        # EXTRACT role — fast 8B model (Meta-Llama-3.1-8B-Instruct-FP8) via LiteLLM
        # alias "llama3.1-8b" → port 9100. Extraction prompts are short (≤1200 tokens
        # per chunk), so a 7B/8B model is sufficient and 3-4× faster than the 30B.
        async def _extract_llm(prompt, system_prompt=None, history_messages=None, **kwargs):
            kwargs.pop("model", None)
            kwargs.pop("host", None)
            kwargs.pop("options", None)
            return await openai_complete_if_cache(
                config.vllm_extract_model,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages or [],
                base_url=config.vllm_extract_base_url,
                api_key="EMPTY",
                **kwargs,
            )

        # Call vLLM's /v1/embeddings directly (NOT lightrag's openai_embed wrapper):
        # that wrapper is an EmbeddingFunc preset to OpenAI's 1536 dim and reshapes the
        # flat response by 1536, mangling our native-1024 vectors. The qwen3 embedder
        # returns 1024-dim natively, so we send no `dimensions` param.
        _embed_client = AsyncOpenAI(base_url=config.vllm_embed_base_url, api_key="EMPTY")

        async def _embed(texts: list[str]):
            resp = await _embed_client.embeddings.create(
                model=config.vllm_embed_model, input=texts,
            )
            return np.array([d.embedding for d in resp.data], dtype=np.float32)
    else:
        ollama_host = config.ollama_host
        llm_model = config.lightrag_llm_model
        embed_model = config.lightrag_embed_model

        async def _llm(prompt, system_prompt=None, history_messages=None, **kwargs):
            # ollama_model_complete reads model from global_config["llm_model_name"],
            # not from a kwarg — passing model= here causes a duplicate-arg TypeError.
            kwargs.pop("model", None)
            # Size the context window to the prompt: extraction calls (one <=1200
            # token chunk) fit in 8K and keep Ollama's 4 parallel slots cheap;
            # query calls assemble up to ~30K tokens of graph context and need the
            # full 32K — a fixed 8K window truncates them into garbage answers.
            approx_tokens = (len(prompt) + len(system_prompt or "")) // 4
            num_ctx = 8192 if approx_tokens <= 5500 else 32768
            return await ollama_model_complete(
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages or [],
                host=ollama_host,
                options={"num_ctx": num_ctx},
                **kwargs,
            )

        async def _embed(texts: list[str]) -> list[list[float]]:
            return await ollama_embed(texts, embed_model=embed_model, host=ollama_host)

    _lightrag_kwargs: dict = dict(
        working_dir=working_dir,
        # Treat LLM special tokens (<|endoftext|> etc.) in source text as normal text.
        tokenizer=_SAFE_TOKENIZER,
        # Per-instance workspace — passed to every storage backend. Without
        # this, all projects shared whatever POSTGRES_WORKSPACE happened to be
        # set when the process-wide PG client first initialized (restart-order
        # roulette): an agno-hive bootstrap once wrote into the ekam workspace.
        workspace=project_id,
        llm_model_name=llm_model,   # sets global_config["llm_model_name"] used by ollama_model_complete
        llm_model_func=_llm,
        # Indexing throughput tuning (defaults: max_parallel_insert=2,
        # embedding_batch_num=10, gleaning=1). Both raised 6→12 (2026-07-08):
        # vLLM continuous batching handles concurrent requests natively at the
        # GPU level — no slot serialisation like Ollama. 12 async slots halves
        # extraction time for large files (~360s → ~180s LLM-only on a 33-chunk
        # file). Original 6 matched OLLAMA_NUM_PARALLEL=6 (now unused).
        max_parallel_insert=12,
        llm_model_max_async=12,
        embedding_batch_num=32,
        # Gleaning is a second full LLM pass per chunk for marginal extra
        # entities — poor trade on code corpora; halves extraction calls.
        entity_extract_max_gleaning=0,
        embedding_func=EmbeddingFunc(
            embedding_dim=embed_dim,
            max_token_size=8192,
            model_name=project_id,   # sets workspace_id in Qdrant payload → project isolation
            func=_embed,
        ),
        # Storage backends — all four must be set explicitly
        kv_storage="PGKVStorage",
        vector_storage="QdrantVectorDBStorage",
        graph_storage="PGGraphStorage",
        doc_status_storage="PGDocStatusStorage",
        vector_db_storage_cls_kwargs={
            "collection_name": f"project_{project_id}",
            "url": config.qdrant_url,
        },
    )

    # Role-split LLMs (LightRAG >= 1.5.0): EXTRACT → fast 8B, QUERY → 30B.
    # Guarded so deploying this before the ZGX pip upgrade doesn't crash the service.
    try:
        import importlib.metadata as _meta
        _lr_version = tuple(int(x) for x in _meta.version("lightrag-hku").split(".")[:3])
        if _lr_version >= (1, 5, 0):
            _extract_func = _extract_llm if backend == "vllm" else _llm
            _lightrag_kwargs["role_llm_configs"] = {
                "extract": {"func": _extract_func, "max_async": 12},
                "query": {"func": _llm, "max_async": 12},
            }
            _rebuild_log.info(
                "LightRAG[%s] role_llm_configs active: extract=%s query=%s",
                project_id,
                config.vllm_extract_model if backend == "vllm" else "ollama-default",
                config.vllm_llm_model if backend == "vllm" else llm_model,
            )
    except Exception as _e:
        _rebuild_log.warning("role_llm_configs skipped: %s", _e)

    return LightRAG(**_lightrag_kwargs)


def _set_pg_env(postgres_uri: str) -> None:
    """Parse postgres_uri and export the env vars LightRAG's PGGraphStorage reads."""
    if not postgres_uri:
        return
    p = urlparse(postgres_uri)
    os.environ.setdefault("AGE_POSTGRES_DB", (p.path or "").lstrip("/"))
    os.environ.setdefault("AGE_POSTGRES_USER", p.username or "")
    os.environ.setdefault("AGE_POSTGRES_PASSWORD", p.password or "")
    os.environ.setdefault("AGE_POSTGRES_HOST", p.hostname or "localhost")
    os.environ.setdefault("AGE_POSTGRES_PORT", str(p.port or 5432))
    os.environ.setdefault("POSTGRES_GRAPH_NAME", "agno")
