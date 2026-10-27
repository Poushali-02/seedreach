"""Draft a plain-text cold email whose project paragraph matches the company's domain."""

from __future__ import annotations

from dataclasses import dataclass

from src.intelligence.extractor import CompanyProfile
from src.profile import Profile
from src.utils import strip_markdown


@dataclass(frozen=True)
class EmailDraft:
    subject: str
    body: str


def _assert_plain_text(text: str) -> None:
    bad = [c for c in "*#`" if c in text]
    if bad:
        raise ValueError(f"Email contains markdown characters: {bad}")


def generate_email(company: CompanyProfile, profile: Profile) -> EmailDraft:
    name = strip_markdown(company.company_name)
    first = strip_markdown(company.founder_first_name) if company.founder_first_name else ""
    greeting = f"Hi {first}," if first else f"Hi {name} team,"
    projects = "\n\n".join(profile.projects_for(company.core_domain))

    links = [
        f"{label}: {value}"
        for label, value in (
            ("Portfolio", profile.portfolio), ("GitHub", profile.github),
            ("LinkedIn", profile.linkedin), ("Resume", profile.resume),
        )
        if value
    ]
    signature = "\n".join(filter(None, [profile.name, profile.phone]))

    sections = [
        greeting,
        company.personalized_hook,
        f"I'm {profile.first_name}, {profile.intro}, and I'd love to join {name} as {profile.role_pitch}",
        projects,
        f"{profile.experience} My core stack is {profile.stack}.",
        profile.closing,
        "\n".join(links),
        f"Best,\n{signature}",
    ]
    body = "\n\n".join(s for s in sections if s) + "\n"
    subject = profile.subject.replace("{company}", name)
    _assert_plain_text(body)
    _assert_plain_text(subject)
    return EmailDraft(subject=subject, body=body)
