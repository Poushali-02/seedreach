"""Company size and stage from Apollo's organization enrichment (only when APOLLO_API_KEY is set).

GET https://api.apollo.io/api/v1/organizations/enrich?domain=<domain> returns an `organization` with,
among others, `estimated_num_employees`, `latest_funding_stage`, `total_funding_printed` and
`founded_year`. It is available on Apollo's free plan; the people endpoints are not.

Checked on real companies (2026-11-13): headcount is the reliable size signal. The funding stage is not:
Ramp (2,200 people), Vercel (550) and Stripe (9,400) are all "Venture (Round not Specified)", and
Palantir has no stage at all. Country is also unreliable (an Indian YC company came back as United
States), so it isn't used. Some domains return no data; an unknown company is never skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)

ENRICH_URL = "https://api.apollo.io/api/v1/organizations/enrich"


@dataclass(frozen=True)
class CompanyFacts:
    employees: Optional[int] = None
    funding_stage: str = ""  # e.g. "Seed"; informational only (see the module docstring)
    total_funding: str = ""  # e.g. "8.2M"
    founded_year: Optional[int] = None

    def summary(self) -> str:
        """'Seed, ~7 people' for run output; '' when Apollo knew nothing useful."""
        parts = [self.funding_stage] if self.funding_stage else []
        if self.employees:
            parts.append(f"~{self.employees} people")
        return ", ".join(parts)


class CompanyFactsClient:
    """Looks up a company by domain. Never raises; turns itself off for the run if Apollo refuses the key."""

    def __init__(self, api_key: str, timeout: float = 15.0, client: Optional[httpx.Client] = None) -> None:
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=timeout)
        self.enabled = bool(api_key)

    def lookup(self, domain: str) -> Optional[CompanyFacts]:
        if not self.enabled:
            return None
        try:
            resp = self.client.get(ENRICH_URL, params={"domain": domain},
                                   headers={"X-Api-Key": self.api_key, "Cache-Control": "no-cache"})
        except httpx.HTTPError as exc:
            log.debug("Apollo company lookup for %s failed: %s", domain, exc)
            return None
        if resp.status_code in (401, 403):  # bad key, or not on this plan: same answer for every company
            self.enabled = False
            log.warning("Apollo company lookups are off for this run (HTTP %s): %s",
                        resp.status_code, _error_text(resp))
            return None
        if resp.status_code != 200:
            log.debug("Apollo company lookup for %s: HTTP %s", domain, resp.status_code)
            return None
        try:
            org = resp.json().get("organization") or {}
        except (ValueError, AttributeError):
            return None
        if not isinstance(org, dict) or not org:
            return None
        employees = org.get("estimated_num_employees")
        founded = org.get("founded_year")
        return CompanyFacts(
            employees=employees if isinstance(employees, int) and employees > 0 else None,
            funding_stage=str(org.get("latest_funding_stage") or ""),
            total_funding=str(org.get("total_funding_printed") or ""),
            founded_year=founded if isinstance(founded, int) else None,
        )


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:160]
    return str(data.get("error") or data)[:160] if isinstance(data, dict) else str(data)[:160]
