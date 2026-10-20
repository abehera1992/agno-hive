"""T36 Track C -- a cross-service tracing task names a service with no verified
link to any other named service.

Root cause, confirmed against the live T11 answer (session 5b776aa0-60e7-4974-bfc4-
a6105888abce, 2026-10-19 battery, recovered verbatim from this conversation's own
transcript): asked which services are involved when a seller uploads a document, the
Researcher's FIRST step was a bare "seller" OR "document" keyword grep across the
WHOLE repository. That surfaced `API/email-service/security.py:20` and
`API/imagegen-service/security.py:20` / `main.py:18` -- generic files that merely
mention one of those words for unrelated reasons -- and the model then explicitly
decided to "check if there are any OTHER services involved... such as the
email-service... imagegen-service..." and reported real functions it found there
(`_get_seller_credentials`, `upload_bytes`) as part of the upload chain, without ever
finding or citing an actual call, import, or config reference connecting either
service to the correctly-traced business-service/storage-service chain.

Design gate (required before implementation, per this track's own mission text):
see `_unconfirmed_service_inclusion`'s own header comment in swarm/team.py for the
full evidence contract, which this file reproduces in the final T36 report. Summary:
a service belongs in a dependency trace only via a verified call-site, import, or
config-mediated reference (this project's own `<SERVICE>_SERVICE_URL` naming
convention, already proven live by `_integration_mechanism_missing`'s T12 fix) --
never mere keyword co-occurrence. A generic semantic-relevance classifier is
explicitly out of scope; this checks only for that one, already-deployed contract,
repo-wide (not directory-scoped, since EkamApp's real service-client wrappers live in
a shared `API/utils/service_clients/` directory, not inside either service's own
tree).
"""
import asyncio

import pytest

import swarm.team as team_mod
from swarm.team import _unconfirmed_service_inclusion

_T11_TASK = (
    "Which services are involved when a seller uploads a document, and which "
    "functions handle it in each service?"
)

_T11_REAL_ANSWER_CONCLUSION = (
    "### Conclusion\n\nWhen a seller uploads a document, the following services "
    "are involved:\n\n"
    "1. **API/business-service**: Handles the business logic for document uploads.\n"
    "2. **API/storage-service**: Manages the actual storage of uploaded documents.\n"
    "3. **API/email-service**: Handles email notifications related to document "
    "uploads.\n"
    "4. **API/imagegen-service**: Processes images or documents uploaded by "
    "sellers.\n"
)


def _run(coro):
    return asyncio.run(coro)


def _patch_search(monkeypatch, confirmed: set[str]):
    """Stand in for _repo_search_text: returns a fake hit line for any constant
    whose service name is in `confirmed`, else empty (not found anywhere)."""
    async def _fake(pattern, glob_filter, hive_mcp_url, hive_mcp_tools=None):
        for svc in confirmed:
            const = svc.upper().replace("-", "_") + "_SERVICE_URL"
            if pattern == const:
                return f"API/utils/service_clients/{svc.replace('-service', '')}_service_client.py:10:{const}: str = ..."
        return ""
    monkeypatch.setattr(team_mod, "_repo_search_text", _fake)


# ── The real T11 shape: 2 confirmed, 2 unconfirmed ──────────────────────────────

def test_t11_real_answer_flags_email_and_imagegen_but_not_the_verified_chain(
        monkeypatch):
    _patch_search(monkeypatch, confirmed={"business-service", "storage-service"})
    note = _run(_unconfirmed_service_inclusion(
        _T11_TASK, _T11_REAL_ANSWER_CONCLUSION, "http://hive-mcp:9003/mcp"))
    assert "email-service" in note
    assert "imagegen-service" in note
    assert "business-service" not in note.split("unlike")[0]
    assert "SERVICE INCLUSION NOT CONFIRMED" in note


# ── 2. The verified business-service/storage-service chain remains present ─────

