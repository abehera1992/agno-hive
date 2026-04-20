"""OllamaToolFix — handles qwen2.5-coder's non-standard tool call output.

qwen2.5-coder emits tool calls as raw JSON in the `content` field rather than
in Ollama's `tool_calls` API field. This subclass overrides _parse_provider_response
and _parse_provider_response_delta to intercept that content and convert it to
proper tool_calls before Agno processes the response.

Handled formats:
  1. <tool_call>{"name": ..., "arguments": ...}</tool_call> blocks
  2. Two or more bare JSON objects concatenated with whitespace
  3. A single bare JSON object as the entire content
"""
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

        # Format 2 / 3: bare JSON objects
        stripped = content.strip()
        if not stripped.startswith("{"):
            return calls

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

        return calls

    def _to_tool_calls(self, parsed: list[dict]) -> list[dict]:
        result = []
        for call in parsed:
            name = call.get("name") or call.get("function", {}).get("name")
            args = call.get("arguments") or call.get("function", {}).get("arguments", {})
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
            return model_response

        if model_response.content:
            parsed = self._parse_tool_calls_from_content(model_response.content)
            if parsed:
                model_response.tool_calls = self._to_tool_calls(parsed)
                model_response.content = ""

        return model_response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        model_response = super()._parse_provider_response_delta(response)

        if model_response.tool_calls:
            return model_response

        if model_response.content:
            parsed = self._parse_tool_calls_from_content(model_response.content)
            if parsed:
                model_response.tool_calls = self._to_tool_calls(parsed)
                model_response.content = ""

        return model_response
