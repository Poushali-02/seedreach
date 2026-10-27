"""Sourcing provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Lead:
    url: str
    source: str
    name_hint: Optional[str] = None
    context: Optional[str] = None


class BaseSourcingProvider(ABC):
    name: str

    @abstractmethod
    def fetch(self, limit: int) -> list[Lead]:
        """Return up to `limit` candidate leads. Must not raise on network failures."""
