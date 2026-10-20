"""T41 Track A (F5) -- _objective_names_new_entity (swarm/team.py) recognizes a
genuinely new CamelCase/snake_case identifier (e.g. 'Party' -> 'PartyRegistration')
but has no mechanism at all for recognizing a genuine SUBJECT change expressed in
ordinary English (e.g. 'list the backend endpoints' -> 'list the frontend hooks').

Prior art this fix must respect (swarm/team.py's own documented history,
_normalize_delegation_task's docstring and the comment block above
_make_duplicate_delegation_gate_hook): generic prose similarity (SequenceMatcher,
Jaccard) was tried and EMPIRICALLY RULED OUT for this exact gate, specifically
because it conflates two phrasings that are lexically very similar but refer to
different targets ('what fields does Party have' vs 'what fields does
PartyRegistration have'). This fix is deliberately NOT a continuous-similarity
score over raw prose -- it is a small, fixed, categorical vocabulary lookup
(structured, like the target/action tuple the codebase's own history already
prefers over fuzzy text matching), scoped to recognizing a handful of concretely
different SUBJECT AREAS this gate's own real objectives fall into (endpoints,
hooks, tables, models, migrations, auth, config, upload, import, tests) -- never
a general semantic-similarity engine, and never a replacement for the existing
identifier-shape check, which stays exactly as it was.

Categories A-H below match T41's own mission spec. Each test documents its own
expected classification and why.
"""
from swarm.team import _objective_names_new_entity, _make_duplicate_delegation_gate_hook


async def _fake_delegate(**kwargs):
    return f"delegated: {kwargs}"


def _args(member_id="researcher", target="vouchers_api.py", objective="list endpoints"):
    return {
        "member_id": member_id, "target": target, "objective": objective,
        "evidence_required": "evidence", "completion_criteria": "done",
    }


# ── Category A: genuine subject changes, expressed in plain English ────────────

def test_backend_endpoints_to_frontend_hooks_is_a_new_entity():
    assert _objective_names_new_entity(
        "List the frontend hooks used by the vouchers module.",
        "List all endpoints in the vouchers module backend.",
    ) is True


def test_authentication_middleware_to_database_migrations_is_a_new_entity():
    assert _objective_names_new_entity(
        "Review the database migrations for the inventory service.",
        "Review the authentication middleware for the inventory service.",
    ) is True


def test_document_upload_to_inventory_import_is_a_new_entity():
    assert _objective_names_new_entity(
        "Trace the inventory import session handling.",
        "Trace the seller document upload handling.",
    ) is True


def test_route_registration_to_config_validation_is_a_new_entity():
    assert _objective_names_new_entity(
        "Check the configuration validation logic.",
        "Check the API route registration logic.",
    ) is True


# ── Category B: same subject, different wording -- must NOT become false progress ─

def test_endpoints_vs_api_routes_same_subject_is_not_a_new_entity():
    """'Endpoints' and 'API routes' are synonyms in this codebase's own domain
    vocabulary (confirmed by its own battery prompts using both terms
    interchangeably for the same real subject) -- a rewording, not a new
    question."""
    assert _objective_names_new_entity(
        "Review the server-side API routes for the vouchers module.",
        "Inspect the backend endpoints for the vouchers module.",
    ) is False


def test_hooks_synonym_rewording_is_not_a_new_entity():
    assert _objective_names_new_entity(
        "Enumerate every hook declaration used by the page.",
        "List the frontend hooks used by the page.",
    ) is False


def test_tables_synonym_rewording_is_not_a_new_entity():
    assert _objective_names_new_entity(
        "List the database schema columns for this module.",
        "Identify the database tables used by this module.",
    ) is False


# ── Category C: identifier-based behavior (existing, must remain intact) ───────

def test_camelcase_entity_change_is_still_a_new_entity():
    assert _objective_names_new_entity(
        "What fields does PartyRegistration have?",
        "What fields does Party have?",
    ) is True


def test_snake_case_entity_change_is_still_a_new_entity():
    assert _objective_names_new_entity(
        "List the voucher_series table columns.",
        "List the stock_ledger table columns.",
    ) is True


def test_same_wording_reworded_endpoint_question_is_not_a_new_entity():
    """The pre-existing, already-covered case: a plain rewording naming no new
    symbol and no new category must stay a duplicate."""
    assert _objective_names_new_entity(
        "enumerate every endpoint it exposes",
        "list its endpoints",
    ) is False


# ── Category D: filler and reformulation ────────────────────────────────────────

def test_added_introductory_prose_is_not_a_new_entity():
    assert _objective_names_new_entity(
        "Please go ahead and list all the endpoints in the vouchers module backend, thanks.",
        "List all endpoints in the vouchers module backend.",
    ) is False


