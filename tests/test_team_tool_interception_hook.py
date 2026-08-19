"""Unit tests for the tool_hooks interception checkpoint (AGNOHive 2.3.1 Phase 9a).

Async + shared across the coordinator AND every member agent, for the same two
mechanical facts _make_read_cache_tool_hook depends on (verified by reading
agno's actual source, see that hook's docstring): every MCP-server-backed tool
call is async on the client side unconditionally, and in mode="coordinate" the
coordinator mostly delegates -- a coordinator-only hook misses the member
agents' own tool calls.

Deliberately NOT wired to Phase 7's client-side `_steering_queue` (cli/hive) --
that queue lives in the user's machine's CLI process, this hook runs
server-side in swarm/team.py. There is no existing mid-run client<->server
channel connecting the two; abort_event is a reusable building block a future
caller can wire up, not something already connected to steering. See
_make_tool_interception_hook's docstring for the full scoping note.
"""
import asyncio
from types import SimpleNamespace

import pytest

from swarm.team import _make_tool_interception_hook, _build_team, ToolCallAborted


def _fake_mcp(functions: dict):
    """A minimal stand-in for a connected MCPTools server -- _scope_coordinator_tools
    only ever reads `.functions` (a dict of name -> Function-like object) off it."""
    return SimpleNamespace(functions=functions)


# ── _make_tool_interception_hook: pass-through / audit behavior (abort_event=None) ──

@pytest.mark.asyncio
async def test_hook_calls_through_and_returns_the_result_when_no_abort_event():
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return kwargs["x"] * 2

    result = await hook("double", fake_tool, {"x": 21})

    assert result == 42


@pytest.mark.asyncio
async def test_hook_prints_a_trace_line_on_success(capsys):
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "ok"

    await hook("my_tool", fake_tool, {"x": 1})

    out = capsys.readouterr().out
    assert "my_tool" in out


