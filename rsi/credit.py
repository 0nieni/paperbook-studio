"""Credit assignment: which agent's prompt is to blame for a weak book?

blame(role) = Σ_dims  deficit(dim) × weight(dim) × confidence × share(role, dim)

share() blends the rubric's prior ownership with the judge's per-problem "suspects", so a style
complaint caused by the music is not pinned on the illustrator. Roles whose recent edits kept failing
are damped, which shifts attention to the co-owners of the problem.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from .evaluator import RUBRIC, Dimension
from .schema import ROLES, DimensionScore, ScoreCard, UserFeedback

MIN_SHARE_FOR_TARGET = 0.15


@dataclass
class Blame:
    role: str
    amount: float = 0.0
    target_dims: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    pages: set[int] = field(default_factory=set)


def assign_credit(card: ScoreCard, fb: UserFeedback | None = None, rubric: Sequence[Dimension] = RUBRIC,
                  target: float = 0.8, suspect_share: float = 0.5, min_blame: float = 0.005,
                  damping: dict[str, float] | None = None) -> list[Blame]:
    blames = {r: Blame(r) for r in ROLES}
    for dim in rubric:
        ds = card.dims.get(dim.name)
        if ds is None:
            continue
        deficit = max(0.0, target - ds.score) * dim.weight * (0.5 + 0.5 * ds.confidence)
        if deficit <= 0:
            continue
        shares = dict(dim.owners)
        suspects = [s for s in ds.suspects if s in ROLES]
        if suspects:
            shares = {r: (1 - suspect_share) * dim.owners.get(r, 0.0)
                      + (suspect_share / len(suspects) if r in suspects else 0.0)
                      for r in set(dim.owners) | set(suspects)}
        for role, share in shares.items():
            if share <= 0:
                continue
            b = blames[role]
            b.amount += deficit * share
            if share >= MIN_SHARE_FOR_TARGET:
                b.target_dims.append(dim.name)
                b.evidence.append(f"[{dim.name} = {ds.score:.2f}] {ds.critique or '(no critique)'}")
                b.pages |= set(ds.pages)

    if fb is not None:
        user_lines = [f"[user said] {c}" for c in fb.comments]
        user_lines += [f"[user disliked page {p}]" for p, v in sorted(fb.page_reactions.items()) if v < 0]
        if fb.regenerate_requested:
            user_lines.append("[user asked to regenerate the whole book]")
        for b in blames.values():
            if b.target_dims:
                b.evidence += user_lines

    for role, factor in (damping or {}).items():
        if role in blames:
            blames[role].amount *= factor

    out = [b for b in blames.values() if b.amount >= min_blame and b.target_dims]
    return sorted(out, key=lambda b: -b.amount)


def aggregate_cards(cards: Sequence[ScoreCard], rubric: Sequence[Dimension] = RUBRIC) -> ScoreCard:
    """Average many score cards into one population-level card (input to the global loop)."""
    dims: dict[str, DimensionScore] = {}
    for d in rubric:
        ds = [c.dims[d.name] for c in cards if d.name in c.dims]
        if not ds:
            continue
        worst = sorted(ds, key=lambda x: x.score)[:6]
        critiques = list(dict.fromkeys(x.critique for x in worst if x.critique))
        sus = Counter(s for x in ds for s in set(x.suspects))
        dims[d.name] = DimensionScore(
            d.name,
            sum(x.score for x in ds) / len(ds),
            sum(x.confidence for x in ds) / len(ds),
            " | ".join(critiques),
            [],
            [s for s, n in sus.items() if n >= 0.3 * len(ds)],
        )
    total_w = sum(d.weight for d in rubric if d.name in dims) or 1.0
    overall = sum(d.weight * dims[d.name].score for d in rubric if d.name in dims) / total_w
    return ScoreCard("aggregate", "*", dims, overall, overall, None, True)