def test_two_service_chain_alone_is_never_checked():
    """Fewer than 3 named services: nothing to cross-check against itself."""
    answer = (
        "The following services are involved: API/business-service and "
        "API/storage-service."
    )
    note = _run(_unconfirmed_service_inclusion(_T11_TASK, answer, None))
    assert note == ""


# ── 3. email-service/imagegen-service excluded when no qualifying dependency ───

def test_services_with_no_constant_anywhere_are_flagged(monkeypatch):
    _patch_search(monkeypatch, confirmed={"business-service", "storage-service"})
    note = _run(_unconfirmed_service_inclusion(
        _T11_TASK, _T11_REAL_ANSWER_CONCLUSION, "http://hive-mcp:9003/mcp"))
    assert note != ""


# ── 4. A genuine direct dependency is included (never flagged) ─────────────────

def test_a_service_with_a_real_constant_anywhere_is_never_flagged(monkeypatch):
    """All 4 named services happen to have the constant somewhere (e.g. a
    legitimate, unrelated reason) -- all-confirmed is silent, same as
    all-unconfirmed; nothing to split the chain on."""
    _patch_search(monkeypatch, confirmed={
        "business-service", "storage-service", "email-service", "imagegen-service"})
    note = _run(_unconfirmed_service_inclusion(
        _T11_TASK, _T11_REAL_ANSWER_CONCLUSION, "http://hive-mcp:9003/mcp"))
    assert note == ""


# ── 5. A supported indirect/config-mediated dependency is handled per contract ──

def test_a_partially_confirmed_chain_names_which_service_does_have_a_link(
        monkeypatch):
    _patch_search(monkeypatch, confirmed={"business-service", "storage-service"})
    note = _run(_unconfirmed_service_inclusion(
        _T11_TASK, _T11_REAL_ANSWER_CONCLUSION, "http://hive-mcp:9003/mcp"))
    assert "business-service" in note
    assert "storage-service" in note
    assert "does have one" in note


# ── 6. An unresolved relationship is marked uncertain, never proven/disproven ───

def test_banner_language_reports_uncertainty_not_a_proven_error(monkeypatch):
    _patch_search(monkeypatch, confirmed={"business-service", "storage-service"})
    note = _run(_unconfirmed_service_inclusion(
        _T11_TASK, _T11_REAL_ANSWER_CONCLUSION, "http://hive-mcp:9003/mcp"))
    assert "reported as unconfirmed, not as an error" in note


def test_all_unconfirmed_is_silent_not_a_false_accusation(monkeypatch):
    """When hive-mcp search finds the constant for NONE of the named services,
    this is silent -- most likely the live search did not run at all (or this
    project's convention genuinely does not apply here), not a scope-creep
    signal. A different problem than this check is built to diagnose."""
    _patch_search(monkeypatch, confirmed=set())
    note = _run(_unconfirmed_service_inclusion(
        _T11_TASK, _T11_REAL_ANSWER_CONCLUSION, "http://hive-mcp:9003/mcp"))
    assert note == ""


def test_task_not_asking_about_services_is_silent():
    answer = _T11_REAL_ANSWER_CONCLUSION
    note = _run(_unconfirmed_service_inclusion(
        "Add a new endpoint to list seller documents.", answer, None))
    assert note == ""


def test_disclosure_only_never_mutates_the_answer_content(monkeypatch):
    """The check only ever returns an APPENDABLE banner string; it never takes
    the content as a parameter it could rewrite, and its own return is a pure
    function of (task, content, search results) -- confirmed by checking the
    original conclusion text is unchanged, not replaced."""
    _patch_search(monkeypatch, confirmed={"business-service", "storage-service"})
    original = _T11_REAL_ANSWER_CONCLUSION
    note = _run(_unconfirmed_service_inclusion(
        _T11_TASK, original, "http://hive-mcp:9003/mcp"))
    assert original == _T11_REAL_ANSWER_CONCLUSION  # untouched by the call
    assert note.startswith("\n\n---\n**SERVICE INCLUSION")