@pytest.mark.asyncio
async def test_hook_still_prints_and_reraises_on_a_failing_tool(capsys):
    hook = _make_tool_interception_hook()

    async def failing_tool(**kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await hook("bad_tool", failing_tool, {})

    out = capsys.readouterr().out
    assert "bad_tool" in out


# ── _make_tool_interception_hook: abort behavior (abort_event set) ─────────────

@pytest.mark.asyncio
async def test_hook_calls_through_when_abort_event_exists_but_is_not_set():
    abort_event = asyncio.Event()
    hook = _make_tool_interception_hook(abort_event=abort_event)
    calls = []

    async def fake_tool(**kwargs):
        calls.append(kwargs)
        return "ran"

    result = await hook("some_tool", fake_tool, {"x": 1})

    assert result == "ran"
    assert calls == [{"x": 1}]


@pytest.mark.asyncio
async def test_hook_skips_the_call_and_raises_when_abort_event_is_set():
    abort_event = asyncio.Event()
    abort_event.set()
    hook = _make_tool_interception_hook(abort_event=abort_event)
    calls = []

    async def fake_tool(**kwargs):
        calls.append(kwargs)  # must never run
        return "ran"

    with pytest.raises(ToolCallAborted):
        await hook("some_tool", fake_tool, {"x": 1})

    assert calls == []  # the underlying tool was never invoked


@pytest.mark.asyncio
async def test_hook_prints_an_aborted_trace_line(capsys):
    abort_event = asyncio.Event()
    abort_event.set()
    hook = _make_tool_interception_hook(abort_event=abort_event)

    async def fake_tool(**kwargs):
        return "ran"

    with pytest.raises(ToolCallAborted):
        await hook("some_tool", fake_tool, {})

    out = capsys.readouterr().out
    assert "some_tool" in out
    assert "ABORTED" in out


# ── _build_team wiring: coordinator_temperature reaches the coordinator's model ────
# (2026-08-10) -- the coordinator gets its OWN tuned value, coordinator_temperature,
# never member_temperature; see config.py's coordinator_temperature docstring.

def test_build_team_passes_coordinator_temperature_to_the_coordinator_model(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "vllm")
    monkeypatch.setattr("swarm.team.config.vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr("swarm.team.config.coordinator_temperature", 0.3)

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert result.model.temperature == 0.3


def test_build_team_applies_member_temperature_not_coordinator_temperature_to_member_agents(monkeypatch):
    """Confirmed live 2026-08-12: member agents (the fallback path here is
    make_coder + make_reviewer, per _build_team's agent_specs=None branch) were left
    on get_model()'s raw temperature=1.0 default because the 2026-08-10 coordinator
    fix was scoped coordinator-only -- a Researcher turn stalled 4+ minutes with the
    exact repetition-loop signature that fix targets. Members now get their own
    config.member_temperature, deliberately a SEPARATE field from
    coordinator_temperature (set to a different value here) so a future
    coordinator-specific retune can't silently also retune every member agent."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "vllm")
    monkeypatch.setattr("swarm.team.config.vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr("swarm.team.config.coordinator_temperature", 0.3)
    monkeypatch.setattr("swarm.team.config.member_temperature", 0.7)

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    for member in result.members:
        assert member.model.temperature == 0.7


# ── _build_team wiring: coordinator_max_tokens reaches the coordinator's model ─────
# (2026-08-10) -- the coordinator gets its OWN tuned value, coordinator_max_tokens,
# never member_max_tokens/coder_max_tokens; see config.py's coordinator_max_tokens
# docstring.

def test_build_team_passes_coordinator_max_tokens_to_the_coordinator_model(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "vllm")
    monkeypatch.setattr("swarm.team.config.vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr("swarm.team.config.coordinator_max_tokens", 4096)

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert result.model.max_tokens == 4096


def test_build_team_applies_member_or_coder_max_tokens_not_coordinator_max_tokens(monkeypatch):
    """Same live incident as the temperature test above. member.max_tokens must be
    config.member_max_tokens (Reviewer) or config.coder_max_tokens (Coder) -- never
    coordinator_max_tokens and never None -- distinct, separately-tunable fields set
    to three different values here specifically to prove none of them alias."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "vllm")
    monkeypatch.setattr("swarm.team.config.vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr("swarm.team.config.coordinator_max_tokens", 4096)
    monkeypatch.setattr("swarm.team.config.member_max_tokens", 2048)
    monkeypatch.setattr("swarm.team.config.coder_max_tokens", 8192)

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    by_name = {member.name: member for member in result.members}
    assert by_name["Coder"].model.max_tokens == 8192
    assert by_name["Reviewer"].model.max_tokens == 2048


# ── _build_team wiring: coordinator_frequency_penalty reaches the coordinator's model ──
# (2026-08-10) -- the coordinator gets its OWN tuned value, coordinator_frequency_penalty,
# never member_frequency_penalty; see config.py's coordinator_frequency_penalty docstring.

def test_build_team_passes_coordinator_frequency_penalty_to_the_coordinator_model(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "vllm")
    monkeypatch.setattr("swarm.team.config.vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr("swarm.team.config.coordinator_frequency_penalty", 0.4)

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert result.model.frequency_penalty == 0.4


def test_build_team_applies_member_frequency_penalty_not_coordinator_frequency_penalty(monkeypatch):
    """Same live incident as the two tests above."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "vllm")
    monkeypatch.setattr("swarm.team.config.vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr("swarm.team.config.coordinator_frequency_penalty", 0.4)
    monkeypatch.setattr("swarm.team.config.member_frequency_penalty", 0.1)

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    for member in result.members:
        assert member.model.frequency_penalty == 0.1


# ── _build_team wiring: interception hook shared across coordinator AND every member ──

def test_build_team_registers_the_interception_hook_alongside_the_cache_hook(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    # interception hook + search-before-browse gate hook (2026-08-15) + read-cache
    # hook + decompose-first gate hook (Engineering Team 2.0 Phase 2, 2026-08-14) +
    # duplicate-delegation gate hook (2026-08-15, T2c groundedness incident) +
    # delegation-log hook (shared session_state, 2026-08-13) -- six, not five, as
    # of the duplicate-delegation gate addition.
    assert len(result.tool_hooks) == 6
    # interception hook is listed FIRST (2026-08-11: order changed deliberately so it
    # is the OUTERMOST wrapper -- agno reduces hooks from the innermost outward, so
    # hooks[0] wraps everything else. This makes it always run and always log, even
    # when read_cache_hook returns early on a cache hit or duplicate-read stub. See
    # _build_team's own comment at the tool_hooks assignment for the live incident
    # this fixes -- test_team_read_cache_hook.py has the fuller regression test).
    assert result.tool_hooks[0].__name__ == "_tool_interception_hook"


def test_build_team_shares_the_same_interception_hook_instance_with_fallback_members(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    interception_hook = result.tool_hooks[0]
    for member in result.members:
        assert member.tool_hooks[0] is interception_hook


# ── _make_tool_interception_hook: shared activity tracker (wander-then-go-quiet diagnostics) ──

@pytest.mark.asyncio
async def test_hook_updates_activity_dict_on_success():
    activity = {"last_call_name": None, "last_call_at": 0.0}
    hook = _make_tool_interception_hook(activity=activity)

    async def fake_tool(**kwargs):
        return "ok"

    await hook("my_tool", fake_tool, {})

    assert activity["last_call_name"] == "my_tool"
    assert activity["last_call_at"] > 0.0


@pytest.mark.asyncio
async def test_hook_updates_activity_dict_on_failure_too():
    activity = {"last_call_name": None, "last_call_at": 0.0}
    hook = _make_tool_interception_hook(activity=activity)

    async def failing_tool(**kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await hook("bad_tool", failing_tool, {})

    assert activity["last_call_name"] == "bad_tool"
    assert activity["last_call_at"] > 0.0


@pytest.mark.asyncio
async def test_hook_updates_last_progress_at_on_success():
    """2026-08-19 fix -- see _make_tool_interception_hook's own docstring addendum
    and DOCS.md's "7-Test Groundedness Battery" section. Before this fix, a
    delegated member tool call only ever touched last_call_at, never
    last_progress_at -- the field _run_heartbeat's is_stagnant check actually
    reads. Live: 3/3 long-running, real, successful research delegations got
    killed by the liveness watchdog as "stagnant for 300s" despite fresh
    get_file_content/search_files_batch calls seconds earlier, because none of
    that activity ever reached last_progress_at."""
    activity = {"last_call_name": None, "last_call_at": 0.0, "last_progress_at": 0.0}
    hook = _make_tool_interception_hook(activity=activity)

    async def fake_tool(**kwargs):
        return "ok"

    await hook("get_file_content", fake_tool, {})

    assert activity["last_progress_at"] > 0.0


@pytest.mark.asyncio
async def test_hook_updates_last_progress_at_on_failure_too():
    """Symmetric with last_call_at's existing on-failure update -- a delegated
    call that fails still proves the process is alive and doing something, the
    same reasoning already applied to last_call_at on this path."""
    activity = {"last_call_name": None, "last_call_at": 0.0, "last_progress_at": 0.0}
    hook = _make_tool_interception_hook(activity=activity)

    async def failing_tool(**kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await hook("bad_tool", failing_tool, {})

    assert activity["last_progress_at"] > 0.0


@pytest.mark.asyncio
async def test_hook_still_works_when_activity_dict_lacks_last_progress_at_key():
    """A caller's activity dict predating this fix (e.g. an older test's literal
    {"last_call_name": None, "last_call_at": 0.0}) must not crash the hook --
    the hook only ever assigns the key, never reads it first."""
    activity = {"last_call_name": None, "last_call_at": 0.0}
    hook = _make_tool_interception_hook(activity=activity)

    async def fake_tool(**kwargs):
        return "ok"

    await hook("my_tool", fake_tool, {})

    assert activity["last_progress_at"] > 0.0


@pytest.mark.asyncio
async def test_hook_works_fine_when_no_activity_dict_supplied():
    hook = _make_tool_interception_hook()  # activity=None default, unchanged behavior

    async def fake_tool(**kwargs):
        return "ok"

    result = await hook("my_tool", fake_tool, {})

    assert result == "ok"


def test_build_team_shares_the_same_interception_hook_instance_with_spec_based_members(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    class _FakeSpec:
        name = "Researcher"
        model = "qwen2.5-coder:32b"
        tools = None
        instructions = ["Research the codebase."]
        role = "Researcher"
        description = "Research specialist."
        skills = None

    result = _build_team(
        agent_specs=[_FakeSpec()],
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    interception_hook = result.tool_hooks[0]
    assert result.members[0].tool_hooks[0] is interception_hook


# ── _build_team wiring: coordinator_no_direct_writes (2026-08-10 experiment) ───────
# Forces the coordinator to delegate implementation instead of writing directly --
# see config.py's coordinator_no_direct_writes docstring for the live evidence
# motivating this (every repetition-loop/stall diagnosed that day showed ONLY the
# coordinator's own TeamRunContent events, never a delegated member's RunContent).

def test_coordinator_no_direct_writes_strips_mutating_tools_from_coordinator(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.coordinator_no_direct_writes", True)
    mcp_list = [_fake_mcp({"apply_diff": "apply_diff", "get_file_content": "get_file_content"})]

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=mcp_list,
        instructions=[],
    )

    assert "apply_diff" not in result.tools
    assert "get_file_content" in result.tools


def test_coordinator_keeps_write_tools_by_default(monkeypatch):
    """Backward compat: coordinator_no_direct_writes defaults to False, so an
    unmodified deployment's coordinator keeps its existing direct write access.

    _scope_coordinator_tools' no-restriction branch now always resolves individual
    functions (rather than returning the raw MCP toolkit object unfiltered) so the
    discovery-tool exclusion (_COORDINATOR_DISCOVERY_TOOLS) can apply uniformly across
    every branch -- see that constant's docstring for why. Non-discovery tools like
    apply_diff/get_file_content are unaffected by this resolution change."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.coordinator_no_direct_writes", False)
    mcp_list = [_fake_mcp({"apply_diff": "apply_diff", "get_file_content": "get_file_content"})]

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=mcp_list,
        instructions=[],
    )

    assert "apply_diff" in result.tools
    assert "get_file_content" in result.tools


def test_coordinator_no_direct_writes_does_not_affect_member_tools(monkeypatch):
    """The flag scopes ONLY the coordinator's own tools -- make_agent_from_spec
    resolves a member's tools independently (from spec.tools), never touching
    _scope_coordinator_tools/coordinator_no_direct_writes at all."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.coordinator_no_direct_writes", True)
    mcp_list = [_fake_mcp({"apply_diff": "apply_diff", "get_file_content": "get_file_content"})]

    class _FakeCoderSpec:
        name = "Coder"
        model = "qwen2.5-coder:32b"
        tools = ["apply_diff", "get_file_content"]
        instructions = ["Implement the change."]
        role = "Coder"
        description = "Implementation specialist."
        skills = None

    result = _build_team(
        agent_specs=[_FakeCoderSpec()],
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=mcp_list,
        instructions=[],
    )

    assert "apply_diff" not in result.tools  # coordinator: stripped
    assert "apply_diff" in result.members[0].tools  # Coder: unaffected


# ── _build_team wiring: discovery-tool exclusion (2026-08-11) ──────────────────────
# Confirmed live: a prose-only "prefer delegating to ContextRouter" instruction had
# zero effect on the coordinator's actual tool-call behavior. This enforces the same
# outcome at the tool surface instead -- see _COORDINATOR_DISCOVERY_TOOLS' docstring.

def _mcp_with_discovery_and_other_tools():
    return [_fake_mcp({
        "find_files": "find_files",
        "search_files": "search_files",
        "list_directory": "list_directory",
        "list_directory_tree": "list_directory_tree",
        "search_knowledge_graph": "search_knowledge_graph",
        "web_search": "web_search",
        "web_fetch": "web_fetch",
        "get_file_content": "get_file_content",
        "apply_diff": "apply_diff",
    })]


_DISCOVERY_TOOL_NAMES = (
    "find_files", "search_files", "list_directory", "list_directory_tree",
    "search_knowledge_graph", "web_search", "web_fetch",
)


def test_coordinator_discovery_tools_stripped_by_default(monkeypatch):
    """No allowlist, not read_only -- the 'preserve existing engineering-team
    behavior' branch. Discovery tools must still be excluded even here."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.coordinator_no_direct_writes", False)
    mcp_list = _mcp_with_discovery_and_other_tools()

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=mcp_list,
        instructions=[],
    )

    for name in _DISCOVERY_TOOL_NAMES:
        assert name not in result.tools
    assert "get_file_content" in result.tools
    assert "apply_diff" in result.tools


