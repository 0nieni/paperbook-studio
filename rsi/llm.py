"""Minimal LLM interface. Plug in whatever model API the project already uses."""
from __future__ import annotations

import json
import re
from typing import Any, Protocol


class LLMClient(Protocol):
    def complete(self, system: str, user: str, *, task: str, temperature: float = 0.7) -> str:
        """Return the model's text reply.

        `task` is one of "judge", "optimize_prompt", "meta_optimize". Use it to route tasks to
        different models (e.g. a strong multimodal model for judging) and for logging / cost tracking.
        """
        ...


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(text: str) -> Any:
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        end = max(text.rfind("}"), text.rfind("]"))
        if not starts or end <= min(starts):
            raise
        return json.loads(text[min(starts): end + 1])


def complete_json(llm: LLMClient, system: str, user: str, *, task: str,
                  temperature: float = 0.2, retries: int = 2) -> Any:
    last: Exception | None = None
    for _ in range(retries + 1):
        raw = llm.complete(system, user, task=task, temperature=temperature)
        try:
            return parse_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            last = e
            user = user + "\n\nYour previous reply was not valid JSON. Reply with JSON only."
    raise ValueError(f"LLM returned invalid JSON for task={task}: {last}")
