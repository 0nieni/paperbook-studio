"""Scoring a finished picture book.

score = fusion of three evidence sources
  1. LLM / VLM judge against a FIXED rubric (gives per-dimension critiques + suspected components)
  2. automatic metrics the pipeline already computes (CLIP text-image similarity, face consistency, ...)
  3. the user's own signals (stars, page thumbs, comments, completion, replays, "regenerate")

The rubric and judge prompt are deliberately outside the optimization loop: the optimizer may never
edit what it is graded on (anti reward-hacking). Change them by hand, and re-check calibration.
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Sequence

from .llm import LLMClient, complete_json
from .schema import ROLES, BookArtifact, DimensionScore, ScoreCard, UserFeedback


@dataclass(frozen=True)
class Dimension:
    name: str
    description: str
    weight: float
    owners: dict[str, float]          # prior responsibility of each role (sums to 1)
    auto_metric: str | None = None    # key in BookArtifact.auto_metrics, if one exists


RUBRIC: tuple[Dimension, ...] = (
    Dimension("intent_alignment",
              "Delivers what this user asked for and evidently wanted: topic, characters, tone, length, "
              "explicit preferences and anything they said in feedback.",
              0.30, {"story": 0.55, "text2image": 0.20, "tts": 0.10, "music": 0.15}),
    Dimension("narrative_quality",
              "Coherent arc, age-appropriate vocabulary, rhythm that reads well aloud, satisfying ending.",
              0.15, {"story": 1.0}),
    Dimension("style_and_mood_fit",
              "Art style, voice, music and sound effects share one mood that matches the requested style.",
              0.15, {"text2image": 0.45, "music": 0.25, "tts": 0.15, "sfx": 0.15}),
    Dimension("character_consistency",
              "Recurring characters look the same on every page (face, colours, clothing, proportions).",
              0.12, {"text2image": 0.7, "image2video": 0.3}, "character_consistency"),
    Dimension("text_image_alignment",
              "Each page's picture shows what that page's text says.",
              0.10, {"text2image": 0.8, "story": 0.2}, "clip_text_image"),
    Dimension("motion_quality",
              "Animation is smooth and on-model, without warping or flicker; motion supports the story beat.",
              0.06, {"image2video": 1.0}, "motion_quality"),
    Dimension("narration_quality",
              "Narration is clear, correctly pronounced, expressive and paced for the child's age.",
              0.07, {"tts": 1.0}, "tts_mos"),
    Dimension("av_sync_and_pacing",
              "Narration, page turns, music and effects are in sync; nothing clipped, too loud or too long.",
              0.05, {"render": 0.7, "tts": 0.15, "image2video": 0.15}, "av_sync"),
)

JUDGE_SYSTEM = """You are the quality judge for an AI picture-book product for young children.
You score one generated book against a fixed rubric.

Score each dimension from 1 (very poor) to 10 (excellent). Be strict and specific: a critique must name
the pages and the concrete problem, and say what the user would have preferred when the evidence shows it.
For each problem, list the pipeline components you suspect caused it, chosen from:
story, text2image, image2video, tts, music, sfx, render.

What the user has told us (comments, page reactions, stated preferences) is the best evidence of what
they had in mind. Weigh it above your own taste.

Separately decide whether the book is safe for a child of the stated age (nothing frightening, violent,
sexual, hateful or dangerous; no personal data). Safety is pass/fail.

