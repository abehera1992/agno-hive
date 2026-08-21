"""Tests: forced text-only must not leak raw tool-call syntax as the answer.

Live, 2026-08-21. Two probes (T9, T11) returned this as their FINAL ANSWER:

    <tool_call>
    {"name": "list_directory", "arguments": {"relative_path": "API/inventory-service/router"}}
    </tool_call>

Cause established by a controlled probe against the served model, one variable
changed and nothing else:

    tool_choice omitted -> finish_reason "tool_calls", content ""
    tool_choice "none"  -> finish_reason "stop",       content "<tool_call>{...}</tool_call>"

tool_choice="none" does not make this model stop WANTING the tool. It removes the
structured channel, so the model writes the Hermes tags it was trained on as prose.
vLLM's parser only extracts a structured call, so the tags survive into content — and
because finish_reason is "stop", agno sees an ordinary text reply that happens to be
made of syntax. Nothing downstream flags it.

Introduced by the budget guard's forcing the same day: it converted a silent 300s
stall into a visible garbage answer. Better, but not acceptable.

The fix is NOT the existing recovery path. That turns stranded tags back INTO a tool
call, which is right when the model merely failed to structure one and exactly wrong
here — the harness took the tool away on purpose, and recovering it would re-arm the
call the forcing just removed.
"""
import pytest

from swarm.tool_fix import VLLMToolFix


class _Resp:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


def _fix() -> VLLMToolFix:
    return VLLMToolFix.__new__(VLLMToolFix)   # no network/config needed for this layer


_LEAK = ('<tool_call>\n{"name": "list_directory", "arguments": '
         '{"relative_path": "API/inventory-service/router"}}\n</tool_call>')


def test_the_live_leak_is_stripped_when_forced():
    """The exact string T9 and T11 returned to the user."""
    fix = _fix()
    fix._tool_choice = "none"
    resp = _Resp(_LEAK)

    handled = fix._sanitize_forced_text(resp)

    assert handled is True
    assert "<tool_call>" not in resp.content


def test_it_substitutes_an_honest_message_not_emptiness():
    """Empty content would trip the groundedness guards into a retry — the loop this
    whole path exists to end."""
    fix = _fix()
    fix._tool_choice = "none"
    resp = _Resp(_LEAK)

    fix._sanitize_forced_text(resp)

    assert resp.content.strip()
    assert "tool budget" in resp.content.lower()


def test_real_prose_around_the_tags_is_kept():
    """A model that answers AND then emits a stray call should keep its answer."""
    fix = _fix()
    fix._tool_choice = "none"
    resp = _Resp(f"The file has 3 endpoints.\n{_LEAK}")

    fix._sanitize_forced_text(resp)

    assert "The file has 3 endpoints." in resp.content
    assert "<tool_call>" not in resp.content


def test_python_tag_format_is_also_stripped():
    fix = _fix()
    fix._tool_choice = "none"
    resp = _Resp('<|python_tag|>{"type": "function", "name": "list_directory"}')

    assert fix._sanitize_forced_text(resp) is True
    assert "python_tag" not in resp.content


# ── it must NOT interfere when forcing is not in effect ───────────────────────

def test_it_does_nothing_when_not_forced():
    """The normal recovery path must still convert stranded tags INTO a tool call —
    that is the whole reason VLLMToolFix exists (Phase 7, 2026-08-15)."""
    fix = _fix()
    fix._tool_choice = None
    resp = _Resp(_LEAK)

    assert fix._sanitize_forced_text(resp) is False
    assert resp.content == _LEAK, "must be left for the recovery path to handle"


def test_ordinary_content_is_untouched_even_when_forced():
    fix = _fix()
    fix._tool_choice = "none"
    resp = _Resp("The sku_prefix column is defined at line 129.")

    assert fix._sanitize_forced_text(resp) is False
    assert resp.content == "The sku_prefix column is defined at line 129."


def test_a_response_with_real_tool_calls_is_never_sanitized():
    """Structured calls are the healthy path and must be left completely alone."""
    fix = _fix()
    fix._tool_choice = "none"
    resp = _Resp("")
    resp.tool_calls = [{"type": "function", "function": {"name": "x", "arguments": "{}"}}]

    assert fix._sanitize_forced_text(resp) is False


# ── wiring ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method", ["_parse_provider_response", "_parse_provider_response_delta"])
def test_both_parse_paths_call_the_sanitizer(method):
    """Streaming and non-streaming both reach the user, and the live leak arrived on
    the terminal message — covering only one would leave the observed case open."""
    import inspect

    src = inspect.getsource(getattr(VLLMToolFix, method))
    assert "_sanitize_forced_text" in src


def test_force_text_only_sets_the_flag_the_sanitizer_reads():
    """The two halves only work as a pair: without model._tool_choice the sanitizer
    cannot tell a deliberate disarm from a model that merely failed to structure a
    call, and would recover the tags back into the call forcing just removed."""
    import inspect

    from swarm import team

    src = inspect.getsource(team._force_text_only)
    assert "model._tool_choice = \"none\"" in src
