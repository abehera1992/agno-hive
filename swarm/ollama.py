"""Ollama model management — check availability and pull on demand."""
import httpx


async def list_models(ollama_host: str) -> list[str]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ollama_host}/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]


async def pull_model(model: str, ollama_host: str) -> None:
    """Pull a model and drain the stream until complete."""
    print(f"[agno-hive] pulling model: {model}")
    async with httpx.AsyncClient(timeout=600) as client:
        async with client.stream("POST", f"{ollama_host}/api/pull", json={"name": model}) as r:
            r.raise_for_status()
            async for _ in r.aiter_lines():
                pass


async def ensure_models(models: list[str], ollama_host: str) -> list[str]:
    """Pull any models not already in Ollama. Returns list of models that were pulled."""
    available = await list_models(ollama_host)
    # Match both exact tags (deepseek-r1:latest) and tagless names (deepseek-r1)
    available_exact = set(available)
    available_bases = {m.split(":")[0] for m in available}
    pulled = []
    for model in models:
        base = model.split(":")[0]
        if model not in available_exact and base not in available_bases:
            await pull_model(model, ollama_host)
            pulled.append(model)
    return pulled
