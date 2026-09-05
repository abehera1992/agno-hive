"""OllamaToolFix / VLLMToolFix — client-side recovery for tool-call intent that
arrives as raw content text instead of a structured tool_calls entry.

Supported formats:
  1. <tool_call>{"name": ..., "arguments": ...}</tool_call> (qwen2.5)
  2. <|python_tag|>{"type": "function", "name": ..., "parameters": ...} (llama3.3)
  3. {"name": ..., "arguments": ...} bare JSON (qwen2.5 non-streaming)
  4. Multiple bare JSON objects concatenated with whitespace
  5. JSON embedded in prose (qwen2.5-coder in explanatory text)
  6. <function=NAME><parameter=KEY>VALUE</parameter>...</function> (qwen3-coder XML)

NOTE: agno-internal tools (delegate_task_to_member, delegate_task_to_members) are
intentionally excluded from MCP tool call interception so agno can handle them natively.

VLLMToolFix (2026-08-15): the SAME recovery, ported onto the OpenAILike/vLLM path.
get_model()'s vLLM branch (swarm/agents.py) previously used plain OpenAILike with
NO client-side recovery at all -- OllamaToolFix was only ever wired into the
Ollama fallback branch. Confirmed live via a direct, reproducible gateway request
(bypassing agno's own classification entirely): this project's ONLY served vLLM
model, local-shared (every local model_id collapses onto it via the ALL-MoE
consolidation in model_catalog; called qwen3-coder-30b at the time of this
2026-08-15 investigation, renamed 2026-08-16), correctly emits Format 1
(<tool_call>{"name": "search_files", ...}</tool_call>) in BOTH streaming and
non-streaming mode -- vLLM's own engine logs show sustained ~20-24 tokens/s
generation the whole time, ruling out an instant-EOS "gives up" explanation --
but LiteLLM's own hermes-format tool-call parser does not reliably extract it
into a structured tool_calls delta. When that extraction fails, the raw tagged
text has no recovery layer under the current vLLM-backend configuration and the
turn comes back with no usable content and no tool call, so agno just re-prompts
with the identical context -- a silent, repeating loop until the 300s liveness
auto-kill (config.liveness_silence_threshold_s) fires with zero answer produced.
"""

# agno team internal tools — do not strip from content, let agno handle natively
_AGNO_INTERNAL_TOOLS = {"delegate_task_to_member", "delegate_task_to_members", "get_member_information"}

# Largest prompt, in real tokens, that the model server has reported this run.
#
# This is the only honest measure of how full the context is. The guard that was
# supposed to prevent overflow counted CHARACTERS OF MEMBER RESULTS instead, and
# they are not the same quantity or even close: T12 died at 258,049 of 262,144
# tokens while that counter sat at 42,804 of a 400,000 budget -- under 11%, never
# remotely close to firing. Member results are a small slice of the prompt; the
# members' own file reads, the tool results, and the accumulated turn history are
# most of it, and none of them were being counted.
#
# agno already asks for this: it sends stream_options={"include_usage": True}, so
# the final chunk of every streamed call carries usage, and _get_metrics maps
# prompt_tokens -> MessageMetrics.input_tokens. Both parsers below populate
# model_response.response_usage from it. Nothing new is requested from the server;
# the number was already arriving and simply never read.
#
# Module-level is per-RUN state, not global state: api/server.py runs each task in
# its own subprocess (_run_worker_subprocess), so this module is freshly imported
# per run and starts at zero. Peak rather than last-seen, because a run's prompt
# does not grow monotonically -- a member's own turns are short, and taking the
# most recent would read as "context freed up" right after a big coordinator turn.
_peak_input_tokens = 0


def peak_input_tokens() -> int:
    """Largest prompt-token count the model server has reported this run."""
    return _peak_input_tokens


def _record_input_tokens(model_response) -> None:
    global _peak_input_tokens
    usage = getattr(model_response, "response_usage", None)
    if usage is None:
        return
    seen = getattr(usage, "input_tokens", 0) or 0
    if isinstance(seen, (int, float)) and seen > _peak_input_tokens:
        _peak_input_tokens = int(seen)
