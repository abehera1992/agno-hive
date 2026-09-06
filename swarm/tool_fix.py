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


# A tool call the model emits as CONTENT arrives one fragment per delta -- the first
# fragment is the bare opening tag, "<tool_call>", on its own. The per-delta recovery
# below looks for a complete <tool_call>...</tool_call> pair, which cannot exist inside a
# fragment, so nothing converts; agno then concatenates the fragments and the member's
# whole report is 120-201 characters of syntax, discarded as a failed delegation.
#
# Established 2026-09-06 by dumping the raw provider payload at the delta parser, 5
# captures out of 5 identical: ChoiceDelta(content='<tool_call>', tool_calls=None), tag
# present in both the raw chunk and the parsed .content. Ten earlier probes asked which
# FIELD held the tag and all came back negative; it was the right field at the wrong
# granularity, which no field-level probe could have shown.
_TC_OPEN = "<tool_call>"
_TC_CLOSE = "</tool_call>"
# Bounds a malformed emission, not an answer: the buffer only ever holds text that
# follows an opening tag, and anything longer is handed back verbatim.
_TC_BUFFER_CAP = 8192


def _open_tag_prefix_len(text: str) -> int:
    """How many trailing characters of `text` could still grow into _TC_OPEN.

    "...the <too" -> 4, so those four are withheld until the next delta decides. Returns
    0 when nothing at the end could become the tag, which is every chunk of ordinary
    prose. Without this the trigger has to ask `_TC_OPEN in content`, and at one
    character per delta that is never true -- the exact blindness the first attempt at
    this reproduced.
    """
    if not text:
        return 0
    for n in range(min(len(text), len(_TC_OPEN) - 1), 0, -1):
        if _TC_OPEN.startswith(text[-n:]):
            return n
    return 0


# JSON escapes a model may legally write. Anything else after a backslash is stray.
_JSON_ESCAPES = set('"\\/bfnrtu')


def _escape_stray_backslashes(text: str) -> str:
    """Make a model-written tool call parseable without changing what it says.

    The live payload that this exists for:

        {"name": "search_files", "arguments": {"pattern": "router.post\\(\\"/register\\","}}

    `\\(` is not a legal JSON escape, so json.loads rejects the whole object -- which is
    why vLLM's hermes parser did not convert this call either and emitted it as content
    instead. The model wrote a REGEX into a JSON string without escaping its backslashes,
    and a search tool taking a regex argument makes that a routine thing for it to do.

    Consumes a valid escape as a PAIR. A regex that inspects each backslash on its own
    gets "back\\\\slash" wrong -- the second backslash of a correct pair is followed by
    's', so it is doubled again and the string quietly changes meaning. That version was
    written first and caught by replaying the safe cases, not by reading it.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        if nxt and nxt in _JSON_ESCAPES:
            out.append(ch)
            out.append(nxt)
            i += 2
        else:
            out.append("\\\\")
            i += 1
    return "".join(out)


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
                body = m.strip()
                try:
                    calls.append(json.loads(body))
                except json.JSONDecodeError:
                    # Retry with stray backslashes escaped. Measured on the live leak:
                    # every discarded member report in the 2026-09-06 battery carried a
                    # regex argument written straight into JSON, and this is the reason
                    # the call could not be recovered even once it was reassembled.
                    try:
                        calls.append(json.loads(_escape_stray_backslashes(body)))
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


    def _stream_key(self, response) -> str:
        """Per-completion key, so two concurrent streams cannot share a buffer.

        The reverted first attempt kept one buffer on the instance. In `coordinate` mode
        members run one at a time and that is survivable, but `collaborate` (the
        parallel-review team) runs three members at once, and interleaved fragments in a
        single buffer would splice two answers together. The completion id is already on
        every chunk.
        """
        try:
            cid = getattr(response, "id", None)
            if cid:
                return str(cid)
        except Exception:  # noqa: BLE001
            pass
        return "_default"

    @staticmethod
    def _stream_finished(response) -> bool:
        """Has the provider said this stream is over? Drives the flush below."""
        try:
            for ch in (getattr(response, "choices", None) or []):
                if getattr(ch, "finish_reason", None):
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _recover_split_tool_call(self, model_response, response) -> None:
        """Reassemble a tool call that arrives split across deltas.

        Buffering starts only once an opening tag (or a prefix that could still become
        one) is seen, so ordinary prose never accumulates and a normal answer streams
        through byte-identical. While buffering, content is WITHHELD -- emitting the
        fragments is precisely how a raw tag reaches a reader.

        Everything that is not a recognised call is handed back verbatim: an unclosed
        tag at end of stream, anything past _TC_BUFFER_CAP, and a tag pair whose JSON
        will not parse. Swallowing a genuine quotation would be its own fabrication, and
        an unflushed buffer would be silent data loss in the hot streaming path -- the
        specific trap that got the first attempt reverted, where held text was dropped
        whenever a stream ended without a closing tag.
        """
        if not isinstance(getattr(self, "_tc_pending", None), dict):
            self._tc_pending = {}
        key = self._stream_key(response)
        pending = self._tc_pending.get(key, "") + (model_response.content or "")
        if not pending:
            return

        if _TC_OPEN in pending:
            if _TC_CLOSE in pending:
                parsed = self._parse_tool_calls_from_content(pending)
                tool_calls = self._to_tool_calls(parsed) if parsed else None
                if tool_calls:
                    model_response.tool_calls = tool_calls
                    # Whatever sat outside the tags is real text and still ships.
                    model_response.content = re.sub(
                        r"<tool_call>.*?</tool_call>", "", pending, flags=re.DOTALL)
                    self._tc_pending.pop(key, None)
                    print(f"[toolfix] recovered a tool call split across deltas "
                          f"({len(pending)} chars buffered) -> "
                          f"{[c.get('function', {}).get('name') for c in tool_calls]}",
                          flush=True)
                    return
                # A closed pair we cannot parse is not a call. Hand it back.
                model_response.content = pending
                self._tc_pending.pop(key, None)
                return
            if len(pending) > _TC_BUFFER_CAP or self._stream_finished(response):
                model_response.content = pending
                self._tc_pending.pop(key, None)
                return
            self._tc_pending[key] = pending
            model_response.content = ""
            return

        # No opening tag yet: withhold only a trailing fragment that could still become
        # one, emit the rest now so streaming latency is unchanged for ordinary text.
        n = _open_tag_prefix_len(pending)
        if n and not self._stream_finished(response):
            self._tc_pending[key] = pending[-n:]
            model_response.content = pending[:-n]
        else:
            self._tc_pending.pop(key, None)
            model_response.content = pending

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
            if isinstance(getattr(self, "_tc_pending", None), dict):
                self._tc_pending.pop(self._stream_key(response), None)
            return model_response

        if self._sanitize_forced_text(model_response):
            return model_response

        # Reassemble across deltas FIRST: the single-delta parse below cannot see a call
        # that arrives one fragment at a time, which is how every one of them arrives.
        self._recover_split_tool_call(model_response, response)
        if model_response.tool_calls:
            return model_response

        if model_response.content:
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
