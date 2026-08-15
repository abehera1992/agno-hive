"""Tests for swarm/tool_fix.py -- OllamaToolFix, VLLMToolFix, and the shared
_ToolCallRecoveryMixin they're both built from.

VLLMToolFix (2026-08-15) is the direct fix for a live-reproduced bug: get_model()'s
vLLM branch used plain OpenAILike with NO client-side tool-call-text recovery,
on the documented assumption that vLLM's own --tool-call-parser always converts a
model's raw tool-call text into a structured tool_calls delta. Confirmed FALSE via
a direct, reproducible gateway request (bypassing agno's classification layer
entirely): qwen3-coder-30b (this project's only served vLLM model) correctly
emits Format 1 (<tool_call>{"name": ...}</tool_call>) in both streaming and
non-streaming mode -- vLLM's own engine logs showed genuine sustained ~20-24
tokens/s generation the whole time, ruling out an instant-EOS explanation -- but
LiteLLM's hermes parser did not reliably extract it into tool_calls. The raw
tagged text then had nowhere to go: no recovery layer existed on the vLLM path
(OllamaToolFix, the only code with this exact recovery logic, was only ever
wired into the Ollama fallback branch). Live symptom: an empty turn, no tool
call, agno re-prompting with the identical context, silently looping until the
300s liveness auto-kill fired with zero answer produced.

Neither OllamaToolFix nor (now) VLLMToolFix had direct unit coverage of the
parsing/recovery logic itself before this file -- only a get_model() shape check
existed (test_get_model_cloud.py's test_ollama_backend_returns_ollama_tool_fix).
This closes that gap for both, not just the new class.
"""
from agno.models.response import ModelResponse

from swarm.tool_fix import OllamaToolFix, VLLMToolFix, _ToolCallRecoveryMixin


class _FakeBaseModel:
    """Stand-in for agno's real Ollama/OpenAILike base classes -- exposes the
    same two hook methods _ToolCallRecoveryMixin calls via super(), returning a
    caller-controlled ModelResponse so the mixin's OWN recovery logic can be
    tested in isolation from real network/parsing machinery."""

    def __init__(self, response_to_return: ModelResponse):
        self._response_to_return = response_to_return

    def _parse_provider_response(self, response):
        return self._response_to_return

    def _parse_provider_response_delta(self, response):
        return self._response_to_return


class _FakeToolFix(_ToolCallRecoveryMixin, _FakeBaseModel):
    pass


def _make_fixture(content: str | None = None, tool_calls: list | None = None) -> _FakeToolFix:
    base_response = ModelResponse(content=content, tool_calls=tool_calls or [])
    return _FakeToolFix(base_response)


# ── _parse_tool_calls_from_content: Format 1, <tool_call> tags ──────────────────

def test_parses_a_single_tool_call_tag():
    fixture = _make_fixture()
    content = '<tool_call>\n{"name": "search_files", "arguments": {"pattern": "GSTIN"}}\n</tool_call>'

    parsed = fixture._parse_tool_calls_from_content(content)

    assert parsed == [{"name": "search_files", "arguments": {"pattern": "GSTIN"}}]


def test_parses_multiple_tool_call_tags():
    fixture = _make_fixture()
    content = (
        '<tool_call>{"name": "search_files", "arguments": {"pattern": "a"}}</tool_call>'
        '<tool_call>{"name": "get_file_content", "arguments": {"relative_path": "b.py"}}</tool_call>'
    )

    parsed = fixture._parse_tool_calls_from_content(content)

    assert len(parsed) == 2
    assert parsed[0]["name"] == "search_files"
    assert parsed[1]["name"] == "get_file_content"


def test_malformed_json_inside_tool_call_tag_is_skipped_not_raised():
    fixture = _make_fixture()
    content = '<tool_call>{not valid json}</tool_call>'

    parsed = fixture._parse_tool_calls_from_content(content)

    assert parsed == []


# ── Format 2: <|python_tag|> (llama3.3) ─────────────────────────────────────────

def test_parses_python_tag_format():
    fixture = _make_fixture()
    content = '<|python_tag|>{"type": "function", "name": "search_files", "parameters": {"pattern": "x"}}'

    parsed = fixture._parse_tool_calls_from_content(content)

    assert parsed == [{"type": "function", "name": "search_files", "parameters": {"pattern": "x"}}]


# ── Format 6: qwen3-coder XML <function=...> ────────────────────────────────────

def test_parses_function_xml_format():
    fixture = _make_fixture()
    content = '<function=search_files><parameter=pattern>GSTIN</parameter></function>'

    parsed = fixture._parse_tool_calls_from_content(content)

    assert parsed == [{"name": "search_files", "arguments": {"pattern": "GSTIN"}}]


# ── Format 3/4: bare JSON ────────────────────────────────────────────────────────

def test_parses_bare_json_object():
    fixture = _make_fixture()
    content = '{"name": "search_files", "arguments": {"pattern": "GSTIN"}}'

    parsed = fixture._parse_tool_calls_from_content(content)

    assert parsed == [{"name": "search_files", "arguments": {"pattern": "GSTIN"}}]


# ── Format 5: JSON embedded in prose ─────────────────────────────────────────────

