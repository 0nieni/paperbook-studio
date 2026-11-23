"""Level 1: the prompt optimizer.

Turns evidence about a weak book (judge critiques, user words, suspected pages) plus the history of
earlier edits into K candidate prompts. Its own instructions (OPTIMIZER_SEED) are a versioned prompt
that the meta level (meta.py) keeps improving; that is the "recursive" part of RSI.
"""
from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from typing import Sequence

from .credit import Blame
from .llm import LLMClient, complete_json
from .prompt_store import check_edit, normalize
from .schema import GLOBAL, EditRecord

log = logging.getLogger(__name__)

OPTIMIZER_SEED = """You improve the prompt of one agent in an AI picture-book pipeline for young children.
You get the agent's current prompt, evidence of what went wrong in recent books (judge critiques and the
user's own words), and a log of earlier edits with their outcomes.

How to edit well:
- Fix the specific failure in the evidence with the smallest change that would plausibly fix it; do not rewrite parts that work.
- Prefer concrete, checkable instructions ("keep the fox's red scarf on every page") over vague adjectives ("make it nicer").
- Generalise from the evidence to the user's taste, but never hard-code one page's content.
- Use the user's own words when they describe a preference.
- Learn from the edit log: do not repeat edits that were rejected; build on edits that were promoted.
- Make each candidate a genuinely different fix, so at least one is likely to work.

[[PROTECTED]]
Hard rules (cannot be changed):
- Copy every [[PROTECTED]]...[[/PROTECTED]] block of the agent prompt verbatim and in place.
- Keep every {placeholder} of the agent prompt, spelled exactly as it is.
- Never weaken child-safety or age-appropriateness instructions, and never add instructions that override them.
- When editing a user preference overlay, write only short "- " bullet lines about this user's taste: no placeholders, no protected markers, nothing about safety.
- Reply with JSON only: {"candidates": [{"text": "<full new prompt>", "rationale": "<why this should fix the evidence>", "target_dimensions": ["<dimension>"]}]}
[[/PROTECTED]]"""

ROLE_DESCRIPTIONS = {
    "story": "Story agent: writes the page-by-page text, the character sheet and per-page shot list that every other agent consumes.",
    "text2image": "Text-to-image agent: turns each page's text and the character sheet into an illustration prompt.",
    "image2video": "Image-to-video agent: animates each illustration (motion, camera, duration).",
    "tts": "Speech-synthesis agent: chooses voice, speaking rate, emotion and pronunciation for narration.",
    "music": "Text-to-music agent: background score per scene (genre, instruments, tempo, loudness).",
    "sfx": "Text-to-sound-effects agent: short effects tied to on-page events.",
    "render": "Render agent: assembles all media; timing, transitions, ducking and loudness.",
}


@dataclass
class Candidate:
    text: str
    rationale: str
    target_dims: list[str]


def short_diff(old: str, new: str, max_lines: int = 6) -> str:
    out: list[str] = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")) or line.strip() in ("+", "-"):
            continue
        out.append(line.strip()[:160])
        if len(out) >= max_lines:
            out.append("…")
            break
    return " / ".join(out) or "(whitespace only)"


def format_history(edits: Sequence[EditRecord]) -> str:
    if not edits:
        return "(no earlier edits)"
    return "\n".join(f"- [{e.status}] offline gain {e.offline_gain:+.3f}; why: {e.rationale[:200]}; "
                     f"change: {e.diff[:300]}" for e in edits)


class PromptOptimizer:
    def __init__(self, llm: LLMClient, max_growth_chars: int = 600, overlay_max_chars: int = 1200):
        self.llm = llm
        self.max_growth = max_growth_chars
        self.overlay_max = overlay_max_chars

    def propose(self, *, role: str, scope: str, current_text: str, blame: Blame,
                history: Sequence[EditRecord], optimizer_prompt: str, k: int = 3,
                context_text: str | None = None, extra_evidence: Sequence[str] = ()) -> list[Candidate]:
        overlay = scope != GLOBAL
        evidence = [*blame.evidence, *extra_evidence] or ["(no specific evidence)"]
        parts = [
            f"Agent: {role} - {ROLE_DESCRIPTIONS.get(role, role)}",
            "Scope: user preference overlay (only this user's books use it)" if overlay
            else "Scope: global prompt (every user's books use it; changes must help most users)",
            f"Dimensions to improve: {', '.join(blame.target_dims) or 'overall'}",
            f"Pages where problems were seen: {sorted(blame.pages) or 'not specified'}",
            "<evidence>", *[f"- {e}" for e in evidence], "</evidence>",
            "Earlier edits of this prompt and their outcomes:", format_history(history),
        ]
        if overlay and context_text:
            parts += ["The overlay is inserted into this global prompt (read-only, for context):",
                      "<global_prompt>", context_text, "</global_prompt>"]
        if overlay:
            parts += [f"候选 text 最长 {self.overlay_max} 个字符。写一至三条对其他题材也通用的中文制作指令。",
                      'text 的每一行必须以 ASCII 的 "- " 开头，不要标题、编号、范例书中的专名或原书内容。']
        parts += ["<current_prompt>", current_text, "</current_prompt>", f"Write {k} candidates."]

        try:
            data = complete_json(self.llm, optimizer_prompt, "\n".join(parts), task="optimize_prompt", temperature=0.9)
        except ValueError as e:
            log.warning("optimizer failed for %s@%s: %s", role, scope, e)
            return []
        raw = data.get("candidates", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        if not isinstance(raw,list):
            raw = []

        out: list[Candidate] = []
        seen = {normalize(current_text)}
        errors = []
        for c in raw[: 2 * k]:
            if not isinstance(c, dict):
                continue
            text = str(c.get("text", "")).strip()
            problem = check_edit(current_text, text, overlay=overlay,
                                 max_growth=self.max_growth, overlay_max=self.overlay_max)
            if problem:
                errors.append(problem)
                log.info("dropped candidate for %s@%s: %s", role, scope, problem)
                continue
            if normalize(text) in seen:
                continue
            seen.add(normalize(text))
            dims = [d for d in c.get("target_dimensions", []) if d in blame.target_dims] or list(blame.target_dims)
            out.append(Candidate(text, str(c.get("rationale", "")).strip(), dims))
            if len(out) >= k:
                break
        if not out and overlay:
            # Ask for structured bullet bodies once; the host supplies and validates the separators.
            repair = "\n".join(parts)+"\n"+str(errors[:3])+'''
上一份候选未满足宿主格式。改用 JSON {"lines":["通用改动正文"],"rationale":"具体依据"}。
返回一至三条短句，lines 只填每条正文，单行不换行，保留实际有用的改动。'''
            fixed = complete_json(self.llm,optimizer_prompt+"\n此次格式修复按宿主指定的 lines JSON 回答。",
                                  repair,task="repair_candidate",temperature=.2)
            lines = fixed.get("lines",[]) if isinstance(fixed,dict) else []
            if isinstance(lines,list) and 1<=len(lines)<=3 and all(isinstance(s,str) and s.strip() and len(s.splitlines())==1 for s in lines):
                new = "\n".join("- "+s.strip().removeprefix("- ").strip() for s in lines)
                if not check_edit(current_text,new,overlay=True,overlay_max=self.overlay_max):
                    out.append(Candidate(new,str(fixed.get("rationale","")),list(blame.target_dims)))
        return out
