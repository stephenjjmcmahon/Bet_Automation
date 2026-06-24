"""Provider-agnostic LLM adapter for the search agent and its intent classifier.

Only the new search code goes through this layer; the existing ai_interpreter
calls stay on the OpenAI SDK directly. The agent loop talks to `LLMClient` in a
normalized message/tool-call shape, so swapping OpenAI for Claude later means
adding one adapter class here — not rewriting the agent.

Normalized message shapes the agent builds (the adapter translates to/from the
provider wire format):
  {"role": "user", "content": str}
  {"role": "assistant", "content": str | None, "tool_calls": [ToolCall, ...]}
  {"role": "tool", "tool_call_id": str, "content": str}

Normalized tool definition (parameters is a JSON Schema dict):
  {"name": str, "description": str, "parameters": {...}}
"""
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    text: Optional[str] = None
    tool_calls: list = field(default_factory=list)


class LLMClient:
    """Interface every provider adapter implements."""

    def complete(self, system: str, messages: list, tools: list = None,
                 temperature: float = 0) -> LLMResponse:
        raise NotImplementedError


class OpenAIClient(LLMClient):
    def __init__(self, model: str = "gpt-4o"):
        self._client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=30.0)
        self.model = model

    def complete(self, system, messages, tools=None, temperature=0) -> LLMResponse:
        payload_messages = [{"role": "system", "content": system}]
        payload_messages.extend(self._to_openai(m) for m in messages)

        kwargs = {
            "model": self.model,
            "temperature": temperature,
            "messages": payload_messages,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ]

        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        tool_calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        return LLMResponse(text=msg.content, tool_calls=tool_calls)

    @staticmethod
    def _to_openai(m: dict) -> dict:
        role = m["role"]
        if role == "assistant" and m.get("tool_calls"):
            return {
                "role": "assistant",
                "content": m.get("content"),
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in m["tool_calls"]
                ],
            }
        if role == "tool":
            return {
                "role": "tool",
                "tool_call_id": m["tool_call_id"],
                "content": m["content"],
            }
        return {"role": role, "content": m.get("content", "")}


def get_llm(model: str = "gpt-4o") -> LLMClient:
    """Adapter selected by the LLM_PROVIDER env var (default 'openai'). A future
    Claude swap adds an 'anthropic' branch returning an AnthropicClient."""
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    if provider == "openai":
        return OpenAIClient(model=model)
    raise ValueError(f"Unsupported LLM_PROVIDER: {provider!r}")
