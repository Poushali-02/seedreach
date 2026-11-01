"""What happened to one company in one run, step by step: timings, tokens and what each step saw."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional


@dataclass
class Trace:
    domain: str
    source: str
    outcome: str = ""
    detail: str = ""
    pages: int = 0  # pages whose text reached the LLM
    text_chars: int = 0
    truncated: bool = False  # text was cut, so a team page may have been lost
    model: str = ""
    llm_calls: int = 0  # more than 1 means the model's output had to be retried
    input_tokens: int = 0
    output_tokens: int = 0
    founder_in_text: Optional[bool] = None  # None when no founder was named
    email_source: str = ""
    ms: dict[str, int] = field(default_factory=dict)  # step -> milliseconds

    @contextmanager
    def timed(self, step: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.ms[step] = round((time.perf_counter() - start) * 1000)


def name_in_text(first: Optional[str], last: Optional[str], text: str) -> Optional[bool]:
    """Whether a named founder actually appears in the page (False means the model got it elsewhere)."""
    if not first:
        return None
    name = " ".join(p for p in (first, last) if p)
    return re.search(rf"\b{re.escape(name)}\b", text, re.I) is not None