def test_coordinator_discovery_tools_stripped_when_read_only(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.coordinator_no_direct_writes", False)
    mcp_list = _mcp_with_discovery_and_other_tools()

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=mcp_list,
        instructions=[],
        read_only=True,
    )

    for name in _DISCOVERY_TOOL_NAMES:
        assert name not in result.tools
    assert "get_file_content" in result.tools
    assert "apply_diff" not in result.tools  # read_only still strips mutating tools too


def test_web_search_and_web_fetch_specifically_are_stripped_from_the_coordinator(monkeypatch):
    """Confirmed live 2026-08-11: AFTER find_files/search_files/etc. were removed
    from the coordinator's own tool surface, a later retest still made ZERO
    delegate_task_to_member calls -- it opened a NEW escape hatch instead,
    web_search('EkamApp frontend codebase GitHub repo') (searching the public web
    for a private, internal codebase's own structure), before falling back to blind
    get_file_content() path guesses. Locked in as its own explicit test, not just
    folded into the generic discovery-tools loop above, since this was a distinct,
    separately-discovered gap in the original fix."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.coordinator_no_direct_writes", False)
    mcp_list = _mcp_with_discovery_and_other_tools()

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=mcp_list,
        instructions=[],
    )

    assert "web_search" not in result.tools
    assert "web_fetch" not in result.tools


def test_web_search_and_web_fetch_stay_available_to_delegated_members(monkeypatch):
    """The capability isn't lost, only moved: ContextRouter/Researcher both carry
    web_search/web_fetch in teams/engineering.yaml for legitimate EXTERNAL research
    (verifying a library name, checking docs) -- removing them from the coordinator
    must not remove them from the team's actual research capability."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.coordinator_no_direct_writes", False)
    mcp_list = _mcp_with_discovery_and_other_tools()

    class _FakeResearcherSpec:
        name = "Researcher"
        model = "qwen2.5-coder:32b"
        tools = ["web_search", "web_fetch", "get_file_content"]
        instructions = ["Research the codebase."]
        role = "Researcher"
        description = "Research specialist."
        skills = None

    result = _build_team(
        agent_specs=[_FakeResearcherSpec()],
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=mcp_list,
        instructions=[],
    )

    assert "web_search" in result.members[0].tools
    assert "web_fetch" in result.members[0].tools


