from __future__ import annotations

import ast
import base64
import re
from pathlib import Path

import pytest

import src.outreach.gmail_client as gmail_client
from src.config import Settings
from src.db import Database
from src.enrichment import email_finder
from src.enrichment.email_finder import EmailFinder
from src.intelligence.extractor import CompanyProfile
from src.intelligence.scraper import _parse
from src.config import ROOT
from src.outreach.generator import generate_email
from src.profile import ProfileError, load_profile
from src.sourcing.hacker_news import parse_job_comment
from src.utils import domain_of, normalize_url


def _company(domain: str, **kw) -> CompanyProfile:
    base = dict(
        company_name="Shiplog", one_line_summary="Changelogs.", core_domain=domain,
        founder_first_name="Ada", founder_last_name=None,
        personalized_hook="I was curious how Shiplog diffs release notes across monorepo packages.",
    )
    return CompanyProfile(**{**base, **kw})


# ---- utils ------------------------------------------------------------
def test_domain_normalisation():
    assert domain_of("useshiplog.com") == "useshiplog.com"
    assert domain_of("https://www.app.foo.co.uk/x") == "foo.co.uk"
    assert domain_of("https://foo.vercel.app") == "foo.vercel.app"
    with pytest.raises(ValueError):
        normalize_url("not a url")