Reply with JSON only:
{"dimensions": {"<name>": {"score": <1-10>, "critique": "<text>", "pages": [<int>...], "suspects": ["<component>"...]}, ...},
 "safety": {"ok": true|false, "notes": "<text>"}}"""


def _clip(s: str, n: int = 400) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclass
class FusionWeights:
    judge: float = 1.0                  # multiplied by judge confidence
    auto: float = 1.0
    user_on_intent: float = 2.0         # user signal is the strongest evidence of intent
    user_on_overall: float = 0.5        # share of overall taken from user signal when present
    engagement_on_overall: float = 0.1


def user_signal(fb: UserFeedback | None) -> float | None:
    if fb is None:
        return None
    parts: list[tuple[float, float]] = []
    if fb.overall_rating is not None:
        parts.append(((fb.overall_rating - 1.0) / 4.0, 1.0))
    if fb.page_reactions:
        vals = list(fb.page_reactions.values())
        parts.append((sum(1 for v in vals if v > 0) / len(vals), 0.5))
    s = sum(v * w for v, w in parts) / sum(w for _, w in parts) if parts else None
    if fb.regenerate_requested:
        s = 0.3 if s is None else min(s, 0.3)
    return None if s is None else min(1.0, max(0.0, s))


def engagement_signal(fb: UserFeedback | None) -> float | None:
    if fb is None or fb.completion_rate is None:
        return None
    return 0.8 * fb.completion_rate + 0.2 * min(fb.replays, 3) / 3


class Evaluator:
    def __init__(self, llm: LLMClient, rubric: Sequence[Dimension] = RUBRIC,
                 judge_samples: int = 1, fusion: FusionWeights | None = None):
        self.llm = llm
        self.rubric = tuple(rubric)
        self.judge_samples = max(1, judge_samples)
        self.fusion = fusion or FusionWeights()

    # judge ---------------------------------------------------------------------------------
    def _judge_prompt(self, book: BookArtifact, fb: UserFeedback | None, known_prefs: Sequence[str]) -> str:
        it = book.intent
        lines = [f"Book {book.book_id} for user {it.user_id}; child age: {it.child_age or 'unknown'}",
                 f"Request: {it.request}"]
        if it.explicit_prefs:
            lines.append("Explicit preferences: " + "; ".join(f"{k}={v}" for k, v in it.explicit_prefs.items()))
        if known_prefs:
            lines.append("What this user has told us before (authoritative):")
            lines += [f"  - {_clip(c, 300)}" for c in known_prefs]
        # Star rating and "regenerate" are NOT shown, so judge-vs-user calibration stays meaningful.
        if fb and fb.comments:
            lines.append("User comments on this book:")
            lines += [f"  - {_clip(c, 300)}" for c in fb.comments]
        if fb and fb.page_reactions:
            lines.append("Page reactions: " + ", ".join(
                f"p{p}:{'+' if v > 0 else '-'}" for p, v in sorted(fb.page_reactions.items())))
        if book.auto_metrics:
            lines.append("Automatic metrics (0-1): " + ", ".join(f"{k}={v:.2f}" for k, v in book.auto_metrics.items()))
        lines.append("\nRubric:")
        lines += [f"- {d.name}: {d.description}" for d in self.rubric]
        lines.append("\nPages:")
        for p in book.pages:
            lines += [f"Page {p.index}",
                      f"  text: {_clip(p.text)}",
                      f"  picture (as generated): {_clip(p.caption)}",
                      f"  image_prompt: {_clip(p.image_prompt)}",
                      f"  video_prompt: {_clip(p.video_prompt)}",
                      f"  narration_prompt: {_clip(p.narration_prompt)}",
                      f"  music_prompt: {_clip(p.music_prompt)}",
                      f"  sfx_prompt: {_clip(p.sfx_prompt)}"]
        return "\n".join(lines)

    def judge(self, book: BookArtifact, fb: UserFeedback | None = None,
              known_prefs: Sequence[str] = ()) -> tuple[dict[str, DimensionScore], bool, str]:
        prompt = self._judge_prompt(book, fb, known_prefs)
        temp = 0.3 if self.judge_samples > 1 else 0.0
        runs = [complete_json(self.llm, JUDGE_SYSTEM, prompt, task="judge", temperature=temp)
                for _ in range(self.judge_samples)]
        runs = [r if isinstance(r, dict) else {} for r in runs]
        dims: dict[str, DimensionScore] = {}
        for d in self.rubric:
            entries = [r.get("dimensions", {}).get(d.name) for r in runs]
            entries = [e for e in entries if isinstance(e, dict) and "score" in e]
            if not entries:
                dims[d.name] = DimensionScore(d.name, 0.5, 0.1, "judge returned no score")
                continue
            vals = [min(1.0, max(0.0, (float(e["score"]) - 1.0) / 9.0)) for e in entries]
            spread = statistics.pstdev(vals) if len(vals) > 1 else 0.15
            critiques = list(dict.fromkeys(str(e.get("critique", "")).strip() for e in entries if e.get("critique")))
            pages = sorted({int(p) for e in entries for p in e.get("pages", []) or []
                            if str(p).lstrip("-").isdigit()})
            suspects = sorted({s for e in entries for s in e.get("suspects", []) or [] if s in ROLES})
            dims[d.name] = DimensionScore(d.name, sum(vals) / len(vals), max(0.1, 1.0 - 2.0 * spread),
                                          " | ".join(critiques), pages, suspects)
        safety = [r.get("safety") or {} for r in runs]
        ok = all(bool(s.get("ok", False)) for s in safety)   # a missing verdict counts as a fail
        notes = " | ".join(str(s["notes"]) for s in safety if s.get("notes"))
        return dims, ok, notes

    # fusion --------------------------------------------------------------------------------
    def score(self, book: BookArtifact, fb: UserFeedback | None = None,
              known_prefs: Sequence[str] = (), use_user_signals: bool = True) -> ScoreCard:
        dims, safety_ok, notes = self.judge(book, fb, known_prefs)
        f = self.fusion
        total_w = sum(d.weight for d in self.rubric)
        judge_overall = sum(d.weight * dims[d.name].score for d in self.rubric) / total_w
        u = user_signal(fb) if use_user_signals else None

        for d in self.rubric:
            ds = dims[d.name]
            parts = [(ds.score, f.judge * ds.confidence)]
            if d.auto_metric and d.auto_metric in book.auto_metrics:
                parts.append((float(book.auto_metrics[d.auto_metric]), f.auto))
            if u is not None and d.name == "intent_alignment":
                parts.append((u, f.user_on_intent))
            ds.score = sum(v * w for v, w in parts) / sum(w for _, w in parts)
            ds.confidence = min(1.0, ds.confidence + 0.15 * (len(parts) - 1))

        overall = sum(d.weight * dims[d.name].score for d in self.rubric) / total_w
        if u is not None:
            overall = (1 - f.user_on_overall) * overall + f.user_on_overall * u
        e = engagement_signal(fb) if use_user_signals else None
        if e is not None:
            overall = (1 - f.engagement_on_overall) * overall + f.engagement_on_overall * e
        return ScoreCard(book.book_id, book.intent.user_id, dims, overall, judge_overall, u, safety_ok, notes)


@dataclass
class JudgeCalibration:
    """Tracks how well the judge agrees with real users. If agreement drops, stop trusting the
    offline gate and get humans to fix the rubric / judge model before the loop optimizes for noise."""
    window: int = 200
    pairs: deque = field(default_factory=deque)

    def add(self, judge_score: float, user_score: float) -> None:
        self.pairs.append((judge_score, user_score))
        while len(self.pairs) > self.window:
            self.pairs.popleft()

    def correlation(self, min_n: int = 8) -> float | None:
        if len(self.pairs) < min_n:
            return None
        xs, ys = zip(*self.pairs)
        try:
            return statistics.correlation(xs, ys)
        except statistics.StatisticsError:
            return None
