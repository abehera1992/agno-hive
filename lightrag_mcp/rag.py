"""LightRAG instance factory — one instance per project_id, cached for the lifetime of the server."""
import os
from urllib.parse import urlparse

from lightrag import LightRAG
from lightrag.llm.ollama import ollama_model_complete, ollama_embed
from lightrag.utils import EmbeddingFunc

_cache: dict[str, LightRAG] = {}


def get_rag(project_id: str) -> LightRAG:
    if project_id not in _cache:
        _cache[project_id] = _build(project_id)
    return _cache[project_id]


def _build(project_id: str) -> LightRAG:
    from config.config import config

    _set_pg_env(config.postgres_uri)
    os.environ["POSTGRES_WORKSPACE"] = project_id  # isolates all PG tables per project

    working_dir = os.path.join(
        os.getenv("LIGHTRAG_WORKING_DIR", os.path.expanduser("~/.agno-hive/lightrag")),
        project_id,
    )
    os.makedirs(working_dir, exist_ok=True)

    ollama_host = config.ollama_host
    llm_model = config.lightrag_llm_model
    embed_model = config.lightrag_embed_model
    embed_dim = config.lightrag_embed_dim

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

    return LightRAG(
        working_dir=working_dir,
        llm_model_name=llm_model,   # sets global_config["llm_model_name"] used by ollama_model_complete
        llm_model_func=_llm,
        # Indexing throughput tuning (defaults: max_parallel_insert=2,
        # embedding_batch_num=10, gleaning=1). llm_model_max_async stays at
        # its default 4 to match OLLAMA_NUM_PARALLEL=4 on the GB10.
        max_parallel_insert=4,
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
