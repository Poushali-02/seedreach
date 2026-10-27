"""Candidate profile, loaded from profile.json (git-ignored) so no personal data lives in code."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
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

    @property
    def first_name(self) -> str:
        return self.name.split()[0]

    def projects_for(self, core_domain: str) -> list[str]:
        return self.projects.get(core_domain) or self.projects["Other"]


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

    projects = data["projects"]
    if not isinstance(projects, dict) or "Other" not in projects:
        raise ProfileError('"projects" must be an object that includes an "Other" fallback entry.')
    data["projects"] = {k: [v] if isinstance(v, str) else list(v) for k, v in projects.items()}
    return Profile(**data)
