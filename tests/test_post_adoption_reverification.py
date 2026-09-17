"""Phase Z -- post-adoption re-verification inside _verified_answer itself.

Phase Y closed one confirmed instance of this defect class: a Phase R guard
(`_evidence_integrity_check`, OUTSIDE `_verified_answer`) could adopt a
reconciliation candidate without ever passing it back through
`verify_claims`. Phase Z's own structural audit found a SECOND, distinct
instance INSIDE `_verified_answer` itself:

`_verify_claims` runs exactly ONCE, at the very top of `_verified_answer`,
against the original draft. Its verdict (`_fab_report`/`_fab_bad`) is then
REUSED, not re-run, at the function's main "nothing else fired" return site
near its bottom -- explicitly, per that site's own comment: "the answer has
not changed since." That assumption is false whenever an EARLIER guard in
the same call replaces `content` via `_adopt_retry` on grounds of being MORE
GROUNDED (more reads) -- `_more_grounded` compares read counts, never
content correctness, so a retry that reads more files while ALSO inventing a
symbol the original draft never had is adopted, then shipped under the
STALE "clean" verdict computed against the pre-retry text.

Six guards can trigger this (`_adopt_retry` under "unfinished-intent",
"write-claim", "search-claim", "no-evidence", "db-evidence", "enumeration").
This file reproduces it via the unfinished-intent guard, the one
`test_unfinished_intent.py` already exercises end-to-end, so this test
reuses its exact fixture shapes rather than inventing new ones.

The fix: `swarm/team.py` now snapshots the content actually passed to the
up-front `_verify_claims` call and, at the reuse site, re-verifies instead
of reusing the stale verdict whenever `content` no longer matches that
snapshot -- bounded to at most one extra `verify_claims` call per
`_verified_answer` invocation (only when an adoption genuinely happened),
never a loop.
"""
from types import SimpleNamespace

import pytest

from swarm import team


def _msgs(*items):
    return SimpleNamespace(messages=list(items))


def _tool_msg(name: str, content: str):
    return SimpleNamespace(role="tool", tool_name=name, content=content)


class _FakeTeam:
    """Same fixture shape as test_unfinished_intent.py's own _FakeTeam."""

    def __init__(self, retry_result):
        self._retry_result = retry_result
        self.prompts = []

    def arun(self, prompt, stream=False, yield_run_output=False):
        self.prompts.append(prompt)
        if stream:
            return self._stream()
        return self._direct()

    async def _direct(self):
        return self._retry_result

    async def _stream(self):
        if self._retry_result is not None:
            yield self._retry_result


@pytest.mark.asyncio
async def test_a_retry_adopted_for_being_more_grounded_is_still_reverified(monkeypatch):
    """The Phase Z regression: the ORIGINAL draft is clean, the unfinished-intent
    guard retries it, the retry is MORE grounded (a real file read vs. none) so
    _adopt_retry takes it -- and the retry's own text names a symbol that does
    not exist. Before the Phase Z fix, this shipped under the stale "clean"
    verdict computed against the pre-retry draft. After the fix, the adopted
    text is re-verified and the fabrication is surfaced."""
    calls = {"n": 0}

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        calls["n"] += 1
        if "FooBarNonExistentClass" in content:
            # Real verify_claims report shape -- the downstream parser
            # (_claim_token) looks for a line starting with the literal
            # prefix "NOT FOUND", not a summary sentence.
            return (
                "verify_claims — deterministic grep of the claims in this answer\n\n"
                "SYMBOLS (1 checked):\n"
                "  NOT FOUND  FooBarNonExistentClass              "
                "<-- does not exist in the project\n\n"
                "VERDICT: 1 claim(s) could NOT be found in the project."
            ), True, False
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team, "_verify_claims", fake_verify_claims)

    original_result = _msgs(_tool_msg("notion_get_page", "some partial page content"))
    content = "Let me check the migration file for the exact schema:"  # unfinished intent
    retry_result = SimpleNamespace(
        content=(
            "The `FooBarNonExistentClass` handles migration compliance. All "
            "requirements are covered."
        ),
        messages=[_tool_msg("get_file_content", "class Foo: ...")],  # a real read -> more grounded
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(
        content, "compare phase 1 requirements", fake_team,
        "http://fake-hive-mcp", result=original_result, hive_mcp_tools=object())

    assert len(fake_team.prompts) == 1  # exactly one retry, from the unfinished-intent guard
    assert calls["n"] == 2  # the up-front check, and the Phase Z post-adoption re-check
    assert "FooBarNonExistentClass" in out
    assert "could NOT be found" in out  # the fabrication disclaimer actually shipped
    assert "All requirements are covered." in out  # the adopted text itself still ships


@pytest.mark.asyncio
async def test_a_retry_adopted_that_is_genuinely_clean_ships_without_a_false_disclaimer(
        monkeypatch):
    """The other half of the proof: re-verification must not manufacture a false
    positive on a retry that is genuinely fine."""
    calls = {"n": 0}

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        calls["n"] += 1
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team, "_verify_claims", fake_verify_claims)

    original_result = _msgs(_tool_msg("notion_get_page", "some partial page content"))
    content = "Let me check the migration file for the exact schema:"
    retry_result = SimpleNamespace(
        content="The sku_prefix column exists in models.py at line 129. All Phase "
                "1 requirements are covered by the current implementation.",
        messages=[_tool_msg("get_file_content", "sku_prefix = Column(...)")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(
        content, "compare phase 1 requirements", fake_team,
        "http://fake-hive-mcp", result=original_result, hive_mcp_tools=object())

    assert calls["n"] == 2  # re-verified, but...
    assert "could NOT be found" not in out
    assert out.startswith("The sku_prefix column exists")  # ships clean, no false banner


@pytest.mark.asyncio
async def test_no_adoption_means_no_extra_verify_claims_call(monkeypatch):
    """Boundedness / no-regression proof: when content genuinely does not change
    (the common case -- no guard fires), the stale verdict is reused exactly as
    before, and verify_claims is called only ONCE, not twice."""
    calls = {"n": 0}

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        calls["n"] += 1
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team, "_verify_claims", fake_verify_claims)

    original_result = _msgs(_tool_msg("get_file_content", "sku_prefix = Column(...)"))
    content = "The sku_prefix column exists in models.py at line 129. All Phase 1 requirements are covered."
    fake_team = _FakeTeam(retry_result=None)

    out = await team._verified_answer(
        content, "compare phase 1 requirements", fake_team,
        "http://fake-hive-mcp", result=original_result, hive_mcp_tools=object())

    assert fake_team.prompts == []  # nothing retried, nothing adopted
    assert calls["n"] == 1  # verify_claims ran once, verdict reused, no extra round trip
    assert out == content
