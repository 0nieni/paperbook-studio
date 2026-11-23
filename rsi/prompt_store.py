"""Versioned prompt storage with lineage, A/B status, rollback and invariant checks.

Two scopes exist for every production role:
  * global        - the base prompt every user gets ("{user_preferences}" marks where overlays go)
  * user:<id>     - a short overlay of bullet lines describing one user's taste

The per-user loop only edits overlays (fast, low risk, personal). The global loop edits base
prompts (slow, strictly gated, affects everyone).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field

from .schema import GLOBAL, EditRecord

PROTECTED_RE = re.compile(r"\[\[PROTECTED\]\].*?\[\[/PROTECTED\]\]", re.S)
PLACEHOLDER_RE = re.compile(r"\{[a-z_][a-z0-9_]*\}")
PREFS_SLOT = "{user_preferences}"
EMPTY_PREFS = "- (no stored preferences for this user yet)"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def normalize(text: str) -> str:
    return " ".join(text.split())


def protected_blocks(text: str) -> list[str]:
    return PROTECTED_RE.findall(text)


def placeholders(text: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(text)))


def compose(global_text: str, overlay_text: str) -> str:
    """Build the prompt actually sent to an agent."""
    prefs = overlay_text.strip() or EMPTY_PREFS
    if PREFS_SLOT in global_text:
        return global_text.replace(PREFS_SLOT, prefs)
    return f"{global_text}\n\nUser preferences:\n{prefs}"


def check_edit(old: str, new: str, *, overlay: bool, max_growth: int = 600,
               overlay_max: int = 1200) -> str | None:
    """Return a reason string if `new` violates an invariant, else None.

    These checks are code, not instructions, so an optimizer cannot talk its way past them.
    """
    if not new.strip():
        return "empty prompt"
    if normalize(new) == normalize(old):
        return "no change"
    if overlay:
        if PLACEHOLDER_RE.search(new):
            return "overlay may not contain {placeholders}"
        if "[[PROTECTED]]" in new or "[[/PROTECTED]]" in new:
            return "overlay may not contain protected markers"
        if len(new) > overlay_max:
            return f"overlay longer than {overlay_max} chars"
        bad = [ln for ln in new.splitlines() if ln.strip() and not ln.strip().startswith("- ")]
        if bad:
            return f"overlay lines must be '- ' bullets (got: {bad[0][:60]!r})"
        return None
    if protected_blocks(old) != protected_blocks(new):
        return "a [[PROTECTED]] block was changed, added or removed"
    if placeholders(old) != placeholders(new):
        return f"placeholders changed: {placeholders(old)} -> {placeholders(new)}"
    if len(new) - len(old) > max_growth:
        return f"prompt grew by more than {max_growth} chars"
    return None


@dataclass
class PromptVersion:
    id: str
    role: str
    scope: str
    text: str
    parent_id: str | None = None
    status: str = "active"          # active | challenger | retired
    rationale: str = ""
    created_by: str = "seed"        # "seed", "human", "meta" or the optimizer version id
    created_at: float = field(default_factory=time.time)
    activated_at: float | None = None
    # statistics of the current online experiment (Beta posterior over a [0,1] reward)
    alpha: float = 1.0
    beta: float = 1.0
    n: int = 0
    reward_sum: float = 0.0
    # lifetime statistics
    lifetime_n: int = 0
    lifetime_reward: float = 0.0

    def record(self, reward: float) -> None:
        r = min(1.0, max(0.0, reward))
        self.alpha += r
        self.beta += 1.0 - r
        self.n += 1
        self.reward_sum += r
        self.lifetime_n += 1
        self.lifetime_reward += r

    def posterior_mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def reset_experiment(self, prior_strength: float = 2.0) -> None:
        """Start a fresh experiment, keeping only a weak prior from lifetime performance."""
        m = self.lifetime_reward / self.lifetime_n if self.lifetime_n else 0.5
        self.alpha = 1.0 + prior_strength * m
        self.beta = 1.0 + prior_strength * (1.0 - m)
        self.n = 0
        self.reward_sum = 0.0


class PromptStore:
    """JSON-file backed store. Swap for a database table in production; the interface stays the same."""

    def __init__(self, path: str | None = None):
        self.path = path
        self.versions: dict[str, PromptVersion] = {}
        self.edits: dict[str, EditRecord] = {}
        self.meta: dict[str, float] = {}
        if path and os.path.exists(path):
            self._load()

    # persistence ---------------------------------------------------------------------------
    def _load(self) -> None:
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.versions = {d["id"]: PromptVersion(**d) for d in data.get("versions", [])}
        self.edits = {d["id"]: EditRecord(**d) for d in data.get("edits", [])}
        self.meta = data.get("meta", {})

    def save(self) -> None:
        if not self.path:
            return
        data = {"versions": [asdict(v) for v in self.versions.values()],
                "edits": [asdict(e) for e in self.edits.values()],
                "meta": self.meta}
        folder = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)  # atomic

    # versions ------------------------------------------------------------------------------
    def add_version(self, role: str, scope: str, text: str, *, parent_id: str | None = None,
                    status: str = "challenger", rationale: str = "", created_by: str = "") -> PromptVersion:
        v = PromptVersion(id=new_id("pv"), role=role, scope=scope, text=text, parent_id=parent_id,
                          status=status, rationale=rationale, created_by=created_by)
        if status == "active":
            v.activated_at = time.time()
        self.versions[v.id] = v
        return v

    def seed(self, role: str, text: str, scope: str = GLOBAL) -> PromptVersion:
        existing = self.active(role, scope)
        if existing:
            return existing
        return self.add_version(role, scope, text, status="active", created_by="seed")

    def get(self, vid: str) -> PromptVersion | None:
        return self.versions.get(vid)

    def find(self, role: str, scope: str, status: str | None = None) -> list[PromptVersion]:
        out = [v for v in self.versions.values()
               if v.role == role and v.scope == scope and (status is None or v.status == status)]
        return sorted(out, key=lambda v: v.created_at)

    def active(self, role: str, scope: str) -> PromptVersion | None:
        found = self.find(role, scope, "active")
        return found[-1] if found else None

    def challenger(self, role: str, scope: str) -> PromptVersion | None:
        found = self.find(role, scope, "challenger")
        return found[-1] if found else None

    def promote(self, vid: str) -> None:
        v = self.versions[vid]
        old = self.active(v.role, v.scope)
        if old and old.id != vid:
            old.status = "retired"
        v.status = "active"
        v.activated_at = time.time()

    def retire(self, vid: str) -> None:
        self.versions[vid].status = "retired"

    def rollback(self, role: str, scope: str) -> PromptVersion | None:
        """Manual escape hatch: re-activate the parent of the current active version."""
        cur = self.active(role, scope)
        if not cur or not cur.parent_id or cur.parent_id not in self.versions:
            return None
        self.promote(cur.parent_id)
        return self.versions[cur.parent_id]

    def lineage(self, vid: str) -> list[PromptVersion]:
        chain, cur = [], self.versions.get(vid)
        while cur:
            chain.append(cur)
            cur = self.versions.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(chain))

    def scopes(self, role: str, prefix: str = "user:") -> list[str]:
        return sorted({v.scope for v in self.versions.values() if v.role == role and v.scope.startswith(prefix)})

    # edit log ------------------------------------------------------------------------------
    def add_edit(self, rec: EditRecord) -> None:
        self.edits[rec.id] = rec

    def resolve_edit(self, candidate_id: str, status: str) -> None:
        for e in self.edits.values():
            if e.candidate_id == candidate_id:
                e.status = status
                e.resolved_at = time.time()

    def edits_for(self, role: str, scope: str) -> list[EditRecord]:
        return sorted((e for e in self.edits.values() if e.role == role and e.scope == scope),
                      key=lambda e: e.created_at)