def test_reordered_words_same_subject_is_not_a_new_entity():
    assert _objective_names_new_entity(
        "In the vouchers module backend, list all the endpoints.",
        "List all endpoints in the vouchers module backend.",
    ) is False


def test_exact_repeat_with_punctuation_change_is_not_a_new_entity():
    assert _objective_names_new_entity(
        "List all endpoints in the vouchers module backend!!",
        "List all endpoints in the vouchers module backend.",
    ) is False


# ── Category E: partial overlap / broad-to-narrow / ambiguous subjects ─────────

def test_broad_to_narrow_within_the_same_category_is_not_a_new_entity_via_this_mechanism():
    """A narrower follow-up that stays within the SAME recognized category
    (endpoints) must not be flagged as a new entity by the category mechanism
    -- the existing identifier check is what catches a genuinely new symbol
    inside a narrower follow-up, not this one. Uses no CamelCase/snake_case
    symbol so this isolates the category layer specifically."""
    assert _objective_names_new_entity(
        "List only the endpoints that create or modify a voucher.",
        "List all endpoints in the vouchers module backend.",
    ) is False


def test_ambiguous_subject_with_no_recognized_category_is_conservative():
    """Neither phrase matches any known category keyword -- the mechanism
    must stay silent (False) rather than guess, falling through to the
    existing identifier-only behavior."""
    assert _objective_names_new_entity(
        "Explain what this module is responsible for overall.",
        "Summarize the purpose of this part of the system.",
    ) is False


def test_one_side_categorized_other_side_not_is_conservative():
    """Only one phrase names a recognized category; the mechanism must not
    assume a change it cannot actually establish on both sides."""
    assert _objective_names_new_entity(
        "List the frontend hooks used by the page.",
        "Describe how this part of the system behaves.",
    ) is False


# ── Category F: non-code, ordinary natural-language subjects ───────────────────

def test_plain_language_domain_change_without_any_identifier():
    assert _objective_names_new_entity(
        "Summarize the seller onboarding and verification flow.",
        "Summarize the authentication middleware configuration.",
    ) is True


# ── Category G: empty / low-information input ──────────────────────────────────

def test_empty_objective_is_never_a_new_entity():
    assert _objective_names_new_entity("", "list its endpoints") is False


def test_both_empty_is_never_a_new_entity():
    assert _objective_names_new_entity("", "") is False


def test_generic_filler_only_text_is_not_a_new_entity():
    assert _objective_names_new_entity("please help with this", "please help with that") is False


# ── Category H: the real production caller/decision path, not the helper alone ──

async def test_caller_blocks_a_same_category_rewording_as_a_duplicate():
    """The gate's own decision: a same-subject rewording (via the new category
    layer) must still be served ALREADY DONE, exactly as a plain rewording
    already was before this fix."""
    from types import SimpleNamespace
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={"researcher": "ENDPOINTS RESULT"})

    first = await hook("delegate_structured_task", _fake_delegate,
                       _args(objective="Inspect the backend endpoints for the vouchers module."),
                       run_context=None, team=team)
    second = await hook("delegate_structured_task", _fake_delegate,
                        _args(objective="Review the server-side API routes for the vouchers module."),
                        run_context=None, team=team)

    assert first.startswith("delegated:")
    assert second.startswith("ALREADY DONE")


async def test_caller_allows_a_genuine_category_change_to_run_fresh():
    """The gate's own decision: a GENUINE subject change (endpoints -> hooks)
    against the same target must be allowed to run fresh, not blocked/served
    the wrong prior result -- this is the actual F5 consequence: without this
    fix, a same-target plain-English subject change with no prior result
    captured yet is still allowed through today (via the 'no usable prior
    result' branch), but ONCE a prior result DOES exist, the gate's only
    recourse without this fix is the identifier check, which cannot tell
    'endpoints' apart from 'hooks' -- so a later repeat of the hooks question
    would (pre-fix) risk being served the endpoints answer instead."""
    from types import SimpleNamespace
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={"researcher": "ENDPOINTS RESULT"})

    await hook("delegate_structured_task", _fake_delegate,
              _args(objective="List all endpoints in the vouchers module backend."),
              run_context=None, team=team)
    second = await hook("delegate_structured_task", _fake_delegate,
                        _args(objective="List the frontend hooks used by the vouchers module."),
                        run_context=None, team=team)

    assert second.startswith("delegated:"), (
        "a genuine subject change (endpoints -> hooks) against the same "
        f"target must run fresh, not be blocked/served the wrong prior "
        f"result -- got: {second!r}"
    )
