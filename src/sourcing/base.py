"""Sourcing provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from itertools import islice
from typing import Iterator, Optional


@dataclass(frozen=True)
class Lead:
    url: str
    source: str
    name_hint: Optional[str] = None
    context: Optional[str] = None


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
