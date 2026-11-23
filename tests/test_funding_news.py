from __future__ import annotations

import httpx
import pytest

from src.config import Settings
from src.pipeline import build_providers
from src.sourcing.funding_news import (
    FEEDS,
    FundingNewsProvider,
    RobotChecker,
    extract_company_url,
)
from src.utils import is_company_host, is_investor_host, non_company_kind

SAMPLE_INC42_ARTICLE = """
<!DOCTYPE html>
<html>
<body>
  <h1>Fintech Startup Zerodrop Raises $2M In Seed Round</h1>
  <p>
    Bengaluru-based fintech startup <a href="https://zerodrop.io?utm_source=inc42&utm_medium=feed">Zerodrop</a> has secured $2M in a seed funding round.
    The round was led by <a href="https://peakxv.com">Peak XV Partners</a> with participation from
    <a href="https://a16z.com/portfolio">Andreessen Horowitz</a>.
  </p>
  <p>
    Follow us on <a href="https://twitter.com/inc42">Twitter</a> or read more at <a href="https://inc42.com/buzz/other-news">Inc42</a>.
    Another startup mentioned is <a href="https://paycorp.dev">PayCorp</a>.
  </p>
</body>
</html>
"""

SAMPLE_NO_COMPANY_ARTICLE = """
<!DOCTYPE html>
<html>
<body>
  <h1>Venture Capital In India: Q3 2026 Overview</h1>
  <p>
    Funding dropped slightly according to reports from <a href="https://techcrunch.com/funding">TechCrunch</a>.
    Top active funds included <a href="https://accel.com">Accel</a> and <a href="https://matrixpartners.in">Matrix Partners</a>.
    Check out our <a href="https://inc42.com/reports">full report here</a>.
  </p>
</body>
</html>
"""

SAMPLE_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Startup Funding News</title>
    <item>
      <title>Disallowed Startup Raises Seed</title>
      <link>https://inc42.com/disallowed-article</link>
      <description>Disallowed by robots</description>
    </item>
    <item>
      <title>Zerodrop Raises Seed Funding</title>
      <link>https://inc42.com/zerodrop-raises-seed</link>
      <description>Fintech seed round</description>
    </item>
    <item>
      <title>Industry Funding Overview</title>
      <link>https://inc42.com/vc-overview</link>
      <description>No company link</description>
    </item>
  </channel>
</rss>
"""


def test_investor_and_news_host_detection():
    assert is_investor_host("peakxv.com")
    assert is_investor_host("a16z.com")
    assert is_investor_host("subdomain.sequoiacap.com")
    assert not is_investor_host("zerodrop.io")

    assert non_company_kind("peakxv.com") == "investor"
    assert non_company_kind("entrackr.com") == "news"
    assert not is_company_host("peakxv.com")
    assert not is_company_host("entrackr.com")
    assert is_company_host("zerodrop.io")


def test_extract_company_url_picks_first_valid_company_and_skips_investors_news_socials():
    company = extract_company_url(SAMPLE_INC42_ARTICLE, "https://inc42.com/buzz/zerodrop-raises-seed")
    assert company == "https://zerodrop.io"


def test_extract_company_url_returns_none_when_no_company():
    company = extract_company_url(SAMPLE_NO_COMPANY_ARTICLE, "https://inc42.com/features/vc-overview")
    assert company is None


def test_robot_checker_respects_robots_txt():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            content = "User-agent: *\nDisallow: /disallowed-article\n"
            return httpx.Response(200, text=content)
        return httpx.Response(200, text="<html><body>OK</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    checker = RobotChecker(client, "TestAgent/1.0")

    assert not checker.is_allowed("https://inc42.com/disallowed-article")
    assert checker.is_allowed("https://inc42.com/zerodrop-raises-seed")


def test_funding_news_provider_yields_leads():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/feed/"):
            return httpx.Response(200, content=SAMPLE_FEED_XML.encode("utf-8"))
        if "/robots.txt" in url:
            content = "User-agent: *\nDisallow: /disallowed-article\n"
            return httpx.Response(200, text=content)
        if "zerodrop-raises-seed" in url:
            return httpx.Response(200, text=SAMPLE_INC42_ARTICLE)
        if "vc-overview" in url:
            return httpx.Response(200, text=SAMPLE_NO_COMPANY_ARTICLE)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = FundingNewsProvider(Settings(), client=client)

    # Monkeypatch FEEDS to only check one feed during this test
    original_feeds = provider.__class__.__module__
    leads = list(provider.iter_leads())

    # Only zerodrop-raises-seed should succeed and yield a lead
    urls = [lead.url for lead in leads]
    assert "https://zerodrop.io" in urls
    zerodrop_lead = next(lead for lead in leads if lead.url == "https://zerodrop.io")
    assert zerodrop_lead.source == "funding"
    assert "India" in zerodrop_lead.regions


def test_build_providers_includes_funding():
    settings = Settings()
    providers = build_providers(settings, "funding")
    assert len(providers) == 1
    assert providers[0].name == "funding"

    all_providers = build_providers(settings, "all")
    assert any(p.name == "funding" for p in all_providers)
