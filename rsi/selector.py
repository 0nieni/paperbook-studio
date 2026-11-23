"""Is the edited prompt really better? Two gates.

1. OfflineGate - replay: regenerate only the edited component (and its downstream) for a few books
   with the SAME seed, once with the incumbent prompt and once with the candidate, and judge both.
   Paired comparison cancels most generation noise. Rejects edits that do not raise the targeted
   dimensions, that regress other dimensions, that fail safety, or (globally) that help only a few users.
2. OnlineBandit - the surviving candidate becomes a "challenger" and is Thompson-sampled against the
   incumbent on real books; real user reward decides promotion or retirement.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from .evaluator import Evaluator
from .prompt_store import PromptStore, PromptVersion
from .schema import BookArtifact, ScoreCard


class Replayer(Protocol):
    def replay(self, book: BookArtifact, role: str, prompt_text: str, seed: int) -> BookArtifact:
        """Regenerate `book` with `role` using `prompt_text`, re-running that role and everything in
        DOWNSTREAM[role]. All other components, and the seed, stay as they were."""
        ...


@dataclass
class GateConfig:
    min_gain: float = 0.03           # mean gain required on the targeted dimensions (0..1 scale)
    max_regression: float = 0.05     # largest allowed drop on any other dimension
    replay_books: int = 2
    min_book_win_rate: float = 0.0   # share of replayed books that must improve (use >0.5 globally)
    win_epsilon: float = 0.01


@dataclass
class GateResult:
    accepted: bool
    target_gain: float
    overall_gain: float
    worst_regression: float
    regressed_dim: str | None
    book_win_rate: float
    safety_ok: bool
    reason: str


class OfflineGate:
    def __init__(self, evaluator: Evaluator, replayer: Replayer, cache_size: int = 512):
        self.evaluator = evaluator
        self.replayer = replayer
        self.cache_size = cache_size
        self._baseline_cache: dict[str, ScoreCard] = {}

    def _replay_and_score(self, book: BookArtifact, role: str, text: str,
                          prefs: list[str], cache: bool) -> ScoreCard:
        key = hashlib.sha1(f"{book.book_id}|{role}|{book.seed}|{text}|{'|'.join(prefs)}".encode()).hexdigest()
        if cache and key in self._baseline_cache:
            return self._baseline_cache[key]
        replayed = self.replayer.replay(book, role, text, book.seed)
        card = self.evaluator.score(replayed, None, prefs, use_user_signals=False)
        if cache:
            if len(self._baseline_cache) >= self.cache_size:
                self._baseline_cache.clear()
            self._baseline_cache[key] = card
        return card

    def evaluate(self, role: str, baseline_text: str, candidate_text: str, books: Sequence[BookArtifact],
                 target_dims: Sequence[str], cfg: GateConfig,
                 prefs_for: Callable[[BookArtifact], Sequence[str]]) -> GateResult:
        if not books:
            return GateResult(False, 0.0, 0.0, 0.0, None, 0.0, True, "no books to replay")
        names = [d.name for d in self.evaluator.rubric]
        sums = dict.fromkeys(names, 0.0)
        overall, wins, safety = 0.0, 0, True
        for book in books:
            prefs = list(prefs_for(book))
            base = self._replay_and_score(book, role, baseline_text, prefs, cache=True)
            cand = self._replay_and_score(book, role, candidate_text, prefs, cache=False)
            safety = safety and cand.safety_ok
            per_book = []
            for n in names:
                delta = cand.dims[n].score - base.dims[n].score
                sums[n] += delta
                if n in target_dims:
                    per_book.append(delta)
            d_overall = cand.overall - base.overall
            overall += d_overall
            wins += (sum(per_book) / len(per_book) if per_book else d_overall) > cfg.win_epsilon

        k = len(books)
        mean = {n: s / k for n, s in sums.items()}
        targets = [n for n in target_dims if n in mean]
        target_gain = sum(mean[n] for n in targets) / len(targets) if targets else overall / k
        others = {n: v for n, v in mean.items() if n not in targets}
        regressed_dim, worst = min(others.items(), key=lambda kv: kv[1]) if others else (None, 0.0)
        regression = max(0.0, -worst)
        win_rate = wins / k

        reasons = []
        if not safety:
            reasons.append("safety failure on replay")
        if target_gain < cfg.min_gain:
            reasons.append(f"target gain {target_gain:+.3f} < {cfg.min_gain}")
        if overall / k <= 0:
            reasons.append(f"overall gain {overall / k:+.3f} <= 0")
        if regression > cfg.max_regression:
            reasons.append(f"{regressed_dim} regressed by {regression:.3f}")
        if win_rate < cfg.min_book_win_rate:
            reasons.append(f"improved only {win_rate:.0%} of books")
        return GateResult(not reasons, target_gain, overall / k, regression,
                          regressed_dim if regression > 0 else None, win_rate, safety,
                          "; ".join(reasons) or "passed")


@dataclass
class BanditConfig:
    min_trials: int = 3        # challenger books before any decision
    max_trials: int = 12       # force a decision after this many
    promote_p: float = 0.9     # promote when P(challenger > incumbent) >= this
    retire_p: float = 0.1      # retire when it is <= this
    mc_samples: int = 4000


class OnlineBandit:
    def __init__(self, store: PromptStore, cfg: BanditConfig, rng: random.Random):
        self.store, self.cfg, self.rng = store, cfg, rng

    def choose(self, role: str, scope: str) -> PromptVersion | None:
        a, c = self.store.active(role, scope), self.store.challenger(role, scope)
        if a is None or c is None:
            return a or c
        return c if self.rng.betavariate(c.alpha, c.beta) >= self.rng.betavariate(a.alpha, a.beta) else a

    def p_better(self, c: PromptVersion, a: PromptVersion) -> float:
        n = self.cfg.mc_samples
        b = self.rng.betavariate
        return sum(b(c.alpha, c.beta) > b(a.alpha, a.beta) for _ in range(n)) / n

    def decide(self, role: str, scope: str) -> tuple[str, PromptVersion] | None:
        a, c = self.store.active(role, scope), self.store.challenger(role, scope)
        if a is None or c is None or c.n < self.cfg.min_trials:
            return None
        p = self.p_better(c, a)
        if p >= self.cfg.promote_p:
            return "promote", c
        if p <= self.cfg.retire_p:
            return "retire", c
        if c.n >= self.cfg.max_trials:
            return ("promote", c) if c.posterior_mean() > a.posterior_mean() else ("retire", c)
        return None
