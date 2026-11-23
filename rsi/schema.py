"""Shared data types for the RSI (recursive self-improvement) loop."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# Production agents of the pipeline (see the architecture figure).
ROLES: tuple[str, ...] = ("story", "text2image", "image2video", "tts", "interaction")

# The optimizer's own prompt is also a versioned prompt; it is improved by the meta level.
OPTIMIZER_ROLE = "optimizer"

GLOBAL = "global"

# When a role's prompt changes, these components must be regenerated before the book is judged
# again (the edited role itself plus everything that consumes its output).
DOWNSTREAM: dict[str, tuple[str, ...]] = {
    "story": ROLES,
    "text2image": ("text2image", "image2video", "render"),
    "image2video": ("image2video", "render"),
    "tts": ("tts", "render"),
    "music": ("music", "render"),
    "sfx": ("sfx", "render"),
    "render": ("render",),
}


def user_scope(user_id: str) -> str:
    return f"user:{user_id}"


@dataclass
class UserIntent:
    user_id: str
    request: str                                   # e.g. "a bedtime story about a shy little dinosaur"
    child_age: int | None = None
    explicit_prefs: dict[str, str] = field(default_factory=dict)   # e.g. {"art_style": "watercolor"}


@dataclass
class PageAssets:
    index: int
    text: str                       # text read aloud on this page
    caption: str = ""               # what the generated picture actually shows (from a captioning / VLM model)
    image_prompt: str = ""
    video_prompt: str = ""
    narration_prompt: str = ""
    music_prompt: str = ""
    sfx_prompt: str = ""
    media: dict[str, str] = field(default_factory=dict)  # {"image": uri, "video": uri, "audio": uri}


@dataclass
class BookArtifact:
    book_id: str
    intent: UserIntent
    pages: list[PageAssets]
    prompt_texts: dict[str, str]            # role -> fully composed prompt that was sent to that agent
    prompt_versions: dict[str, list[str]]   # role -> [global version id, (user overlay version id)]
    seed: int = 0                           # generation seed, reused when replaying
    auto_metrics: dict[str, float] = field(default_factory=dict)  # 0..1, e.g. clip_text_image, av_sync


@dataclass
class UserFeedback:
    book_id: str
    overall_rating: float | None = None                          # 1..5 stars
    page_reactions: dict[int, int] = field(default_factory=dict)  # page index -> +1 / -1
    comments: list[str] = field(default_factory=list)            # free text from parent (or speech-to-text)
    completion_rate: float | None = None                         # share of pages the child watched
    replays: int = 0
    regenerate_requested: bool = False


@dataclass
class DimensionScore:
    name: str
    score: float            # 0..1
    confidence: float       # 0..1
    critique: str = ""
    pages: list[int] = field(default_factory=list)
    suspects: list[str] = field(default_factory=list)   # roles the judge thinks caused the problem


@dataclass
class ScoreCard:
    book_id: str
    user_id: str
    dims: dict[str, DimensionScore]
    overall: float              # fused score used as the reward
    judge_overall: float        # judge-only score (used for calibration)
    user_overall: float | None  # user-signal-only score, if any
    safety_ok: bool
    safety_notes: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class EditRecord:
    """One attempted prompt edit and what became of it. Feeds the optimizer history and the meta level."""
    id: str
    role: str
    scope: str
    optimizer_id: str           # version id of the optimizer prompt that wrote it
    parent_id: str              # prompt version that was edited
    candidate_id: str | None    # new version id if it was registered as a challenger
    rationale: str
    diff: str
    target_dims: list[str]
    offline_gain: float
    status: str                 # rejected_offline | passed_not_selected | pending_online | promoted | retired
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