def test_parses_tool_call_json_embedded_in_prose():
    fixture = _make_fixture()
    content = 'Let me search for that: {"name": "search_files", "arguments": {"pattern": "GSTIN"}} to find it.'

    parsed = fixture._parse_tool_calls_from_content(content)

    assert parsed == [{"name": "search_files", "arguments": {"pattern": "GSTIN"}}]


def test_plain_prose_with_no_tool_call_shape_parses_to_nothing():
    fixture = _make_fixture()
    content = "The parties module has no GSTIN fields in the frontend form."

    parsed = fixture._parse_tool_calls_from_content(content)

    assert parsed == []


# ── _to_tool_calls ────────────────────────────────────────────────────────────

def test_to_tool_calls_builds_the_openai_shape():
    fixture = _make_fixture()

    result = fixture._to_tool_calls([{"name": "search_files", "arguments": {"pattern": "GSTIN"}}])

    assert result == [{
        "type": "function",
        "function": {"name": "search_files", "arguments": '{"pattern": "GSTIN"}'},
    }]


def test_to_tool_calls_skips_agno_internal_tool_names():
    fixture = _make_fixture()

    result = fixture._to_tool_calls([{"name": "delegate_task_to_member", "arguments": {}}])

    assert result == []


def test_to_tool_calls_supports_parameters_key_for_llama():
    fixture = _make_fixture()

    result = fixture._to_tool_calls([{"name": "search_files", "parameters": {"pattern": "x"}}])

    assert result[0]["function"]["arguments"] == '{"pattern": "x"}'


# ── _parse_provider_response / _parse_provider_response_delta integration ──────

def test_recovers_a_tool_call_from_content_when_base_response_has_none():
    """The exact live incident, reproduced: base class returns real content
    containing a <tool_call> tag and NO structured tool_calls -- the mixin must
    recover it and clear content, so the caller sees a usable tool call instead
    of an empty, un-actionable turn."""
    fixture = _make_fixture(
        content='<tool_call>{"name": "search_files", "arguments": {"pattern": "GSTIN"}}</tool_call>',
        tool_calls=[],
    )

    result = fixture._parse_provider_response({})

    assert result.tool_calls == [{
        "type": "function",
        "function": {"name": "search_files", "arguments": '{"pattern": "GSTIN"}'},
    }]
    assert result.content == ""


def test_streaming_delta_variant_recovers_the_same_way():
    fixture = _make_fixture(
        content='<tool_call>{"name": "search_files", "arguments": {"pattern": "GSTIN"}}</tool_call>',
        tool_calls=[],
    )

    result = fixture._parse_provider_response_delta({})

    assert result.tool_calls
    assert result.content == ""


def test_does_not_touch_a_response_that_already_has_real_tool_calls():
    """If the base class's own parser DID succeed (the common case, live
    evidence shows this usually works), the mixin must be a complete no-op --
    never re-parse or duplicate an already-successful extraction."""
    real_tool_calls = [{"type": "function", "function": {"name": "search_files", "arguments": "{}"}}]
    fixture = _make_fixture(content="", tool_calls=real_tool_calls)

    result = fixture._parse_provider_response({})

    assert result.tool_calls is real_tool_calls


def test_plain_prose_final_answer_passes_through_untouched():
    """The other common case: a genuine final answer with no tool-call shape at
    all must be returned exactly as the base class produced it."""
    fixture = _make_fixture(content="The GSTIN field is not present in the frontend form.", tool_calls=[])

    result = fixture._parse_provider_response({})

    assert result.content == "The GSTIN field is not present in the frontend form."
    assert result.tool_calls == []


def test_content_with_no_recoverable_tool_call_shape_is_left_alone():
    """Content that merely CONTAINS unrelated braces/text must not be mangled
    into a bogus tool call or have its content silently cleared."""
    fixture = _make_fixture(content="See the {config} object for details.", tool_calls=[])

    result = fixture._parse_provider_response({})

    assert result.content == "See the {config} object for details."
    assert result.tool_calls == []


# ── Both concrete classes share identical recovery behavior ────────────────────

def test_ollama_tool_fix_and_vllm_tool_fix_share_the_same_parsing_logic():
    """Proves the mixin refactor didn't change OllamaToolFix's own behavior and
    that VLLMToolFix (new, 2026-08-15) recovers the identical incident shape --
    same content in, same tool_calls out, regardless of which base class."""
    content = '<tool_call>{"name": "search_files", "arguments": {"pattern": "GSTIN"}}</tool_call>'

    ollama_parsed = OllamaToolFix._parse_tool_calls_from_content(OllamaToolFix.__new__(OllamaToolFix), content)
    vllm_parsed = VLLMToolFix._parse_tool_calls_from_content(VLLMToolFix.__new__(VLLMToolFix), content)

    assert ollama_parsed == vllm_parsed == [{"name": "search_files", "arguments": {"pattern": "GSTIN"}}]


def test_vllm_tool_fix_is_a_real_openai_like_subclass():
    from agno.models.openai.like import OpenAILike

    assert issubclass(VLLMToolFix, OpenAILike)


def test_ollama_tool_fix_is_still_a_real_ollama_subclass():
    from agno.models.ollama import Ollama

    assert issubclass(OllamaToolFix, Ollama)
