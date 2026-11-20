"""Draft a plain-text cold email whose project matches the company's domain, and its one follow-up.

Two styles, chosen by `email_style` in profile.json:
  short (default)  greeting, the opener, who you are, one project, one ask, two links: ~100-130 words
  long             every section: role pitch, all matching projects, experience, stack, closing, all links
A follow-up (`generate_followup`) is always short: a reminder, the same ask, the resume.
For LinkedIn (`generate_linkedin`): a connection note within the note limit, and a longer message to send once
they accept. Text only: you paste them yourself; seedreach never touches LinkedIn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
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


def company_text(company: CompanyProfile) -> str:
    """What we know the company does, in words: the text a profile's `keywords` are looked for in."""
    return f"{company.company_name}. {company.one_line_summary} {company.personalized_hook}"


def _short(company: CompanyProfile, profile: Profile, name: str, greeting: str, role: str) -> list[str]:
    proof = _first_sentence(profile.projects_for(company.core_domain, company_text(company))[0])
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
        "\n\n".join(profile.projects_for(company.core_domain, company_text(company))),
        f"{profile.experience} My core stack is {profile.stack}.",
        profile.closing,
        "\n".join(_links(profile, 3)),
        f"Best,\n{signature}",
    ]


@dataclass(frozen=True)
class LinkedInText:
    note: str     # the connection-request note, within the note limit
    message: str  # the longer message to send once they accept


NOTE_ASK = "Would love to connect."
_CLAUSE_BREAKS = (", and ", ", so ", "; ", " and I ", ", which ", " - ")


def _fit_sentence(sentence: str, budget: int) -> str:
    """The sentence, or its first clause, ending with a full stop, within `budget` characters; else ""."""
    s = sentence.strip().rstrip(".?!")
    if len(s) + 1 <= budget:
        return s + "."
    for sep in _CLAUSE_BREAKS:
        head = s.split(sep, 1)[0].rstrip(",")
        if 30 <= len(head) and len(head) + 1 <= budget:
            return head + "."
    return ""


def generate_linkedin(company: CompanyProfile, profile: Profile, limit: int = 200,
                      role: Optional[str] = None) -> Optional[LinkedInText]:
    """A connection note (at most `limit` characters) and a longer after-accept message, for a named founder.

    The note is: hi, who you are, the opener shortened to a full clause, an ask to connect. It never ends
    mid-word: parts that don't fit are dropped, the opener first. None when the site names no founder,
    since a note goes to a person.
    """
    first = strip_markdown(company.founder_first_name or "").split(" ")[0]
    if not first:
        return None
    name = display_name(company.company_name)
    intro = re.sub(r"\s*\([^)]*\)", "", profile.intro).strip().rstrip(".")  # "(expected 2028)" etc. dropped
    greeting, me = f"Hi {first},", f"I'm {profile.first_name}, {intro}."
    if len(f"{greeting} {me} {NOTE_ASK}") > limit:
        me = f"I'm {profile.first_name}."
    budget = limit - len(f"{greeting} {me}  {NOTE_ASK}")
    hook = _fit_sentence(strip_markdown(company.personalized_hook), budget) or \
        _fit_sentence(f"I came across {name}", budget)
    note = " ".join(p for p in (greeting, me, hook, NOTE_ASK) if p)
    if len(note) > limit:  # a very long name or a tiny limit: the bare minimum
        note = f"{greeting} {NOTE_ASK}"[:limit]

    sections = _short(company, profile, name, greeting, _role_line(role))
    sections[0] = f"{greeting} thanks for connecting!"
    sections[-1] = profile.first_name  # a message, not a letter: no "Best," block
    message = "\n\n".join(s for s in sections if s) + "\n"
    _assert_plain_text(note)
    _assert_plain_text(message)
    return LinkedInText(note=note, message=message)


def generate_followup(company_name: str, founder_name: Optional[str], profile: Profile, sent_at: datetime,
                      subject: str = "") -> EmailDraft:
    """A short nudge, once, about a week after your first email: same ask, the resume, nothing else.

    `subject` is the first email's subject as it was sent; the reply keeps it ("Re: ...") so Gmail threads it.
    """
    name = display_name(company_name)
    first = strip_markdown(founder_name).split()[0] if founder_name and strip_markdown(founder_name) else ""
    ask = (profile.ask or DEFAULT_ASK).replace("{company}", name)
    sections = [
        f"Hi {first}," if first else f"Hi {name} team,",
        f"Following up on my email from {sent_at.day} {sent_at:%B}, in case it got buried. {ask}",
        "\n".join(_links(profile, 0)),
        f"Thanks,\n{profile.name}",
    ]
    body = "\n\n".join(s for s in sections if s) + "\n"
    original = subject or profile.subject.replace("{company}", name)
    reply_subject = original if re.match(r"\s*re\s*:", original, re.I) else f"Re: {original}"
    _assert_plain_text(body)
    _assert_plain_text(reply_subject)
    return EmailDraft(subject=reply_subject, body=body)


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