def test_coordinator_discovery_tools_stripped_even_from_an_explicit_allowlist(monkeypatch):
    """A team YAML naming find_files in coordinator_tools should not be able to
    re-grant it -- the exclusion is unconditional, not allowlist-overridable."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.coordinator_no_direct_writes", False)
    mcp_list = _mcp_with_discovery_and_other_tools()

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=["find_files", "get_file_content"],
        mode="coordinate",
        mcp_list=mcp_list,
        instructions=[],
    )

    assert "find_files" not in result.tools
    assert "get_file_content" in result.tools


def test_discovery_tool_exclusion_does_not_affect_member_agents(monkeypatch):
    """Member agents resolve tools independently from their own spec.tools --
    ContextRouter/Researcher must keep full discovery access; only the coordinator's
    own direct surface is scoped."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.coordinator_no_direct_writes", False)
    mcp_list = _mcp_with_discovery_and_other_tools()

    class _FakeContextRouterSpec:
        name = "ContextRouter"
        model = "llama3.1:8b"
        tools = ["find_files", "search_files", "list_directory", "get_file_content"]
        instructions = ["Route to the right context."]
        role = "Routing agent"
        description = "Lightweight query router."
        skills = None

    result = _build_team(
        agent_specs=[_FakeContextRouterSpec()],
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=mcp_list,
        instructions=[],
    )

    assert "find_files" not in result.tools  # coordinator: stripped
    assert "find_files" in result.members[0].tools  # ContextRouter: unaffected
    assert "search_files" in result.members[0].tools
