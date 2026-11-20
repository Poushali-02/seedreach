"""A short, phone-friendly summary of one run: where to apply, who to reach, which drafts to review,
and the LinkedIn note and after-accept message for each named founder, ready to copy.

`python main.py run --digest digest.md` writes it as Markdown. The scheduled-runs template posts it as a
comment on a GitHub issue, so it reaches you through GitHub's mobile notifications. It never contains
email addresses, only company names, roles, public apply links, LinkedIn search links and LinkedIn text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Sequence

from src.utils import linkedin_people_search

if TYPE_CHECKING:  # pragma: no cover
    from src.pipeline import Result
    from src.sourcing.job_alerts import AlertJob


def _name(r: "Result") -> str:
    return f"**{r.company or r.domain}** ({r.domain})"


def _size(r: "Result") -> str:
    return f", {r.size}" if r.size else ""


def _quote(text: str) -> list[str]:
    """Markdown blockquote lines: they wrap on a phone screen, where a code block would scroll sideways."""
    return [f"> {line}" if line else ">" for line in text.strip().splitlines()]


def _linkedin(r: "Result") -> list[str]:
    """The note to paste when connecting, and the message for later, folded so the digest stays short."""
    link = linkedin_people_search(r.founder or "", r.company or "")
    return [f"{_name(r)}: {r.founder}, [find on LinkedIn]({link})", "", *_quote(r.linkedin.note), "",
            "<details><summary>After they accept</summary>", "", *_quote(r.linkedin.message), "", "</details>", ""]


def _posted(r: "Result") -> str:
    d = r.role_posted
    return f", posted {d.day} {d:%b %Y}" if d else ""


def build_digest(results: Iterable["Result"], *, dry_run: bool = False,
                 alerts: Sequence["AlertJob"] = ()) -> str:
    """Markdown with up to six sections; an empty string when nothing in the run needs you."""
    results = list(results)
    apply = [r for r in results if r.role and r.apply_url]
    reach = [r for r in results if r.outcome == "skipped" and r.founder and r.detail.startswith("needs contact")]
    drafts = [r for r in results if r.outcome == "drafted"]
    linkedin = [r for r in results if r.linkedin and r.founder]
    if not (apply or reach or drafts or alerts or linkedin):
        return ""

    lines: list[str] = []
    if dry_run:
        lines.append("_Dry run: nothing was saved to Gmail or the database._\n")
    if apply:
        lines.append("### Apply")
        lines += [f"- {_name(r)}: [{r.role}]({r.apply_url}){_posted(r)}{_size(r)}" for r in apply]
        lines.append("")
    if alerts:
        lines.append("### From your job alerts")
        lines += [f"- **{a.company}**: [{a.role}]({a.apply_url}){', ' + a.location if a.location else ''} ({a.portal})"
                  for a in alerts]
        lines.append("")
    if reach:
        lines.append("### Reach the founder on LinkedIn")
        lines.append("Only a generic or guessed address was found, so no draft was made.")
        for r in reach:
            lines.append(f"- {_name(r)}: {r.founder}, [find on LinkedIn]({linkedin_people_search(r.founder, r.company or '')})"
                         f"{_size(r)}")
        lines.append("")
    if drafts:
        lines.append("### Drafts to review in Gmail")
        lines += [f"- {_name(r)}{': ' + r.role if r.role else ''}{_size(r)}" for r in drafts]
        lines.append("")
    if linkedin:  # #20: copy from the GitHub app; seedreach never uses LinkedIn itself
        lines.append("### LinkedIn notes")
        lines.append("Paste the note when you connect; send the message once they accept.")
        lines.append("")
        for r in linkedin:
            lines += _linkedin(r)
    return "\n".join(lines).rstrip() + "\n"