import json
import re
from typing import Any

from agno.models.ollama import Ollama
from agno.models.openai.like import OpenAILike
from agno.models.response import ModelResponse


# Largest partial tool call to hold before deciding it is not one. A member's whole
# report can be longer than this; the buffer only ever holds text after an opening
# "<tool_call>" tag, so the cap bounds a malformed emission, not an answer.
_TC_OPEN = "<tool_call>"


def _open_tag_prefix_len(text: str) -> int:
    """How many trailing characters of `text` could still grow into _TC_OPEN.

    "…the <too" -> 4, so those four are withheld until the next delta decides. Returns
    0 when nothing at the end could become the tag, which is the case for essentially
    every chunk of ordinary prose.
    """
    if not text:
        return 0
    for n in range(min(len(text), len(_TC_OPEN) - 1), 0, -1):
        if _TC_OPEN.startswith(text[-n:]):
            return n
    return 0


_TC_BUFFER_CAP = 8192


class _ToolCallRecoveryMixin:
    """Shared parsing + response-hook logic for OllamaToolFix and VLLMToolFix.
    Relies on `super()._parse_provider_response(...)` / `..._delta(...)`
    resolving to the real base model class (Ollama or OpenAILike) via each
    subclass's own MRO — the mixin itself makes no network/base-class
    assumptions beyond that both base classes expose the same two hook methods
    (confirmed: both come from agno.models.base.Model)."""

    def _parse_tool_calls_from_content(self, content: str) -> list[dict]:
        calls: list[dict] = []

        # Format 1: <tool_call>...</tool_call> tags
        tag_matches = re.findall(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL)
        if tag_matches:
            for m in tag_matches:
                try:
                    calls.append(json.loads(m.strip()))
                except json.JSONDecodeError:
                    pass
            return calls

        # Format 2: <|python_tag|> prefix (llama3.3)
        python_tag = "<|python_tag|>"
        if python_tag in content:
            parts = content.split(python_tag)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                # Strip any trailing eot-like tags
                part = re.sub(r"<\|[^|]+\|>.*$", "", part).strip()
                if not part:
                    continue
                try:
                    obj = json.loads(part)
                    calls.append(obj)
                except json.JSONDecodeError:
                    pass
            return calls

        # Format 6: qwen3-coder XML — <function=NAME><parameter=KEY>VALUE</parameter></function>
        # The closing </function> is sometimes missing or followed by a stray
        # </tool_call>, so match each block up to the next <function= or end of string.
        if "<function=" in content:
            blocks = re.findall(
                r"<function=([\w./-]+)>(.*?)(?=</function>|<function=|\Z)",
                content,
                re.DOTALL,
            )
            for name, body in blocks:
                params = {
                    k: v.strip()
                    for k, v in re.findall(
                        r"<parameter=([\w./-]+)>(.*?)</parameter>", body, re.DOTALL
                    )
                }
                calls.append({"name": name, "arguments": params})
            if calls:
                return calls

        # Format 3 / 4: bare JSON objects at start of content
        stripped = content.strip()
        if stripped.startswith("{"):
            decoder = json.JSONDecoder()
            pos = 0
            while pos < len(stripped):
                remaining = stripped[pos:].lstrip()
                if not remaining:
                    break
                skipped = len(stripped[pos:]) - len(remaining)
                try:
                    obj, end = decoder.raw_decode(remaining)
                    calls.append(obj)
                    pos += skipped + end
                except json.JSONDecodeError:
                    break

        # Format 5: tool-call JSON embedded in prose
        # qwen2.5-coder sometimes wraps {"name":..., "arguments":...} in explanatory text
        if not calls:
            decoder = json.JSONDecoder()
            for match in re.finditer(r'\{"name"\s*:', content):
                try:
                    obj, _ = decoder.raw_decode(content[match.start():])
                    if isinstance(obj, dict) and obj.get("name") and (
                        "arguments" in obj or "parameters" in obj
                    ):
                        calls.append(obj)
                except json.JSONDecodeError:
                    continue

        return calls

    def _to_tool_calls(self, parsed: list[dict]) -> list[dict]:
        result = []
        for call in parsed:
            name = call.get("name") or call.get("function", {}).get("name")
            # Skip agno-internal tools — agno handles these natively, not via MCP
            if name in _AGNO_INTERNAL_TOOLS:
                continue
            # Support "arguments" (qwen2.5), "parameters" (llama3.3), or nested "function.arguments"
            args = (
                call.get("arguments")
                or call.get("parameters")
                or call.get("function", {}).get("arguments", {})
            )
            if name:
                result.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args) if isinstance(args, dict) else (args or "{}"),
                    },
                })
        return result

    # Text this model emits INSTEAD of a structured call once tool_choice="none"
    # removes the structured channel. It still wants the tool, so it writes the Hermes
    # tags it was trained on as prose.
    _FORCED_TAG_RE = re.compile(r"<tool_call>.*?</tool_call>|<\|python_tag\|>.*", re.DOTALL)

    def _sanitize_forced_text(self, model_response) -> bool:
        """Strip leaked tool-call syntax when the model was forced text-only.

        Returns True if it handled the response, meaning the caller must NOT run the
        normal recovery below.

        The distinction matters and is the whole reason this is separate: normal
        recovery turns stranded tags back INTO a tool call, which is right when the
        model simply failed to structure one -- and exactly wrong here, where the
        harness has deliberately taken the tool away (a budget ceiling, or a repeated
        ignored stub). Recovering it would re-arm the call the forcing just removed.

        Measured 2026-08-21 with a controlled probe against the served model, one
        variable changed:
            tool_choice omitted -> finish_reason "tool_calls", content ""
            tool_choice "none"  -> finish_reason "stop", content
                                   '<tool_call>\\n{"name": "list_directory", ...}</tool_call>'
        Two live runs (T9, T11) returned exactly that as their FINAL ANSWER. From
        agno's side nothing looked wrong -- finish_reason was "stop", so it is an
        ordinary text reply, just one made of syntax.

        Substitutes an honest sentence rather than empty content: an empty answer would
        trip the groundedness guards into a retry, which is the loop this whole path
        exists to end.
        """
        if getattr(self, "_tool_choice", None) != "none":
            return False
        content = model_response.content or ""
        if "<tool_call>" not in content and "<|python_tag|>" not in content:
            return False

        stripped = self._FORCED_TAG_RE.sub("", content).strip()
        model_response.content = stripped or (
            "I attempted another tool call, but this run's tool budget is exhausted so "
            "no further tool calls can be made. Answering from what was already "
            "gathered: I could not complete the remaining lookup, so treat anything "
            "not already established above as undetermined rather than assumed."
        )
        return True

    def _parse_provider_response(self, response: dict) -> ModelResponse:
        model_response = super()._parse_provider_response(response)
        # Before any early return below -- a turn that came back as a tool call has
        # the same prompt behind it as one that came back as prose, and skipping it
        # would blind the budget to exactly the tool-heavy runs that overflow.
        _record_input_tokens(model_response)

        if model_response.tool_calls:
            return model_response

        if self._sanitize_forced_text(model_response):
            return model_response

        if model_response.content:
            parsed = self._parse_tool_calls_from_content(model_response.content)
            if parsed:
                tool_calls = self._to_tool_calls(parsed)
                if tool_calls:  # only strip content if valid named tool calls found
                    model_response.tool_calls = tool_calls
                    model_response.content = ""

        return model_response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        model_response = super()._parse_provider_response_delta(response)
        # Same placement rationale as the non-streaming parser above. Usage rides
        # on the FINAL chunk only (stream_options include_usage), so this is a
        # no-op on the thousands of content deltas and fires once per call.
        _record_input_tokens(model_response)

        if model_response.tool_calls:
            self._tc_buffer = ""
            return model_response

        if self._sanitize_forced_text(model_response):
            return model_response

        if model_response.content:
            # Recover a call that arrives SPLIT ACROSS DELTAS, which is every one of
            # them on this path. The single-delta parse below cannot see it: content
            # streams in fragments as small as one character, so an 11-character
            # "<tool_call>" tag is spread over 11 deltas and no individual fragment
            # ever contains a complete call. The recovery has been here since the
            # 2026-08-15 port and has been structurally unreachable while streaming.
            #
            # Measured 2026-09-05 across two days: 21 of 159 member episodes (13%)
            # end with the member emitting a tool call that arrives as prose, and
            # those runs hold ALL 5 verified fabrications in the corpus. Ruled out as
            # causes, with data: our own _force_text_only (0 of 24 leaks preceded by a
            # forcing line, whole-run window), agno's tool_call_limit (leaks at 13, 15
            # and 16 cumulative calls against limits of 25/45/80, 0 of 24 reaching
            # one), and parser misconfiguration (--enable-auto-tool-choice
            # --tool-call-parser hermes is set, and vLLM logs zero parse errors). The
            # emission itself is well formed and starts at character 0 --
            # "content: +11 chars (11 total) -- '<tool_call>'".
            #
            # Buffering starts ONLY once an opening tag is seen and ends at the
            # closing tag, so ordinary prose never accumulates and a normal answer
            # streams through untouched. Content is withheld while buffering,
            # because emitting the fragments is exactly how the tag reached a user.
            buf = getattr(self, "_tc_buffer", "")
            # The trigger has to be PREFIX-aware, not a containment test. The first
            # version of this fix asked `"<tool_call>" in content` and reproduced the
            # exact blindness it was written to remove: with one character per delta,
            # no single chunk ever contains the eleven-character tag, so buffering
            # never started. Caught by replaying a real leak one character at a time.
            #
            # So: carry any trailing text that could still BECOME the tag, and decide
            # once enough characters have arrived.
            if not buf:
                combined = getattr(self, "_tc_pend", "") + model_response.content
                if _TC_OPEN in combined:
                    head, _, rest = combined.partition(_TC_OPEN)
                    self._tc_pend = ""
                    buf = _TC_OPEN + rest
                    model_response.content = head      # anything before the tag is real
                else:
                    keep = _open_tag_prefix_len(combined)
                    if keep:
                        self._tc_pend = combined[-keep:]
                        model_response.content = combined[:-keep]
                    else:
                        self._tc_pend = ""
                        model_response.content = combined
                    if not buf:
                        if model_response.content:
                            parsed = self._parse_tool_calls_from_content(
                                model_response.content)
                            if parsed:
                                tool_calls = self._to_tool_calls(parsed)
                                if tool_calls:
                                    model_response.tool_calls = tool_calls
                                    model_response.content = ""
                        return model_response
            else:
                buf += model_response.content
            if buf:
                # A runaway buffer would swallow a whole answer if the closing tag
                # never arrives. Past the cap, give the text back rather than eat it.
                if len(buf) > _TC_BUFFER_CAP:
                    self._tc_buffer = ""
                    model_response.content = buf
                    return model_response
                if "</tool_call>" in buf:
                    self._tc_buffer = ""
                    parsed = self._parse_tool_calls_from_content(buf)
                    if parsed:
                        tool_calls = self._to_tool_calls(parsed)
                        if tool_calls:
                            model_response.tool_calls = tool_calls
                            model_response.content = ""
                            return model_response
                    # Not a real call after all -- a quotation, or malformed. Hand the
                    # text back verbatim; swallowing a genuine quotation would be its
                    # own fabrication, the rule _strip_leaked_tool_tags already keeps.
                    model_response.content = buf
                    return model_response
                # Mid-call: hold the fragment back and wait for the rest.
                self._tc_buffer = buf
                model_response.content = ""
                return model_response

            parsed = self._parse_tool_calls_from_content(model_response.content)
            if parsed:
                tool_calls = self._to_tool_calls(parsed)
                if tool_calls:  # only strip content if valid named tool calls found
                    model_response.tool_calls = tool_calls
                    model_response.content = ""

        return model_response


class OllamaToolFix(_ToolCallRecoveryMixin, Ollama):
    pass


class VLLMToolFix(_ToolCallRecoveryMixin, OpenAILike):
    """Same recovery as OllamaToolFix, for the vLLM/OpenAILike path — see this
    module's own docstring for the live incident (2026-08-15) that motivated
    porting it here rather than leaving OllamaToolFix as Ollama-only."""
    pass
