"""Optional Langfuse tracing: one trace per question, a generation per LLM call, a span per tool call.

Enabled when LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST are set; otherwise a no-op,
so the assistant and the tests never depend on it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


class NullTrace:
    id: str | None = None

    def generation(self, **kwargs: Any) -> None: ...

    def tool(self, **kwargs: Any) -> None: ...

    def end(self, **kwargs: Any) -> None: ...


class LangfuseTrace(NullTrace):
    def __init__(self, client, name: str, question: str, metadata: dict[str, Any]):
        self._trace = client.trace(name=name, input=question, metadata=metadata)
        self.id = self._trace.id

    def generation(self, *, model: str, messages: list, output: Any, start: datetime, end: datetime,
                   prompt_tokens: int, completion_tokens: int) -> None:
        self._trace.generation(
            name="llm", model=model, input=messages, output=output, start_time=start, end_time=end,
            usage={"input": prompt_tokens, "output": completion_tokens, "unit": "TOKENS"},
        )

    def tool(self, *, name: str, arguments: dict, output: str, summary: str, start: datetime, end: datetime) -> None:
        self._trace.span(name=name, input=arguments, output=output[:4000], start_time=start, end_time=end,
                         metadata={"summary": summary})

    def end(self, *, output: str, metadata: dict[str, Any]) -> None:
        self._trace.update(output=output, metadata=metadata)


class Tracer:
    def __init__(self):
        self.client = None
        if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
            try:
                from langfuse import Langfuse

                self.client = Langfuse()  # reads LANGFUSE_HOST / keys from the environment
            except Exception as exc:  # tracing must never break answering
                log.warning("Langfuse disabled: %s", exc)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def start(self, question: str, **metadata: Any) -> NullTrace:
        if not self.client:
            return NullTrace()
        try:
            return LangfuseTrace(self.client, "ask", question, metadata)
        except Exception as exc:
            log.warning("Langfuse trace failed: %s", exc)
            return NullTrace()

    def flush(self) -> None:
        if self.client:
            self.client.flush()


def now() -> datetime:
    return datetime.now(timezone.utc)