# ---- db ---------------------------------------------------------------
def test_db_dedupes_domain_and_email(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    assert db.queue("a.com", "https://a.com", "manual") is True
    assert db.queue("a.com", "https://a.com", "manual") is False
    assert not db.is_known("a.com")  # queued is not "done"
    db.mark_drafted("a.com", "https://a.com", "manual", company_name="A", founder_name=None,
                    founder_email="Ada@a.com", email_source="apollo", draft_id="d1")
    assert db.is_known("a.com") and db.email_taken("ada@a.com")
    db.mark_skipped("b.com", "https://b.com", "hn", "no email")
    assert db.is_known("b.com")
    assert db.stats() == {"total": 2, "queued": 0, "drafted": 1, "skipped": 1}


# ---- profile + generator ----------------------------------------------
PROFILE = load_profile(ROOT / "profile.example.json")


@pytest.mark.parametrize("domain,expected", [
    ("DevTools", ["ExampleScan"]),
    ("Fintech", ["ExamplePay"]),
    ("AI Agents", ["ExampleAgent"]),
    ("Other", ["ExampleCRM"]),
])
def test_generator_matches_domain(domain: str, expected: list[str]):
    body = generate_email(_company(domain), PROFILE).body
    for needle in expected:
        assert needle in body
    for needle in ["ExampleCo", "https://example.dev", "github.com/example", "Alex Example"]:
        assert needle in body


def test_generator_is_plain_text_even_with_markdown_input():
    c = _company("Other", company_name="**Acme** #1", personalized_hook="I wondered about `x` and **y**.")
    draft = generate_email(c, PROFILE)
    assert not re.search(r"[*#`]", draft.body + draft.subject)


def test_generator_greeting_and_empty_optional_fields():
    body = generate_email(_company("Other", founder_first_name=None), PROFILE).body
    assert body.startswith("Hi Shiplog team,")
    assert "Phone" not in body and body.rstrip().endswith("Alex Example")  # phone is blank in the example


def test_profile_validation(tmp_path: Path):
    with pytest.raises(ProfileError, match="not found"):
        load_profile(tmp_path / "missing.json")
    bad = tmp_path / "p.json"
    bad.write_text('{"name": "A"}', encoding="utf-8")
    with pytest.raises(ProfileError, match="Missing required"):
        load_profile(bad)
    bad.write_text('{"nme": "A"}', encoding="utf-8")
    with pytest.raises(ProfileError, match="Unknown field"):
        load_profile(bad)


# ---- email finder -----------------------------------------------------
def _finder() -> EmailFinder:
    return EmailFinder(Settings(apollo_api_key=""))


def test_mailto_prefers_founder_and_skips_support():
    f = _finder()
    r = f.find("x.com", "X", "Ada", None, ["support@x.com", "ada@x.com", "bob@other.com"])
    assert (r.email, r.source) == ("ada@x.com", "mailto")
    r = f.find("x.com", "X", None, None, ["support@x.com", "hello@x.com"])
    assert r.email == "hello@x.com" and r.confidence == "generic"
    assert f.find("x.com", "X", None, None, ["support@x.com"]) is None


def test_heuristic_requires_mx(monkeypatch: pytest.MonkeyPatch):
    f = _finder()
    monkeypatch.setattr(email_finder, "has_mx", lambda d: True)
    assert f.find("x.com", "X", "Zoë", None, []).email == "zoe@x.com"
    monkeypatch.setattr(email_finder, "has_mx", lambda d: False)
    assert f.find("x.com", "X", "Ada", None, []) is None


# ---- scraper / sourcing parsing ---------------------------------------
def test_scraper_extracts_text_meta_mailto_and_about_links():
    html = """<html><head><title>Shiplog</title><meta name="description" content="Changelogs"></head>
    <body><script>x()</script><h1>Ship notes</h1><a href="mailto:ada@shiplog.io?subject=hi">mail</a>
    <a href="/about">About</a><p>Built by Ada Lovelace.</p></body></html>"""
    page, extra = _parse(html, "https://shiplog.io")
    assert page.title == "Shiplog" and page.meta["description"] == "Changelogs"
    assert "ada@shiplog.io" in page.mailtos and "x()" not in page.text
    assert extra == ["https://shiplog.io/about"]


def test_hn_comment_parsing():
    good = ('Acme | Backend Python Intern | REMOTE | https://acme.dev<p>We build agents. '
            '<a href="https://acme.dev/jobs">jobs</a> <a href="https://jobs.lever.co/acme">lever</a>')
    lead = parse_job_comment(good)
    assert lead and lead.url == "https://acme.dev/jobs" and lead.name_hint == "Acme"
    assert parse_job_comment("Acme | Sales | REMOTE <a href='https://acme.dev'>x</a>") is None  # no matching role
    assert parse_job_comment("Acme | Python | Onsite NYC <a href='https://acme.dev'>x</a>") is None  # not remote
    assert parse_job_comment("Acme | Python | Remote <a href='https://jobs.lever.co/a'>x</a>") is None  # only job board


# ---- gmail safety -----------------------------------------------------
def test_gmail_client_never_sends():
    tree = ast.parse(Path(gmail_client.__file__).read_text(encoding="utf-8"))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "send" not in attrs
    assert {"drafts", "create"} <= attrs


def test_raw_message_roundtrip():
    raw = gmail_client.build_raw_message("a@b.com", "Hi", "Body line\n")
    decoded = base64.urlsafe_b64decode(raw).decode()
    assert "To: a@b.com" in decoded and "Subject: Hi" in decoded and "Body line" in decoded


# ---- extractor providers ----------------------------------------------
GOOD = {
    "company_name": "Shiplog", "one_line_summary": "Changelogs.", "core_domain": "DevTools",
    "founder_first_name": " ", "founder_last_name": None, "personalized_hook": "I wondered about **diffs**.",
}


def _page():
    from src.intelligence.scraper import ScrapedPage
    return ScrapedPage(url="https://x.com", domain="x.com", text="hello")


def test_extractor_gemini_retries_bad_json_then_validates():
    import json
    from types import SimpleNamespace as NS
    from src.intelligence.extractor import Extractor

    replies = iter([NS(text="not json"), NS(text=json.dumps(GOOD))])

    class Models:
        @staticmethod
        def generate_content(**kw):
            assert kw["config"].response_mime_type == "application/json"
            return next(replies)

    client = NS(models=Models)
    ex = Extractor(Settings(llm_provider="gemini"), client=client)
    c = ex.extract(_page())
    assert c.founder_first_name is None and "*" not in c.personalized_hook


def test_extractor_anthropic_tool_call():
    from types import SimpleNamespace as NS
    from src.intelligence.extractor import Extractor

    client = NS(messages=NS(create=lambda **kw: NS(content=[NS(type="tool_use", input=GOOD)])))
    c = Extractor(Settings(llm_provider="anthropic"), client=client).extract(_page())
    assert c.core_domain == "DevTools"


def test_extractor_requires_key_for_chosen_provider():
    from src.intelligence.extractor import Extractor, ExtractionError
    with pytest.raises(ExtractionError, match="GEMINI_API_KEY"):
        Extractor(Settings(llm_provider="gemini", gemini_api_key=""))
    with pytest.raises(ExtractionError, match="ANTHROPIC_API_KEY"):
        Extractor(Settings(llm_provider="anthropic", anthropic_api_key=""))


def test_gmail_headless_never_opens_browser(tmp_path: Path):
    client = gmail_client.GmailDraftClient(tmp_path / "credentials.json", tmp_path / "token.json", headless=True)
    with pytest.raises(gmail_client.GmailError, match="HEADLESS"):
        client.ensure_authenticated()


def test_run_reads_past_known_leads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A growing database must not stall runs: known companies are skipped for free."""
    import src.pipeline as pipeline
    from src.sourcing.base import BaseSourcingProvider, Lead

    db = Database(tmp_path / "t.db")
    for i in range(30):
        db.mark_skipped(f"old{i}.com", f"https://old{i}.com", "hn", "seen before")

    class FakeHN(BaseSourcingProvider):
        name = "hn"

        def iter_leads(self):
            for i in range(30):
                yield Lead(url=f"https://old{i}.com", source="hn")
            yield Lead(url="https://fresh.io", source="hn")

    monkeypatch.setattr(pipeline, "build_providers", lambda settings, source: [FakeHN()])
    pipe = pipeline.Pipeline.__new__(pipeline.Pipeline)
    pipe.settings, pipe.db, pipe.dry_run = Settings(), db, True
    processed = []
    pipe.process = lambda url, src, hint=None, force=False: processed.append(url) or pipeline.Result(
        domain_of(url), "drafted", "ok")

    pipe.run(limit=1)
    assert processed == ["https://fresh.io"]
    assert FakeHN().fetch(3) == [Lead(url=f"https://old{i}.com", source="hn") for i in range(3)]


# ---- extra sources: YC directory, Launch HN, mixing ---------------------
class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def test_yc_selects_small_active_hiring_remote_first():
    from src.sourcing.yc_directory import select_companies

    base = {"status": "Active", "isHiring": True, "stage": "Early", "regions": [], "launched_at": 1}
    companies = [
        {**base, "name": "Old", "website": "https://old.dev", "team_size": 5, "launched_at": 100},
        {**base, "name": "New", "website": "https://new.dev", "team_size": 5, "launched_at": 900},
        {**base, "name": "Remote", "website": "https://remote.dev", "team_size": 5, "regions": ["Fully Remote"]},
        {**base, "name": "Big", "website": "https://big.dev", "team_size": 400},
        {**base, "name": "Gone", "website": "https://gone.dev", "team_size": 3, "status": "Inactive"},
        {**base, "name": "NoSite", "website": "", "team_size": 3},
        {**base, "name": "Unknown", "website": "https://unknown.dev", "team_size": None, "stage": "Growth"},
    ]
    picked = [c["name"] for c in select_companies(companies, max_team_size=25)]
    assert picked == ["Remote", "New", "Old"]


def test_yc_provider_yields_leads_and_tolerates_outages():
    import httpx
    from src.sourcing.yc_directory import YCDirectoryProvider

    data = [{"name": "Acme", "website": "https://acme.dev", "status": "Active", "isHiring": True,
             "team_size": 4, "batch": "Fall 2026", "one_liner": "Agents for X"}]
    client = type("C", (), {"get": lambda self, url: _FakeResponse(data)})()
    leads = YCDirectoryProvider(Settings(), client=client).fetch(5)
    assert [(l.url, l.source, l.name_hint) for l in leads] == [("https://acme.dev", "yc", "Acme")]
    assert "Fall 2026" in leads[0].context

    def boom(self, url):
        raise httpx.ConnectError("offline")
    down = type("C", (), {"get": boom})()
    assert YCDirectoryProvider(Settings(), client=down).fetch(5) == []


def test_launch_hn_uses_company_links_only():
    from src.sourcing.launch_hn import LaunchHNProvider, parse_launch

    good = {"title": "Launch HN: Alter (YC S25) – Access control for agents", "url": "https://alterauth.com/"}
    lead = parse_launch(good)
    assert lead.url == "https://alterauth.com/" and lead.name_hint == "Alter" and lead.source == "launchhn"
    assert parse_launch({"title": "Launch HN: Repo (YC W24) – thing", "url": "https://github.com/x/y"}) is None
    assert parse_launch({"title": "Launch HN: Text (YC F26) – no link", "url": None}) is None

    pages = {0: {"hits": [good, good], "nbPages": 1}}
    client = type("C", (), {"get": lambda self, url, params: _FakeResponse(pages[params["page"]])})()
    assert [l.url for l in LaunchHNProvider(Settings(), client=client).fetch(10)] == ["https://alterauth.com/"]


def test_run_mixes_sources_and_skips_repeats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import src.pipeline as pipeline
    from src.sourcing.base import BaseSourcingProvider, Lead

    def source(name, urls):
        class P(BaseSourcingProvider):
            def iter_leads(self):
                return (Lead(url=u, source=name) for u in urls)
        P.name = name
        return P()

    providers = [source("yc", ["https://a.dev", "https://b.dev"]), source("hn", ["https://a.dev", "https://c.dev"])]
    monkeypatch.setattr(pipeline, "build_providers", lambda settings, src: providers)
    pipe = pipeline.Pipeline.__new__(pipeline.Pipeline)
    pipe.settings, pipe.db, pipe.dry_run = Settings(), Database(tmp_path / "t.db"), True
    processed = []
    pipe.process = lambda url, src, hint=None, force=False: processed.append((src, url)) or pipeline.Result(
        domain_of(url), "skipped", "x")

    pipe.run(limit=5)
    # turns alternate yc, hn, yc, hn; the second a.dev (from hn) is skipped as a repeat
    assert processed == [("yc", "https://a.dev"), ("yc", "https://b.dev"), ("hn", "https://c.dev")]
