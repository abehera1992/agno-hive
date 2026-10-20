"""T39 Priority B (secondary, independent finding) -- tracing the T13a-T37-LIVE1
incident's exact call sequence (ZGX journalctl recovery) found a `[budget]
Researcher: call 79/80` log line -- the REAL, agno-enforced tool_call_limit for
the Researcher role in the engineering team was 80 that run, and the model was
correctly force-text-only'd one call before it, exactly as the budget guard hook
(swarm/team.py, _make_tool_budget_guard_hook) is designed to do.

But every T37/T38 telemetry record for the SAME run reported `tool_call_limit: 25`
-- the global config default, not the real DB-configured 80. Tracing both call
sites to swarm/model_routing.py's `_resolve_tool_call_limit` -> `get_role_policy`
found the cause: `_role_policy_cache` is a plain `dict[(team_name, role_name),
RolePolicy]` populated from the DB with role_name in its natural casing
('Researcher', per swarm/db.py's own column comment). The budget guard hook
resolves correctly because it is constructed with `role=spec.name` ('Researcher',
_hooks_for(spec.name), team.py ~17579). phase0.record_member_result's own call
site resolves the SAME role through `_member_key()` first (team.py ~19729,
`_who = _member_key(out.get("agent_name", ""))`), which lowercases and strips
separators -- 'researcher'. The exact-string dict lookup on 'researcher' misses
the DB cache entry keyed by 'Researcher', silently falls through to
config.tool_call_limit (25), and reports a WRONG limit for telemetry purposes
only -- real enforcement was correct throughout.

This means the T37 incident was NEVER an 80-call-ceiling violation: the model
legitimately used 79 of its real 80-call budget. The appearance of "79 vs 25" was
a telemetry case-sensitivity bug, not a budget defect. Fixed with a SEPARATE
lowercase-keyed fallback index (_role_policy_cache_ci) consulted only when the
exact-case lookup misses -- _role_policy_cache's own keys keep the DB's natural
casing untouched, so reload()'s admin-facing diff output (which derives its
"team/role" display strings directly from that cache's keys) is unaffected.
"""
from swarm import model_routing


def _seed(team_name: str, role_name: str, tool_call_limit: int) -> None:
    """Mirrors load_cache()'s own real population of BOTH caches from one row."""
    model_routing._role_policy_cache.clear()
    model_routing._role_policy_cache_ci.clear()
    policy = model_routing.RolePolicy(
        model_id="qwen2.5-coder:32b", temperature=None, max_tokens=None,
        tool_call_limit=tool_call_limit,
    )
    model_routing._role_policy_cache[(team_name, role_name)] = policy
    model_routing._role_policy_cache_ci[
        model_routing._ci_role_key(team_name, role_name)
    ] = policy


def teardown_function(_fn):
    model_routing._role_policy_cache.clear()
    model_routing._role_policy_cache_ci.clear()


def test_db_seeded_role_name_casing_is_found_by_its_own_exact_casing():
    _seed("engineering", "Researcher", 80)
    policy = model_routing.get_role_policy("engineering", "Researcher")
    assert policy is not None
    assert policy.tool_call_limit == 80


def test_lowercased_role_name_still_finds_the_same_db_row():
    """THE FIX: phase0's own call site passes a _member_key()-lowercased role
    name ('researcher'). Before the fix this missed the cache entirely."""
    _seed("engineering", "Researcher", 80)
    policy = model_routing.get_role_policy("engineering", "researcher")
    assert policy is not None, (
        "a lowercased role_name must still resolve to the DB-seeded policy -- "
        "this is the exact T13a-T37-LIVE1 incident's own telemetry defect"
    )
    assert policy.tool_call_limit == 80


def test_get_default_model_is_also_case_insensitive():
    _seed("engineering", "Researcher", 80)
    assert model_routing.get_default_model("engineering", "researcher") == "qwen2.5-coder:32b"
    assert model_routing.get_default_model("engineering", "RESEARCHER") == "qwen2.5-coder:32b"
    assert model_routing.get_default_model("engineering", "Researcher") == "qwen2.5-coder:32b"


def test_the_exact_t37_incident_shape_end_to_end():
    """Reproduces the real incident's exact two call sites side by side: the
    budget guard hook's own resolution (correctly-cased 'Researcher', from
    spec.name) and phase0's telemetry resolution (lowercased via _member_key)
    must now agree."""
    _seed("engineering", "Researcher", 80)
    guard_hook_limit = model_routing.get_role_policy("engineering", "Researcher").tool_call_limit
    telemetry_limit = model_routing.get_role_policy("engineering", "researcher").tool_call_limit
    assert guard_hook_limit == telemetry_limit == 80, (
        "both call sites must resolve the SAME real limit for the SAME role -- "
        "a mismatch here is exactly what made T13a-T37-LIVE1's telemetry report "
        "'tool_call_limit: 25' while agno's own real enforcement correctly used 80"
    )


def test_a_role_with_no_db_override_still_falls_back_to_config_default():
    """Negative control: a role genuinely absent from the DB (no row at all)
    must still fall through to config.tool_call_limit, regardless of casing --
    the fix must not invent a false positive for an unconfigured role."""
    model_routing._role_policy_cache.clear()
    model_routing._role_policy_cache_ci.clear()
    assert model_routing.get_role_policy("engineering", "NobodyHome") is None
    assert model_routing.get_role_policy("engineering", "nobodyhome") is None


def test_team_name_is_not_altered_by_the_fix():
    """The fix only normalizes role_name -- team_name casing/spelling is a
    separate dimension and must still require an exact match, since every
    team_name observed in this codebase is already consistently lowercase and
    nothing here claims that axis is broken."""
    _seed("engineering", "Researcher", 80)
    assert model_routing.get_role_policy("Engineering", "Researcher") is None
