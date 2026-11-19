"""Candidate profile, loaded from profile.json (git-ignored) so no personal data lives in code."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from src.config import ROOT

PROFILE_PATH = ROOT / "profile.json"
_REQUIRED = ("name", "intro", "role_pitch", "experience", "stack", "projects")


class ProfileError(Exception):
    """profile.json is missing or malformed."""


@dataclass(frozen=True)
class Profile:
    name: str
    intro: str  # e.g. "a Computer Science student (graduating 2028) based in Berlin"
    role_pitch: str  # completes "I'd love to join <Company> as ..."
    experience: str  # one or two sentences on your most relevant experience
    stack: str
    projects: dict[str, list[str]]  # core_domain -> paragraphs; "Other" is the required fallback
    portfolio: str = ""
    github: str = ""
    linkedin: str = ""
    resume: str = ""
    phone: str = ""
    subject: str = "Internship inquiry - {company}"
    closing: str = "If it would be useful, I'm happy to share more detail or jump on a short call."
    email_style: str = "short"  # "short": ~120 words, one project, one ask; "long": every section below
    ask: str = ""  # the short email's last line; may use {company}. Empty: a default ask
    # Your own topics, picked by words on the company's site rather than by the model:
    # {"Sports": ["sport", "cricket"]} uses projects["Sports"] when one of those words appears.
    keywords: dict[str, list[str]] = field(default_factory=dict)

    @property
    def first_name(self) -> str:
        return self.name.split()[0]

    def topic_for(self, text: str) -> str:
        """The first of your `keywords` topics with a word in `text` (whole words, plurals too), or ""."""
        for topic, words in self.keywords.items():
            if topic in self.projects and any(
                    re.search(rf"\b{re.escape(w.strip())}s?\b", text, re.I) for w in words if w.strip()):
                return topic
        return ""

    def projects_for(self, core_domain: str, text: str = "") -> list[str]:
        """Paragraphs for a company: a `keywords` topic found in `text` wins over the model's domain."""
        return self.projects.get(self.topic_for(text) or core_domain) or self.projects["Other"]


def load_profile(path: Path = PROFILE_PATH) -> Profile:
    if not path.exists():
        raise ProfileError(
            f"{path.name} not found. Copy profile.example.json to profile.json and fill in your details."
        )
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"{path.name} is not valid JSON: {exc}") from exc

    known = {f.name for f in fields(Profile)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ProfileError(f"Unknown field(s) in {path.name}: {', '.join(unknown)}")
    missing = [k for k in _REQUIRED if not data.get(k)]
    if missing:
        raise ProfileError(f"Missing required field(s) in {path.name}: {', '.join(missing)}")

    if data.get("email_style", "short") not in ("short", "long"):
        raise ProfileError(f'"email_style" in {path.name} must be "short" or "long".')
    projects = data["projects"]
    if not isinstance(projects, dict) or "Other" not in projects:
        raise ProfileError('"projects" must be an object that includes an "Other" fallback entry.')
    data["projects"] = {k: [v] if isinstance(v, str) else list(v) for k, v in projects.items()}
    empty = [k for k, v in data["projects"].items() if not any(str(p).strip() for p in v)]
    if empty:  # every draft quotes a project paragraph, so an empty entry would fail every draft
        raise ProfileError(f'"projects" entries need at least one paragraph; empty: {", ".join(empty)}.')
    keywords = data.get("keywords", {})
    if not isinstance(keywords, dict) or set(keywords) - set(data["projects"]):
        raise ProfileError('Every topic under "keywords" needs an entry of the same name under "projects".')
    data["keywords"] = {k: [v] if isinstance(v, str) else list(v) for k, v in keywords.items()}
    return Profile(**data)
