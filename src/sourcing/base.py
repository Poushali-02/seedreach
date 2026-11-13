"""Sourcing provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from itertools import islice
from typing import Iterable, Iterator, Optional


@dataclass(frozen=True)
class Lead:
    url: str
    source: str
    name_hint: Optional[str] = None
    context: Optional[str] = None
    # where the company says it hires, when the source knows (YC's region names, "Remote" for HN's
    # remote-only roles). Empty means unknown. Used only to rank leads with PREFER_REGIONS.
    regions: tuple[str, ...] = ()


def parse_preferences(raw: str) -> tuple[str, ...]:
    """`"India, Remote"` -> ("india", "remote"): lower-cased, in order, without blanks or repeats."""
    out: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def region_rank(regions: Iterable[str], preferences: tuple[str, ...]) -> int:
    """Position of the first preferred region the lead lists; len(preferences) when it lists none.

    Names match whole and ignore case, so "Remote" doesn't match YC's separate "Partly Remote".
    """
    names = {str(r).strip().lower() for r in regions}
    for i, preferred in enumerate(preferences):
        if preferred in names:
            return i
    return len(preferences)


class BaseSourcingProvider(ABC):
    name: str

    @abstractmethod
    def iter_leads(self) -> Iterator[Lead]:
        """Yield candidate leads lazily, best first. Must not raise on network failures.

        Lazy on purpose: the pipeline skips companies it has already handled, so it may need to read
        far past the first few leads to find new ones.
        """

    def fetch(self, limit: int) -> list[Lead]:
        return list(islice(self.iter_leads(), limit))
