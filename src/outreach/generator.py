"""Draft a plain-text cold email whose project matches the company's domain.

Two styles, chosen by `email_style` in profile.json:
  short (default)  greeting, the opener, who you are, one project, one ask, two links: ~100-130 words
  long             every section: role pitch, all matching projects, experience, stack, closing, all links
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from src.intelligence.extractor import CompanyProfile
from src.profile import Profile
from src.utils import display_name, strip_markdown

DEFAULT_ASK = "Would you be open to a remote intern at {company}? I'd be glad to start with a short trial task."
_SENTENCE = re.compile(r"(.+?[.!?])(?=\s+[A-Z(\"']|\s*$)", re.S)


@dataclass(frozen=True)
class EmailDraft:
    subject: str
    body: str


def _assert_plain_text(text: str) -> None:
    bad = [c for c in "*#`" if c in text]
    if bad:
        raise ValueError(f"Email contains markdown characters: {bad}")


def _first_sentence(paragraph: str) -> str:
    """'I built X, a Y. It does Z.' -> 'I built X, a Y.' (dots inside names like OSV.dev don't count)."""
    match = _SENTENCE.match(paragraph.strip())
    return match.group(1).strip() if match else paragraph.strip()


def _links(profile: Profile, limit: int) -> list[str]:
    """Up to `limit` of portfolio, GitHub and LinkedIn, then the resume whenever it's set.

    The resume is what an internship ask needs most, so it is never the link that gets cut.
    """
    labelled = [("Portfolio", profile.portfolio), ("GitHub", profile.github), ("LinkedIn", profile.linkedin)]
    links = [f"{label}: {value}" for label, value in labelled if value][:limit]
    if profile.resume:
        links.append(f"Resume: {profile.resume}")
    return links


def _role_line(role: Optional[str]) -> str:
    """'I saw your Backend Intern opening.' for a role read from the company's job board, else ''."""
    if not role:
        return ""
    title = re.sub(r"\s+", " ", strip_markdown(role)).strip()
    if len(title) > 90:
        title = title[:90].rsplit(" ", 1)[0]
    return f"I saw your {title} opening." if title else ""


def _short(company: CompanyProfile, profile: Profile, name: str, greeting: str, role: str) -> list[str]:
    proof = _first_sentence(profile.projects_for(company.core_domain)[0])
    ask = (profile.ask or DEFAULT_ASK).replace("{company}", name)
    return [
        greeting,
        company.personalized_hook,
        f"I'm {profile.first_name}, {profile.intro.rstrip('.')}. {proof}",
        f"{role} {ask}" if role else ask,
        "\n".join(_links(profile, 2)),
        f"Best,\n{profile.name}",
    ]


def _long(company: CompanyProfile, profile: Profile, name: str, greeting: str, role: str) -> list[str]:
    signature = "\n".join(filter(None, [profile.name, profile.phone]))
    pitch = f"I'm {profile.first_name}, {profile.intro}, and I'd love to join {name} as {profile.role_pitch}"
    return [
        greeting,
        company.personalized_hook,
        f"{role} {pitch}" if role else pitch,
        "\n\n".join(profile.projects_for(company.core_domain)),
        f"{profile.experience} My core stack is {profile.stack}.",
        profile.closing,
        "\n".join(_links(profile, 3)),
        f"Best,\n{signature}",
    ]


def generate_email(company: CompanyProfile, profile: Profile, role: Optional[str] = None) -> EmailDraft:
    """`role`: an open internship read from the company's job board, named in the draft when given."""
    name = display_name(company.company_name)
    first = strip_markdown(company.founder_first_name) if company.founder_first_name else ""
    greeting = f"Hi {first}," if first else f"Hi {name} team,"
    style = _long if profile.email_style == "long" else _short
    sections = style(company, profile, name, greeting, _role_line(role))
    body = "\n\n".join(s for s in sections if s) + "\n"
    subject = profile.subject.replace("{company}", name)
    _assert_plain_text(body)
    _assert_plain_text(subject)
    return EmailDraft(subject=subject, body=body)
