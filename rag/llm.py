"""Minimal Ollama chat client (local LLM, no API key, no data leaving the machine)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests


@dataclass
class LlmReply:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{"name": ..., "arguments": {...}}]
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatModel(Protocol):
    model: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LlmReply: ...


class OllamaChat:
    def __init__(self, base_url: str, model: str, timeout_s: int = 300, temperature: float = 0.0, num_ctx: int = 8192):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.options = {"temperature": temperature, "num_ctx": num_ctx}
        self.session = requests.Session()

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LlmReply:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False, "options": self.options}
        if tools:
            body["tools"] = tools
        resp = self.session.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message") or {}
        calls = [
            {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or {}}
            for c in msg.get("tool_calls") or []
            if c.get("function", {}).get("name")
        ]
        content = msg.get("content") or ""
        if "</think>" in content:  # reasoning block (the opening tag is sometimes in the template, not the output)
            content = content.rsplit("</think>", 1)[1]
        content = _THINK.sub("", content).strip()
        if not calls:
            calls, content = parse_text_tool_calls(content)
        return LlmReply(
            content=content,
            tool_calls=calls,
            prompt_tokens=data.get("prompt_eval_count", 0) or 0,
            completion_tokens=data.get("eval_count", 0) or 0,
        )

    def available(self) -> bool:
        try:
            tags = self.session.get(f"{self.base_url}/api/tags", timeout=5).json().get("models", [])
        except requests.RequestException:
            return False
        names = {m.get("name") for m in tags} | {m.get("model") for m in tags}
        return self.model in names or f"{self.model}:latest" in names


_THINK = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)


def parse_text_tool_calls(content: str) -> tuple[list[dict[str, Any]], str]:
    """Small models sometimes write the tool call as text (`<tool_call>{...}</tool_call>` or bare JSON)
    instead of using the structured field: recover it rather than showing JSON to the user."""
    if "<tool_call>" not in content and not content.startswith("{"):
        return [], content
    decoder = json.JSONDecoder()
    calls = []
    for match in re.finditer(r"\{", content):
        if "<tool_call>" in content and "<tool_call>" not in content[: match.start()]:
            continue
        try:
            obj, _ = decoder.raw_decode(content, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("name"), str):
            args = obj.get("arguments") or obj.get("parameters") or {}
            call = {"name": obj["name"], "arguments": args}
            if call not in calls:
                calls.append(call)
    return (calls, "") if calls else ([], content)
