"""Tests for swarm/agents.py's get_model() -- DB-backed model routing (AGNOHive
2.3.2 addendum, 2026-08-08) plus regression coverage for the pre-existing vLLM/
Ollama branches.

House style: swarm/agents.py does `from config.config import config` (one shared
object import, not per-name), so tests patch attributes directly on that object --
`monkeypatch.setattr(config, "x", value)` -- matching test_agents_skills.py's existing
`monkeypatch.setattr("swarm.agents.config.inference_backend", "ollama")` pattern.

Routing itself is no longer a hardcoded dict/set in swarm/agents.py -- get_model()
reads swarm/model_routing.py's in-process cache, seeded fresh against an in-memory
SQLite DB (model_catalog/team_role_models tables) by the autouse fixture below, so
every test starts from the same known-good seed state regardless of test order.
"""
import pytest

from config.config import config
from swarm import db, model_routing
from swarm.agents import get_model


@pytest.fixture(autouse=True)
async def _fresh_model_routing_cache(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()
    await model_routing.reset_cache_for_tests()
    await model_routing.ensure_cache_loaded()  # triggers the seed against the fresh empty DB
    yield


# ── cloud alias routing ─────────────────────────────────────────────────────────

async def test_cloud_alias_raises_when_allow_cloud_models_is_false(monkeypatch):
    monkeypatch.setattr(config, "allow_cloud_models", False)

    with pytest.raises(RuntimeError, match="ALLOW_CLOUD_MODELS"):
        get_model("claude-sonnet-cloud", "http://ollama-host")


async def test_cloud_alias_error_names_the_requested_model(monkeypatch):
    monkeypatch.setattr(config, "allow_cloud_models", False)

    with pytest.raises(RuntimeError, match="gpt-4o-cloud"):
        get_model("gpt-4o-cloud", "http://ollama-host")


async def test_cloud_alias_succeeds_when_allow_cloud_models_is_true(monkeypatch):
    monkeypatch.setattr(config, "allow_cloud_models", True)
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("claude-sonnet-cloud", "http://ollama-host")

    assert model.id == "claude-sonnet-cloud"
    assert model.base_url == "http://litellm-host:4000/v1"
    assert model.api_key == "EMPTY"


async def test_cloud_alias_routes_the_same_regardless_of_inference_backend(monkeypatch):
    """Cloud routing is a per-agent choice (which model_id a team YAML names), not a
    global backend switch -- it must resolve the same way whether the rest of the
    swarm is running INFERENCE_BACKEND=ollama or =vllm."""
    monkeypatch.setattr(config, "allow_cloud_models", True)
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    monkeypatch.setattr(config, "inference_backend", "ollama")
    via_ollama_backend = get_model("sonar-pro-cloud", "http://ollama-host")

    monkeypatch.setattr(config, "inference_backend", "vllm")
    via_vllm_backend = get_model("sonar-pro-cloud", "http://ollama-host")

    assert via_ollama_backend.id == via_vllm_backend.id == "sonar-pro-cloud"
    assert via_ollama_backend.base_url == via_vllm_backend.base_url == "http://litellm-host:4000/v1"


async def test_all_seeded_cloud_models_are_gated(monkeypatch):
    """Every model_catalog row seeded with kind='cloud' must actually hit the gate --
    guards against a future seed entry added without requires_cloud_gate=True."""
    monkeypatch.setattr(config, "allow_cloud_models", False)

    cloud_ids = [mid for mid, route in model_routing._route_cache.items() if route.kind == "cloud"]
    assert cloud_ids, "expected at least one seeded cloud model"
    for model_id in cloud_ids:
        with pytest.raises(RuntimeError, match="ALLOW_CLOUD_MODELS"):
            get_model(model_id, "http://ollama-host")


async def test_inactive_route_falls_back_to_unregistered_behavior(monkeypatch):
    """A model_catalog row marked active=False must behave exactly like an
    unregistered id -- a soft-disable, not a delete, but still inert for routing."""
    import sqlalchemy as sa

    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    async with db.get_routing_engine().begin() as conn:
        await conn.execute(
            sa.update(db.model_catalog)
            .where(db.model_catalog.c.model_id == "qwen2.5-coder:32b")
            .values(active=False)
        )
    await model_routing.load_cache()

    model = get_model("qwen2.5-coder:32b", "http://ollama-host")
    assert model.id == "qwen2.5-coder-32b"  # dash-mangled passthrough, NOT the consolidation target


# ── regression: pre-existing vLLM / Ollama branches, unaffected by the cloud check ──

async def test_non_cloud_model_id_with_vllm_backend_unaffected_by_allow_cloud_models(monkeypatch):
    """A model id that ISN'T a cloud alias must behave identically regardless of
    ALLOW_CLOUD_MODELS -- the gate only ever applies to requires_cloud_gate rows."""
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr(config, "allow_cloud_models", False)

    model = get_model("qwen2.5-coder:32b", "http://ollama-host")

    assert model.id == "local-shared"  # collapsed via the seeded ALL-MoE consolidation row
    assert model.base_url == "http://litellm-host:4000/v1"


async def test_unmapped_model_id_with_vllm_backend_falls_back_to_dash_mangling(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("some-unmapped:tag", "http://ollama-host")

    assert model.id == "some-unmapped-tag"


async def test_vllm_backend_returns_vllm_tool_fix(monkeypatch):
    """2026-08-15: get_model()'s vLLM branch used plain OpenAILike until a live,
    reproducible incident showed vLLM's own tool-call parser doesn't always
    extract a model's raw <tool_call> text into a structured delta -- see
    swarm/tool_fix.py's own module docstring. VLLMToolFix (same recovery logic
    as OllamaToolFix, shared via a mixin) must be what get_model() actually
    returns on this path now, not stock OpenAILike."""
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host")

    assert type(model).__name__ == "VLLMToolFix"


async def test_cloud_route_still_returns_plain_openai_like_not_vllm_tool_fix(monkeypatch):
    """The vLLM tool-call-text quirk is a local open-weight model training
    artifact (Hermes-format prompting) -- a real cloud provider's own native
    tool-calling is reliable and structured. VLLMToolFix must stay scoped to
    the local vLLM branch only; the cloud route is deliberately untouched."""
    monkeypatch.setattr(config, "allow_cloud_models", True)
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("claude-sonnet-cloud", "http://ollama-host")

    assert type(model).__name__ == "OpenAILike"


async def test_ollama_backend_returns_ollama_tool_fix(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "ollama")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host:11434")

    assert type(model).__name__ == "OllamaToolFix"
    assert model.id == "qwen2.5-coder:32b"


# ── temperature (2026-08-10): pinned on the coordinator to reduce run-to-run ────
# inconsistency in which decision path it takes -- see config.py's
# coordinator_temperature docstring for the live evidence motivating this.

async def test_temperature_defaults_to_none_when_not_passed(monkeypatch):
    """A caller that doesn't pass temperature must be completely unaffected -- this
    is get_model()'s own backward-compat guarantee. (Historical note: until
    2026-08-12, member agents -- make_coder, make_researcher, etc. -- were exactly
    this "doesn't pass it" case, which is what let the coordinator's own
    repetition-loop fix, config.coordinator_temperature, leave every member agent
    unprotected. They now pass config.member_temperature/coder_max_tokens
    explicitly -- see config.py's member_temperature docstring.)"""
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host")

    assert model.temperature is None


async def test_temperature_is_passed_through_on_vllm_backend(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host", temperature=0.2)

    assert model.temperature == 0.2


async def test_temperature_is_passed_through_on_cloud_route(monkeypatch):
    monkeypatch.setattr(config, "allow_cloud_models", True)
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("claude-sonnet-cloud", "http://ollama-host", temperature=0.2)

    assert model.temperature == 0.2


# ── max_tokens (2026-08-10): output-length cap on the coordinator, to bound a ────
# single completion that never emits a stop token -- see config.py's
# coordinator_max_tokens docstring for the live py-spy evidence motivating this.

async def test_max_tokens_defaults_to_none_when_not_passed(monkeypatch):
    """Same backward-compat guarantee as temperature -- member agents don't pass
    max_tokens and must see the exact previous (unbounded) behavior."""
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host")

    assert model.max_tokens is None


async def test_max_tokens_is_passed_through_on_vllm_backend(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host", max_tokens=4096)

    assert model.max_tokens == 4096


async def test_max_tokens_is_passed_through_on_cloud_route(monkeypatch):
    monkeypatch.setattr(config, "allow_cloud_models", True)
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("claude-sonnet-cloud", "http://ollama-host", max_tokens=4096)

    assert model.max_tokens == 4096


async def test_temperature_and_max_tokens_both_pass_through_together(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host", temperature=0.2, max_tokens=4096)

    assert model.temperature == 0.2
    assert model.max_tokens == 4096


# ── frequency_penalty (2026-08-10): repetition penalty on the coordinator, to ────
# discourage the exact failure mode confirmed live that day -- the model deciding
# on a plan and then re-generating it verbatim instead of calling a tool. See
# config.py's coordinator_frequency_penalty docstring for the live evidence.

async def test_frequency_penalty_defaults_to_none_when_not_passed(monkeypatch):
    """Same backward-compat guarantee as temperature/max_tokens -- member agents
    don't pass frequency_penalty and must see the exact previous behavior."""
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host")

    assert model.frequency_penalty is None


async def test_frequency_penalty_is_passed_through_on_vllm_backend(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host", frequency_penalty=0.4)

    assert model.frequency_penalty == 0.4


async def test_frequency_penalty_is_passed_through_on_cloud_route(monkeypatch):
    monkeypatch.setattr(config, "allow_cloud_models", True)
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("claude-sonnet-cloud", "http://ollama-host", frequency_penalty=0.4)

    assert model.frequency_penalty == 0.4


async def test_temperature_max_tokens_and_frequency_penalty_all_pass_through_together(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model(
        "qwen2.5-coder:32b", "http://ollama-host",
        temperature=0.2, max_tokens=4096, frequency_penalty=0.4,
    )

    assert model.temperature == 0.2
    assert model.max_tokens == 4096
    assert model.frequency_penalty == 0.4


# ── repetition_penalty / extra_body (T1-T13 gap #1 follow-up, 2026-08-16) ────────
# Not a native OpenAIChat field -- only reachable through extra_body (confirmed
# live against vllm-coord's own /openapi.json, 2026-08-16: repetition_penalty IS
# a supported ChatCompletionRequest field on this vLLM version; no_repeat_ngram_size
# is NOT). See get_model()'s own docstring for the full rationale.

async def test_repetition_penalty_unset_omits_extra_body_on_vllm_backend(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host")

    assert model.extra_body is None


async def test_repetition_penalty_is_passed_through_extra_body_on_vllm_backend(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host", repetition_penalty=1.1)

    assert model.extra_body == {"repetition_penalty": 1.1}


async def test_repetition_penalty_is_never_passed_to_the_cloud_route(monkeypatch):
    """repetition_penalty is a vLLM-native SamplingParams field -- confirmed
    only against this project's own served vLLM model, not something an
    arbitrary third-party cloud provider is known to support. The cloud
    branch's OpenAILike construction deliberately never receives it."""
    monkeypatch.setattr(config, "allow_cloud_models", True)
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("claude-sonnet-cloud", "http://ollama-host", repetition_penalty=1.1)

    assert not hasattr(model, "extra_body") or model.extra_body is None


async def test_all_sampling_params_pass_through_together_including_repetition_penalty(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model(
        "qwen2.5-coder:32b", "http://ollama-host",
        temperature=0.2, max_tokens=4096, frequency_penalty=0.4, repetition_penalty=1.1,
    )

    assert model.temperature == 0.2
    assert model.max_tokens == 4096
    assert model.frequency_penalty == 0.4
    assert model.extra_body == {"repetition_penalty": 1.1}


# ── model_request_timeout_s (2026-08-18 live incident, T6/T12) ──────────────────
# Neither the vLLM nor cloud branch passed agno's `timeout` kwarg through to the
# underlying openai-python client, so a completion request whose response stream
# went silent server-side had nothing to time it out at the HTTP layer -- a live
# py-spy dump of the actually-stalled worker process showed the main thread
# genuinely idle in `select()`, not a Python-level deadlock, only ever ended by
# api/server.py's 300s liveness auto-kill SIGKILLing the whole process. Unlike
# temperature/max_tokens/frequency_penalty above, this one is NOT caller-optional
# -- every model construction must always carry SOME timeout, so there's no
# "defaults to None when not passed" test here; config.model_request_timeout_s
# always has a value (env-overridable, default 120s).

async def test_model_request_timeout_is_passed_through_on_vllm_backend(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr(config, "model_request_timeout_s", 120.0)

    model = get_model("qwen2.5-coder:32b", "http://ollama-host")

    assert model.timeout == 120.0


async def test_model_request_timeout_is_passed_through_on_cloud_route(monkeypatch):
    monkeypatch.setattr(config, "allow_cloud_models", True)
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr(config, "model_request_timeout_s", 120.0)

    model = get_model("claude-sonnet-cloud", "http://ollama-host")

    assert model.timeout == 120.0


async def test_model_request_timeout_respects_config_override(monkeypatch):
    """Confirms the value actually flows from config, not a hardcoded literal in
    get_model() itself -- a deployment can tune this via MODEL_REQUEST_TIMEOUT_S
    without a code change."""
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr(config, "model_request_timeout_s", 45.0)

    model = get_model("qwen2.5-coder:32b", "http://ollama-host")

    assert model.timeout == 45.0
