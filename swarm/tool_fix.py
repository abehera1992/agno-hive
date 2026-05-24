"""OllamaToolFix — handles non-standard tool call output from Ollama models.

Supported formats:
  1. <tool_call>{"name": ..., "arguments": ...}</tool_call> (qwen2.5)
  2. <|python_tag|>{"type": "function", "name": ..., "parameters": ...} (llama3.3)
  3. {"name": ..., "arguments": ...} bare JSON (qwen2.5 non-streaming)
  4. Multiple bare JSON objects concatenated with whitespace
  5. JSON embedded in prose (qwen2.5-coder in explanatory text)

NOTE: agno-internal tools (delegate_task_to_member, delegate_task_to_members) are
intentionally excluded from MCP tool call interception so agno can handle them natively.
"""

# agno team internal tools — do not strip from content, let agno handle natively
_AGNO_INTERNAL_TOOLS = {"delegate_task_to_member", "delegate_task_to_members", "get_member_information"}
import json
import re
from typing import Any

from agno.models.ollama import Ollama
from agno.models.response import ModelResponse


class OllamaToolFix(Ollama):

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

    def _parse_provider_response(self, response: dict) -> ModelResponse:
        model_response = super()._parse_provider_response(response)

        if model_response.tool_calls:
            for tc in (model_response.tool_calls or []):
                name = getattr(getattr(tc, 'function', None), 'name', None) or (tc.get('function',{}).get('name','?') if isinstance(tc, dict) else '?')
                print(f'[tool_fix:native] {name}', flush=True)
            return model_response

        if model_response.content:
            parsed = self._parse_tool_calls_from_content(model_response.content)
            if parsed:
                tool_calls = self._to_tool_calls(parsed)
                if tool_calls:  # only strip content if valid named tool calls found
                    model_response.tool_calls = tool_calls
                    model_response.content = ""
                    for tc in tool_calls:
                        print(f"[tool_fix] {tc.get('function',{}).get('name','?')}({list(tc.get('function',{}).get('arguments',{}).keys())})", flush=True)

        return model_response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        model_response = super()._parse_provider_response_delta(response)

        if model_response.tool_calls:
            for tc in (model_response.tool_calls or []):
                name = getattr(getattr(tc, 'function', None), 'name', None) or (tc.get('function',{}).get('name','?') if isinstance(tc, dict) else '?')
                print(f'[tool_fix:native] {name}', flush=True)
            return model_response

        if model_response.content:
            parsed = self._parse_tool_calls_from_content(model_response.content)
            if parsed:
                tool_calls = self._to_tool_calls(parsed)
                if tool_calls:  # only strip content if valid named tool calls found
                    model_response.tool_calls = tool_calls
                    model_response.content = ""

        return model_response
