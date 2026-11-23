"""Level 2: improving the optimizer itself (the recursive step of RSI).

Recursion levels, deliberately bounded:
  L0  production prompts (story, text2image, ...)  - edited by L1, judged by the fixed rubric
  L1  optimizer prompt                             - edited by L2, judged by how its edits fare
  L2  META_SYSTEM below                            - frozen in code; only humans change it

Fitness of an optimizer version = mean utility of the edits it wrote:
  promoted 1.0 | passed offline but not selected 0.4 | retired online 0.25 | rejected offline 0.0
A rewritten optimizer prompt is A/B tested against the current one exactly like production prompts.
"""
from __future__ import annotations

import random
import time

from .llm import LLMClient, complete_json
from .optimizer import OPTIMIZER_SEED, format_history
from .prompt_store import PromptStore, PromptVersion, check_edit
from .schema import GLOBAL, OPTIMIZER_ROLE, EditRecord

META_SYSTEM = """You maintain the instructions of a prompt optimizer ("the optimizer prompt"). The optimizer edits
the prompts of agents in a children's picture-book pipeline. You get its current text and a log of the edits it
produced, each with an outcome:
  promoted = it truly helped users; passed_not_selected = good but another candidate was better;
  retired = passed offline tests but users did not prefer it; rejected_offline = failed tests or validation.

Rewrite the optimizer prompt so it produces more promoted edits and fewer rejected ones. Look for patterns:
what did successful edits have in common, and which mistakes recur in rejected ones? Turn them into short,
general guidance. Do not add rules about particular users, stories or pages. Keep it concise.
Copy the [[PROTECTED]] block verbatim.
Reply with JSON only: {"text": "<new optimizer prompt>", "rationale": "<patterns you found>"}"""

UTILITY = {"promoted": 1.0, "passed_not_selected": 0.4, "retired": 0.25, "rejected_offline": 0.0}


class MetaOptimizer:
    def __init__(self, llm: LLMClient, store: PromptStore, rng: random.Random, every_n_edits: int = 8,
                 min_edits: int = 6, max_edits: int = 24, margin: float = 0.05):
        self.llm, self.store, self.rng = llm, store, rng
        self.every_n, self.min_edits, self.max_edits, self.margin = every_n_edits, min_edits, max_edits, margin

    def ensure_seed(self) -> None:
        self.store.seed(OPTIMIZER_ROLE, OPTIMIZER_SEED)

    def _resolved(self, optimizer_id: str, since: float | None = None) -> list[EditRecord]:
        return sorted((e for e in self.store.edits.values()
                       if e.optimizer_id == optimizer_id and e.status in UTILITY
                       and (since is None or e.created_at >= since)), key=lambda e: e.created_at)

    def fitness(self, optimizer_id: str, since: float | None = None) -> tuple[float, int]:
        edits = self._resolved(optimizer_id, since)
        if not edits:
            return 0.0, 0
        return sum(UTILITY[e.status] for e in edits) / len(edits), len(edits)

    def choose(self) -> PromptVersion:
        """Which optimizer prompt writes the next edit (Thompson sampling on fitness)."""
        a = self.store.active(OPTIMIZER_ROLE, GLOBAL)
        c = self.store.challenger(OPTIMIZER_ROLE, GLOBAL)
        if c is None:
            return a

        def draw(vid: str, since: float | None) -> float:
            m, n = self.fitness(vid, since)
            return self.rng.betavariate(1 + m * n, 1 + (1 - m) * n)

        return c if draw(c.id, None) >= draw(a.id, c.created_at) else a

    def maybe_step(self) -> list[str]:
        a = self.store.active(OPTIMIZER_ROLE, GLOBAL)
        c = self.store.challenger(OPTIMIZER_ROLE, GLOBAL)
        if c is not None:
            ma, na = self.fitness(a.id, since=c.created_at)
            mc, nc = self.fitness(c.id)
            if nc < self.min_edits or (na < self.min_edits and nc < self.max_edits):
                return []
            self.store.meta["meta_last_step"] = time.time()
            if mc > ma + self.margin:
                self.store.promote(c.id)
                return [f"meta: promoted optimizer prompt {c.id} (fitness {mc:.2f} vs {ma:.2f})"]
            self.store.retire(c.id)
            return [f"meta: retired optimizer prompt {c.id} (fitness {mc:.2f} vs {ma:.2f})"]

        since = max(a.activated_at or 0.0, self.store.meta.get("meta_last_step", 0.0))
        resolved = self._resolved(a.id, since)
        if len(resolved) < self.every_n:
            return []
        self.store.meta["meta_last_step"] = time.time()
        m, n = self.fitness(a.id, since)
        user = "\n".join([
            f"Current fitness: {m:.2f} over {n} edits.",
            "Edits written with the current optimizer prompt:",
            format_history(resolved[-40:]),
            "<optimizer_prompt>", a.text, "</optimizer_prompt>",
        ])
        try:
            data = complete_json(self.llm, META_SYSTEM, user, task="meta_optimize", temperature=0.7)
        except ValueError as e:
            return [f"meta: optimizer rewrite failed ({e})"]
        text = str(data.get("text", "")).strip() if isinstance(data, dict) else ""
        problem = check_edit(a.text, text, overlay=False, max_growth=1500)
        if problem:
            return [f"meta: proposed optimizer prompt rejected ({problem})"]
        v = self.store.add_version(OPTIMIZER_ROLE, GLOBAL, text, parent_id=a.id, status="challenger",
                                   rationale=str(data.get("rationale", "")), created_by="meta")
        return [f"meta: new optimizer prompt {v.id} under A/B test (current fitness {m:.2f} over {n} edits)"]
