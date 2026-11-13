"""A short, phone-friendly summary of one run: where to apply, who to reach, which drafts to review.

`python main.py run --digest digest.md` writes it as Markdown. The scheduled-runs template posts it as a
comment on a GitHub issue, so it reaches you through GitHub's mobile notifications. It never contains
email addresses, only company names, roles, public apply links and LinkedIn search links.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from src.utils import linkedin_people_search

if TYPE_CHECKING:  # pragma: no cover
    from src.pipeline import Result


def _name(r: "Result") -> str:
    return f"**{r.company or r.domain}** ({r.domain})"


def _size(r: "Result") -> str:
    return f", {r.size}" if r.size else ""


def build_digest(results: Iterable["Result"], *, dry_run: bool = False) -> str:
    """Markdown with up to three sections; an empty string when nothing in the run needs you."""
    results = list(results)
    apply = [r for r in results if r.role and r.apply_url]
    reach = [r for r in results if r.outcome == "skipped" and r.founder and r.detail.startswith("needs contact")]
    drafts = [r for r in results if r.outcome == "drafted"]
    if not (apply or reach or drafts):
        return ""

    lines: list[str] = []
    if dry_run:
        lines.append("_Dry run: nothing was saved to Gmail or the database._\n")
    if apply:
        lines.append("### Apply")
        lines += [f"- {_name(r)}: [{r.role}]({r.apply_url}){_size(r)}" for r in apply]
        lines.append("")
    if reach:
        lines.append("### Reach the founder on LinkedIn")
        lines.append("Only a generic or guessed address was found, so no draft was made.")
        lines += [f"- {_name(r)}: {r.founder}, [find on LinkedIn]({linkedin_people_search(r.founder, r.company or '')})"
                  f"{_size(r)}" for r in reach]
        lines.append("")
    if drafts:
        lines.append("### Drafts to review in Gmail")
        lines += [f"- {_name(r)}{': ' + r.role if r.role else ''}{_size(r)}" for r in drafts]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
