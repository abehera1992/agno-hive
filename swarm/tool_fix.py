"""OllamaToolFix — handles qwen2.5-coder's non-standard tool call output.

qwen2.5-coder (7b and 32b) emits tool calls as raw JSON in the `content`
field rather than in Ollama's `tool_calls` API field. Agno's default Ollama
integration only reads `tool_calls`, so the agentic loop stalls without this fix.

Handled formats:
  1. <tool_call>{"name": ..., "arguments": ...}</tool_call> blocks
  2. Two or more bare JSON objects concatenated with whitespace
  3. A single bare JSON object as the entire content
"""
import json
import re
from typing import Any

from agno.models.ollama import Ollama


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

    def _populate_assistant_message_from_stream_data(
        self, assistant_message: Any, data: Any
    ) -> None:
        super()._populate_assistant_message_from_stream_data(assistant_message, data)

        if getattr(assistant_message, "tool_calls", None):
            return

        content = getattr(assistant_message, "content", None)
        if not content or not isinstance(content, str):
            return

        parsed = self._parse_tool_calls_from_content(content)
        if not parsed:
            return

        tool_calls = []
        for call in parsed:
            name = call.get("name") or call.get("function", {}).get("name")
            args = call.get("arguments") or call.get("function", {}).get("arguments", {})
            if name:
                tool_calls.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args) if isinstance(args, dict) else args,
                    },
                })

        if tool_calls:
            assistant_message.tool_calls = tool_calls
            # Suppress raw JSON so it isn't shown as prose
            stripped = content.strip()
            if stripped.startswith("{") or stripped.startswith("<tool_call>"):
                assistant_message.content = ""
