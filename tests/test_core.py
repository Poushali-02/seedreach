from __future__ import annotations

import ast
import base64
import dataclasses
import json
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
LONG = dataclasses.replace(PROFILE, email_style="long")


@pytest.mark.parametrize("domain,expected", [
    ("DevTools", ["ExampleScan"]),
    ("Fintech", ["ExamplePay"]),
    ("AI Agents", ["ExampleAgent"]),
    ("Other", ["ExampleCRM"]),
])
def test_generator_matches_domain(domain: str, expected: list[str]):
    for profile in (PROFILE, LONG):
        body = generate_email(_company(domain), profile).body
        for needle in expected + ["https://example.dev", "github.com/example", "Alex Example"]:
            assert needle in body
    assert "ExampleCo" in generate_email(_company(domain), LONG).body  # experience is long-only


def test_generator_is_plain_text_even_with_markdown_input():
    c = _company("Other", company_name="**Acme** #1", personalized_hook="I wondered about `x` and **y**.")
    draft = generate_email(c, PROFILE)
    assert not re.search(r"[*#`]", draft.body + draft.subject)


def test_generator_greeting_and_empty_optional_fields():
    for profile in (PROFILE, LONG):
        body = generate_email(_company("Other", founder_first_name=None), profile).body
        assert body.startswith("Hi Shiplog team,")
        assert "Phone" not in body and body.rstrip().endswith("Alex Example")  # phone is blank in the example


def test_short_email_is_short_with_one_project_and_one_ask():
    from src.outreach.generator import DEFAULT_ASK, _first_sentence

    body = generate_email(_company("DevTools"), PROFILE).body
    assert len(body.split()) <= 130 and body.count("\n\n") == 5  # 6 sections
    assert DEFAULT_ASK.replace("{company}", "Shiplog") in body and "linkedin.com" not in body  # two links only
    custom = dataclasses.replace(PROFILE, ask="Could I help {company} as an intern this summer?")
    assert "Could I help Shiplog as an intern this summer?" in generate_email(_company("DevTools"), custom).body
    assert _first_sentence("I built X on OSV.dev feeds. It also does Y.") == "I built X on OSV.dev feeds."
    assert _first_sentence("No full stop here") == "No full stop here"
    assert len(generate_email(_company("DevTools"), LONG).body.split()) > len(body.split())


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
    good = json.loads((ROOT / "profile.example.json").read_text(encoding="utf-8"))
    bad.write_text(json.dumps({**good, "email_style": "medium"}), encoding="utf-8")
    with pytest.raises(ProfileError, match="email_style"):
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
    r = f.find("x.com", "X", "Zoë", None, [])
    assert r.email == "zoe@x.com" and r.is_guess
    monkeypatch.setattr(email_finder, "has_mx", lambda d: False)
    assert f.find("x.com", "X", "Ada", None, []) is None


def test_email_finder_order_and_site_pattern(monkeypatch: pytest.MonkeyPatch):
    f = _finder()
    monkeypatch.setattr(email_finder, "has_mx", lambda d: True)
    # the founder's own address on the site beats everything else
    r = f.find("x.com", "X", "Ada", "Lovelace", ["maria.lopez@x.com", "ada@x.com", "hello@x.com"])
    assert (r.email, r.confidence) == ("ada@x.com", "on site")
    # someone else's first.last address shows the format: the founder's address follows it
    r = f.find("x.com", "X", "Ada", "Lovelace", ["maria.lopez@x.com", "hello@x.com"])
    assert (r.email, r.confidence, r.is_guess) == ("ada.lovelace@x.com", "pattern from site", False)
    r = f.find("x.com", "X", "Ada", None, ["maria@x.com"])
    assert (r.email, r.confidence) == ("ada@x.com", "pattern from site")
    # first.last needs a last name; without one, fall back to what the site prints
    assert f.find("x.com", "X", "Ada", None, ["maria.lopez@x.com"]).email == "maria.lopez@x.com"
    assert f.find("x.com", "X", "Ada", None, ["hello@x.com"]).confidence == "generic"
    assert f.find("x.com", "X", "Ada", None, ["jobs@x.com"]).is_guess


# ---- scraper / sourcing parsing ---------------------------------------
def test_scraper_extracts_text_meta_mailto_and_about_links():
    html = """<html><head><title>Shiplog</title><meta name="description" content="Changelogs"></head>
    <body><script>x()</script><h1>Ship notes</h1><a href="mailto:ada@shiplog.io?subject=hi">mail</a>
    <a href="/about">About</a><p>Built by Ada Lovelace.</p></body></html>"""
    page, extra = _parse(html, "https://shiplog.io")
    assert page.title == "Shiplog" and page.meta["description"] == "Changelogs"
    assert "ada@shiplog.io" in page.mailtos and "x()" not in page.text
    assert extra == ["https://shiplog.io/about"]



def test_scraper_ranks_team_pages_first_and_skips_noise():
    html = """<a href="/contact">Contact</a><a href="/about/careers">Careers</a><a href="/blog/2025/launch">Post</a>
    <a href="/about">About</a><a href="/vision">Vision</a><a href="/our-team">Our Team</a>
    <a href="/ueber-uns">Über uns</a><a href="/authors/ada">Ada, Co-founder</a><a href="https://other.dev/team">x</a>
    <a href="/use-cases">How teams use Shiplog</a>"""
    _, extra = _parse(html, "https://shiplog.io")
    assert extra == [
        "https://shiplog.io/our-team", "https://shiplog.io/ueber-uns", "https://shiplog.io/authors/ada",
        "https://shiplog.io/about", "https://shiplog.io/vision", "https://shiplog.io/contact",
    ]


def test_scraper_keeps_extra_pages_when_the_first_page_is_long():
    from src.intelligence import scraper as sc

    pages = {
        "https://shiplog.io": f"<p>{'feature ' * 4000}</p><a href='/team'>Team</a>",
        "https://shiplog.io/team": "<p>Ada Lovelace, Co-founder and CEO.</p>",
    }

    class Client:
        def get(self, url):
            from types import SimpleNamespace as NS
            return NS(text=pages[url.rstrip("/")], url=url, headers={"content-type": "text/html"},
                      raise_for_status=lambda: None)

    page = sc.Scraper(Settings(), client=Client()).fetch("https://shiplog.io")
    assert "Ada Lovelace, Co-founder" in page.text and len(page.text) <= sc.MAX_TEXT_CHARS
    assert page.truncated and page.pages == ["https://shiplog.io", "https://shiplog.io/team"]


def test_scraper_excerpt_keeps_the_founder_section_of_a_long_page():
    from src.intelligence.scraper import _excerpt

    text = "product " * 1000 + "Ada Lovelace, Co-founder"
    assert "Co-founder" in _excerpt(text, 3000) and len(_excerpt(text, 3000)) <= 3000
    assert _excerpt("Founder Ada. " + "x" * 5000, 3000).startswith("Founder Ada")

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


def test_yc_regions_match_whole_names_case_insensitively():
    from src.sourcing.yc_directory import parse_regions, select_companies

    assert parse_regions(" India , Remote,,") == {"india", "remote"}
    assert parse_regions("") == set()

    base = {"status": "Active", "isHiring": True, "team_size": 5, "launched_at": 1}
    companies = [
        {**base, "name": "Bengaluru", "website": "https://b.dev", "regions": ["India", "South Asia"]},
        {**base, "name": "Anywhere", "website": "https://a.dev", "regions": ["Remote", "Partly Remote"]},
        {**base, "name": "Hybrid", "website": "https://h.dev", "regions": ["Partly Remote"]},
        {**base, "name": "SF", "website": "https://s.dev", "regions": ["America / Canada"]},
        {**base, "name": "NoRegion", "website": "https://n.dev"},
    ]

    def names(regions):
        return sorted(c["name"] for c in select_companies(companies, 25, regions))

    assert names(parse_regions("India,Remote")) == ["Anywhere", "Bengaluru"]  # "Partly Remote" isn't "Remote"
    assert names(parse_regions("partly remote")) == ["Anywhere", "Hybrid"]
    assert names(set()) == names(None) == ["Anywhere", "Bengaluru", "Hybrid", "NoRegion", "SF"]


def test_yc_provider_filters_by_region_and_warns_on_unknown_names(caplog):
    from src.sourcing.yc_directory import YCDirectoryProvider

    base = {"status": "Active", "isHiring": True, "team_size": 4}
    data = [
        {**base, "name": "Desi", "website": "https://desi.dev", "regions": ["India", "South Asia"]},
        {**base, "name": "Yank", "website": "https://yank.dev", "regions": ["United States of America"]},
    ]
    client = type("C", (), {"get": lambda self, url: _FakeResponse(data)})()
    with caplog.at_level("WARNING"):
        leads = YCDirectoryProvider(Settings(yc_regions="India, Indai"), client=client).fetch(5)
    assert [l.name_hint for l in leads] == ["Desi"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == ["YC_REGIONS: no hiring YC company lists indai; check the spelling"]


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



def test_region_preferences_parse_and_rank():
    from src.sourcing.base import parse_preferences, region_rank

    prefer = parse_preferences(" India , Remote,,india")
    assert prefer == ("india", "remote")
    assert region_rank(["South Asia", "India"], prefer) == 0
    assert region_rank(["Remote", "India"], prefer) == 0  # the best match counts
    assert region_rank(["America / Canada", "Remote"], prefer) == 1
    assert region_rank(["Partly Remote"], prefer) == 2  # whole names only
    assert region_rank([], prefer) == 2
    assert region_rank(["India"], ()) == 0


def test_yc_prefer_regions_ranks_without_dropping():
    from src.sourcing.yc_directory import YCDirectoryProvider

    base = {"status": "Active", "isHiring": True, "team_size": 5}
    data = [
        {**base, "name": "SF", "website": "https://s.dev", "regions": ["America / Canada"], "launched_at": 900},
        {**base, "name": "Anywhere", "website": "https://a.dev", "regions": ["Remote"], "launched_at": 500},
        {**base, "name": "Bengaluru", "website": "https://b.dev", "regions": ["India"], "launched_at": 100},
        {**base, "name": "Delhi", "website": "https://d.dev", "regions": ["India", "Remote"], "launched_at": 200},
    ]
    client = type("C", (), {"get": lambda self, url: _FakeResponse(data)})()
    leads = YCDirectoryProvider(Settings(prefer_regions="India,Remote"), client=client).fetch(10)
    # India first (newest first within it), then Remote, then the rest; nothing dropped
    assert [l.name_hint for l in leads] == ["Delhi", "Bengaluru", "Anywhere", "SF"]
    assert leads[0].regions == ("India", "Remote")
    # without a preference, the old order: remote-friendly first, then newest
    plain = YCDirectoryProvider(Settings(), client=client).fetch(10)
    assert [l.name_hint for l in plain] == ["Anywhere", "Delhi", "SF", "Bengaluru"]


def test_ranked_interleave_takes_preferred_leads_first_and_stays_lazy():
    from src.pipeline import ranked_interleave
    from src.sourcing.base import Lead, parse_preferences, region_rank

    def stream(name, regions_list):
        for i, regions in enumerate(regions_list):
            read.append(f"{name}{i}")
            yield Lead(url=f"https://{name}{i}.dev", source=name, regions=regions)

    read: list[str] = []
    prefer = parse_preferences("India,Remote")
    streams = [
        stream("yc", [("India",), ("India",), ("Remote",), ()]),
        stream("hn", [("Remote",), ("Remote",)]),
        stream("ph", [(), ()]),
    ]
    leads = ranked_interleave(streams, lambda lead: region_rank(lead.regions, prefer))
    first = next(leads)
    assert first.url == "https://yc0.dev"
    assert read == ["yc0", "hn0", "ph0"]  # one lead read ahead per source, no more
    order = [first.url] + [l.url for l in leads]
    assert [u.split("//")[1].split(".")[0] for u in order] == [
        "yc0", "yc1",           # India
        "hn0", "yc2", "hn1",    # Remote: sources take turns (yc just had two)
        "ph0", "yc3", "ph1",    # the rest
    ]


def test_run_ranks_sources_only_when_prefer_regions_is_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import src.pipeline as pipeline
    from src.sourcing.base import BaseSourcingProvider, Lead

    def source(name, leads):
        class P(BaseSourcingProvider):
            def iter_leads(self):
                return (Lead(url=u, source=name, regions=r) for u, r in leads)
        P.name = name
        return P()

    def run(settings):
        providers = [source("hn", [("https://h.dev", ("Remote",))]),
                     source("yc", [("https://i.dev", ("India",)), ("https://u.dev", ())])]
        monkeypatch.setattr(pipeline, "build_providers", lambda settings, src: providers)
        pipe = pipeline.Pipeline.__new__(pipeline.Pipeline)
        pipe.settings, pipe.db, pipe.dry_run = settings, Database(tmp_path / f"{id(settings)}.db"), True
        processed = []
        pipe.process = lambda url, src, hint=None, force=False: processed.append(url) or pipeline.Result(
            domain_of(url), "skipped", "x")
        pipe.run(limit=5)
        return processed

    assert run(Settings()) == ["https://h.dev", "https://i.dev", "https://u.dev"]
    assert run(Settings(prefer_regions="India,Remote")) == ["https://i.dev", "https://h.dev", "https://u.dev"]


# ---- CLI ---------------------------------------------------------------
def test_cli_starts_and_lists_commands():
    # Imported here so a broken main.py (e.g. a SyntaxError) fails this test by name.
    from typer.testing import CliRunner

    import main

    result = CliRunner().invoke(main.app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("run", "target", "add-target", "status"):
        assert command in result.output


# ---- evals ---------------------------------------------------------------
def test_eval_snapshot_saves_pages_once_and_keeps_corrections(tmp_path: Path):
    from evals import snapshot as snap
    from src.intelligence.scraper import ScrapedPage

    class FakeScraper:
        calls = 0

        def fetch(self, url):
            FakeScraper.calls += 1
            return ScrapedPage(url=url, domain=domain_of(url), text="Shiplog by Ada.", mailtos=["ada@useshiplog.com"])

    class FakeExtractor:
        def extract(self, page, hint):
            return _company("DevTools")

    companies = [{"url": "https://useshiplog.com", "source": "hacker_news", "case": "normal", "name_hint": "Shiplog"}]
    rows = snap.run(companies, FakeScraper(), FakeExtractor(), tmp_path, echo=None)
    assert rows[0]["company_name"] == "Shiplog" and rows[0]["founder_first_name"] == "Ada"
    assert rows[0]["emails_on_site"] == "ada@useshiplog.com" and rows[0]["checked"] == ""

    # a human corrects the row; a second run reuses the saved page and keeps the correction
    rows[0].update(core_domain="B2B SaaS", checked="yes")
    snap.write_csv(tmp_path / "labels.csv", rows)
    again = snap.run(companies, FakeScraper(), FakeExtractor(), tmp_path, echo=None)
    assert FakeScraper.calls == 1
    assert again[0]["core_domain"] == "B2B SaaS" and again[0]["checked"] == "yes"


def test_eval_snapshot_records_failures_and_retries_them(tmp_path: Path):
    from evals import snapshot as snap
    from src.intelligence.extractor import ExtractionError
    from src.intelligence.scraper import ScrapedPage

    class Scraper:
        def fetch(self, url):
            return ScrapedPage(url=url, domain=domain_of(url), text="Shiplog.")

    class FlakyExtractor:
        def __init__(self, fail):
            self.fail = fail

        def extract(self, page, hint):
            if self.fail:
                raise ExtractionError("Gemini API error: 503")
            return _company("DevTools")

    companies = [{"url": "https://useshiplog.com"}]
    first = snap.run(companies, Scraper(), FlakyExtractor(fail=True), tmp_path, echo=None)
    assert first[0]["company_name"] == "" and "503" in first[0]["notes"]
    second = snap.run(companies, Scraper(), FlakyExtractor(fail=False), tmp_path, echo=None)
    assert second[0]["company_name"] == "Shiplog" and second[0]["notes"] == ""


def test_eval_scoring_rules():
    from evals.run_evals import domain_ok, founder_outcome, hook_rule_problems, norm_name

    assert norm_name("Revieve Oy") == norm_name("revieve") and norm_name("Toolhouse AI") != norm_name("Toolhouse")
    assert domain_ok("DevTools|AI Agents", "AI Agents") and not domain_ok("Other", "B2B SaaS")
    assert founder_outcome(("", ""), (None, None)) == "correct"
    assert founder_outcome(("", ""), ("Quinn", "Slack")) == "invented"
    assert founder_outcome(("Derek", "Gregg"), ("Derek", None)) == "partial"
    assert founder_outcome(("René", "Fergen"), (None, None)) == "missed"
    assert founder_outcome(("Jacob", "Shen"), ("Kenny", "Stone")) == "wrong_person"
    assert founder_outcome(("Jacob", "Shen"), ("Kenny", "Stone"), "Kenny Stone") == "correct"
    assert founder_outcome(("Naftali", ""), ("Max", None), "Sam Bobb; Max") == "correct"
    assert hook_rule_problems("I noticed X. I was curious how Y.") == ["more than one sentence"]
    assert hook_rule_problems("I was curious how Shiplog v2.1 diffs notes.") == []


def test_eval_score_saved_counts_wrong_websites_as_unsendable(tmp_path: Path):
    import json

    from evals.run_evals import score_saved, summarize

    pred = _company("DevTools").model_dump()
    labels = [
        {"domain": "useshiplog.com", "right_website": "yes", "company_name": "Shiplog", "core_domain": "DevTools",
         "founder_first_name": "Ada", "founder_last_name": "", "hook_ok": "yes"},
        {"domain": "jobs.example", "right_website": "no", "company_name": "Shiplog", "core_domain": "DevTools",
         "founder_first_name": "", "founder_last_name": "", "hook_ok": "yes"},
    ]
    for label in labels:
        (tmp_path / f"{label['domain']}.json").write_text(json.dumps({"prediction": pred}), encoding="utf-8")
    summary = summarize(score_saved(labels, tmp_path))
    assert summary["company_name"] == 100.0 and summary["founder_correct"] == 100.0
    assert summary["wrong_website_rows"] == 1 and summary["sendable"] == 50.0


# ---- run traces -----------------------------------------------------------
def test_extractor_counts_tokens_across_retries():
    import json
    from types import SimpleNamespace as NS
    from src.intelligence.extractor import Extractor

    usage = NS(prompt_token_count=1000, candidates_token_count=50, thoughts_token_count=None)
    replies = iter([NS(text="not json", usage_metadata=usage), NS(text=json.dumps(GOOD), usage_metadata=usage)])
    client = NS(models=NS(generate_content=lambda **kw: next(replies)))
    ex = Extractor(Settings(llm_provider="gemini"), client=client)
    ex.extract(_page())
    assert (ex.usage.calls, ex.usage.input_tokens, ex.usage.output_tokens) == (2, 2000, 100)


def _traced_pipeline(tmp_path: Path, founder_text: str):
    from types import SimpleNamespace as NS
    import src.pipeline as pipeline
    from src.enrichment.email_finder import EmailResult
    from src.intelligence.extractor import Usage
    from src.intelligence.scraper import ScrapedPage

    class FakeExtractor:
        model, usage = "fake-model", Usage()

        def extract(self, page, hint):
            self.usage = Usage(calls=1, input_tokens=900, output_tokens=40)
            return _company("DevTools")

    pipe = pipeline.Pipeline.__new__(pipeline.Pipeline)
    pipe.settings, pipe.db, pipe.dry_run = Settings(), Database(tmp_path / "t.db"), False
    pipe.run_id, pipe.traces, pipe.profile = "run-1", [], PROFILE
    pipe.scraper = NS(fetch=lambda url: ScrapedPage(
        url=url, domain="useshiplog.com", text=founder_text, pages=[url, url + "/about"], truncated=True))
    pipe.extractor = FakeExtractor()
    pipe.finder = NS(find=lambda *a: EmailResult("ada@useshiplog.com", "heuristic", "pattern from site"))
    pipe.gmail = NS(create_draft=lambda *a: "draft-1")
    pipe.company_facts = NS(enabled=False, lookup=lambda domain: None)  # no Apollo key
    return pipe


def test_pipeline_records_a_trace_per_company(tmp_path: Path):
    pipe = _traced_pipeline(tmp_path, "Shiplog was started by Ada.")  # first name only on the page
    assert pipe.process("https://useshiplog.com", "hn").outcome == "drafted"
    assert pipe.process("https://useshiplog.com", "hn").outcome == "duplicate"  # no second trace

    (t,) = pipe.traces
    assert (t.domain, t.outcome, t.pages, t.truncated, t.llm_calls, t.input_tokens) == (
        "useshiplog.com", "drafted", 2, True, 1, 900)
    assert t.founder_in_text is True and t.email_source == "heuristic (pattern from site)"
    assert set(t.ms) == {"scrape", "extract", "email", "draft"}

    (run,) = pipe.db.recent_runs()
    assert (run["companies"], run["drafted"], run["input_tokens"], run["truncated"]) == (1, 1, 900, 1)


def test_trace_flags_a_founder_the_page_never_names(tmp_path: Path):
    from src.trace import name_in_text

    pipe = _traced_pipeline(tmp_path, "Changelogs for monorepos.")
    pipe.process("https://useshiplog.com", "hn")
    assert pipe.traces[0].founder_in_text is False
    assert pipe.db.recent_runs()[0]["founder_not_in_text"] == 1
    assert name_in_text("Ada", "Lovelace", "by ada lovelace,") and name_in_text(None, None, "x") is None
    assert not name_in_text("Ada", None, "Adam built it")


def test_dry_run_traces_stay_in_memory(tmp_path: Path):
    pipe = _traced_pipeline(tmp_path, "Ada")
    pipe.dry_run = True
    pipe.process("https://useshiplog.com", "hn")
    assert len(pipe.traces) == 1 and pipe.db.recent_runs() == []


# ---- reaching real people (v0.3.1) ------------------------------------------
def test_display_name_drops_legal_endings():
    from src.utils import display_name

    assert display_name("Vernius Systems, Inc.") == "Vernius Systems"
    assert display_name("Revieve Oy") == "Revieve"
    assert display_name("Acme Pvt. Ltd.") == "Acme"
    assert display_name("Coverage Cat") == "Coverage Cat" and display_name("Inc") == "Inc"
    assert "Vernius Systems team" in generate_email(_company("Other", company_name="Vernius Systems, Inc.",
                                                            founder_first_name=None), PROFILE).body


def test_guessed_address_is_not_drafted_unless_allowed(tmp_path: Path):
    from types import SimpleNamespace as NS
    from src.enrichment.email_finder import EmailResult

    pipe = _traced_pipeline(tmp_path, "Shiplog was started by Ada.")
    pipe.finder = NS(find=lambda *a: EmailResult("ada@useshiplog.com", "heuristic", "guess"))
    r = pipe.process("https://useshiplog.com", "hn")
    assert r.outcome == "skipped" and r.detail.startswith("needs contact: guessed ada@useshiplog.com")
    (row,) = pipe.db.needs_contact()
    assert row["domain"] == "useshiplog.com" and row["founder_name"] == "Ada"

    (tmp_path / "allowed").mkdir()
    pipe = _traced_pipeline(tmp_path / "allowed", "Shiplog was started by Ada.")
    pipe.settings = Settings(allow_guessed_emails=True)
    pipe.finder = NS(find=lambda *a: EmailResult("ada@useshiplog.com", "heuristic", "guess"))
    assert pipe.process("https://useshiplog.com", "hn").outcome == "drafted"


def test_founder_not_on_the_page_is_not_used(tmp_path: Path):
    seen = {}
    pipe = _traced_pipeline(tmp_path, "Changelogs for monorepos.")  # the page never names Ada
    real_find = pipe.finder.find
    pipe.finder.find = lambda domain, company, first, last, mailtos: seen.update(first=first) or real_find()
    r = pipe.process("https://useshiplog.com", "hn")
    assert seen["first"] is None and r.draft.body.startswith("Hi Shiplog team,")


def test_non_company_pages_are_skipped(tmp_path: Path):
    from src.utils import non_company_kind

    assert non_company_kind("axmed.teamtailor.com") == "job_board" and non_company_kind("jobs.acme.com") == "job_board"
    assert non_company_kind("techcrunch.com") == "news" and non_company_kind("github.com") == "other"
    assert non_company_kind("useshiplog.com") is None and non_company_kind("teamtailor.com.evil.dev") is None
    assert parse_job_comment("Acme | Python | Remote <a href='https://techcrunch.com/acme-raises'>x</a>") is None

    pipe = _traced_pipeline(tmp_path, "Ada")
    fetched = []
    pipe.scraper.fetch = lambda url: fetched.append(url)
    r = pipe.process("https://axmed.teamtailor.com/jobs/1-ai-engineer", "hn")
    assert r.outcome == "skipped" and "job-board page" in r.detail and fetched == []  # no scrape, no LLM


def test_model_flagged_article_is_skipped(tmp_path: Path):
    pipe = _traced_pipeline(tmp_path, "Ada")
    real = pipe.extractor.extract
    pipe.extractor.extract = lambda page, hint: real(page, hint).model_copy(update={"page_type": "news"})
    r = pipe.process("https://some-blog.dev/acme-raises", "hn")
    assert r.outcome == "skipped" and "a news article about Shiplog" in r.detail


def test_eval_scores_wrong_website_catches():
    from evals.run_evals import score_row, summarize

    label = {"domain": "jobs.example", "right_website": "no", "company_name": "Shiplog", "core_domain": "DevTools",
             "founder_first_name": "", "founder_last_name": ""}
    caught = score_row(label, {**_company("DevTools").model_dump(), "page_type": "job_board"}, None)
    missed = score_row(label, _company("DevTools").model_dump(), None)
    assert summarize([caught, missed])["wrong_website_caught"] == 50.0


# ---- job boards in the draft (#13) -----------------------------------------
def test_draft_names_the_role_before_the_ask_and_stays_short():
    from src.outreach.generator import DEFAULT_ASK

    ask = DEFAULT_ASK.replace("{company}", "Shiplog")
    body = generate_email(_company("DevTools"), PROFILE, role="Software Engineering Intern, Backend").body
    assert f"I saw your Software Engineering Intern, Backend opening. {ask}" in body
    assert len(body.split()) <= 140
    assert "opening" not in generate_email(_company("DevTools"), PROFILE).body  # no role, no mention
    long = generate_email(_company("DevTools"), LONG, role="AI Intern").body
    assert "I saw your AI Intern opening. I'm " in long
    messy = generate_email(_company("DevTools"), PROFILE, role="  **ML**   Intern\n(Remote) ").body
    assert "I saw your ML Intern (Remote) opening." in messy  # still plain text


def test_pipeline_names_an_open_role_from_the_job_board(tmp_path: Path):
    from types import SimpleNamespace as NS
    from src.enrichment.job_boards import Board, BoardCheck, Role
    from src.intelligence.scraper import ScrapedPage

    pipe = _traced_pipeline(tmp_path, "Shiplog was started by Ada.")
    pipe.scraper = NS(fetch=lambda url: ScrapedPage(
        url=url, domain="useshiplog.com", text="Shiplog was started by Ada.", pages=[url],
        job_boards=[Board("ashby", "shiplog")]))
    seen = []

    def check(boards):
        seen.append(boards)
        return BoardCheck(roles=[Role("Finance Intern", "u1", "ashby:shiplog"),
                                 Role("Backend Engineering Intern", "u2", "ashby:shiplog")])
    pipe.boards = NS(check=check)

    result = pipe.process("https://useshiplog.com", "hn")
    assert result.outcome == "drafted" and result.detail.endswith("; role: Backend Engineering Intern")
    assert "I saw your Backend Engineering Intern opening." in result.draft.body
    assert seen == [[Board("ashby", "shiplog")]] and "jobs" in pipe.traces[0].ms

    (tmp_path / "off").mkdir()
    off = _traced_pipeline(tmp_path / "off", "Shiplog was started by Ada.")
    off.scraper, off.boards = pipe.scraper, pipe.boards
    off.settings = Settings(check_job_boards=False)  # switched off: the board isn't read
    assert "opening" not in off.process("https://useshiplog.com", "hn").draft.body and len(seen) == 1


# ---- generic inboxes -------------------------------------------------------
def _inbox_pipeline(tmp_path: Path, name: str, page_text: str, address: str, mode: str = "no_founder"):
    from types import SimpleNamespace as NS
    from src.enrichment.email_finder import EmailResult

    (tmp_path / name).mkdir()
    pipe = _traced_pipeline(tmp_path / name, page_text)
    pipe.settings = Settings(generic_inbox=mode)
    pipe.finder = NS(find=lambda *a: EmailResult(address, "mailto", "generic"))
    return pipe


def test_generic_inbox_with_a_named_founder_goes_to_needs_contact(tmp_path: Path):
    named = "Shiplog was started by Ada."  # the founder is on the page, so the greeting would be "Hi Ada,"
    pipe = _inbox_pipeline(tmp_path, "named", named, "hello@useshiplog.com")
    r = pipe.process("https://useshiplog.com", "hn")
    assert r.outcome == "skipped" and r.detail == (
        "needs contact: only a generic inbox (hello@useshiplog.com); reach Ada directly")
    (row,) = pipe.db.needs_contact()
    assert (row["domain"], row["founder_name"]) == ("useshiplog.com", "Ada")

    # founders@ reaches the founders, so it's still drafted
    assert _inbox_pipeline(tmp_path, "f", named, "founders@useshiplog.com").process(
        "https://useshiplog.com", "hn").outcome == "drafted"
    # "always" restores the old behaviour
    assert _inbox_pipeline(tmp_path, "a", named, "hello@useshiplog.com", "always").process(
        "https://useshiplog.com", "hn").outcome == "drafted"


def test_generic_inbox_without_a_named_founder(tmp_path: Path):
    unnamed = "Changelogs for monorepos."  # no founder on the page: "Hi Shiplog team,"
    drafted = _inbox_pipeline(tmp_path, "d", unnamed, "hello@useshiplog.com").process("https://useshiplog.com", "hn")
    assert drafted.outcome == "drafted" and drafted.draft.body.startswith("Hi Shiplog team,")
    never = _inbox_pipeline(tmp_path, "n", unnamed, "hello@useshiplog.com", "never")
    r = never.process("https://useshiplog.com", "hn")
    assert r.outcome == "skipped" and r.detail == "needs contact: only a generic inbox (hello@useshiplog.com)"


def test_linkedin_people_search_link():
    from src.utils import linkedin_people_search

    assert linkedin_people_search("Ada Lovelace", "Shiplog, Inc.") == (
        "https://www.linkedin.com/search/results/people/?keywords=Ada+Lovelace+Shiplog")
    assert linkedin_people_search("Ada") == "https://www.linkedin.com/search/results/people/?keywords=Ada"


# ---- company size from Apollo ---------------------------------------------
def _apollo_client(handler):
    import httpx
    from src.enrichment.company_facts import CompanyFactsClient

    return CompanyFactsClient("test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_company_facts_reads_apollo_and_turns_off_when_refused(caplog):
    import httpx

    # trimmed from real Apollo responses (2026-11-13): a 7-person seed startup and a large company
    orgs = {
        "context.dev": {"organization": {"estimated_num_employees": 7, "latest_funding_stage": "Seed",
                                         "total_funding_printed": "9.1M", "founded_year": 2025}},
        "stripe.com": {"organization": {"estimated_num_employees": 9400,
                                        "latest_funding_stage": "Venture (Round not Specified)"}},
        "lim.run": {"organization": {}},  # Apollo knows nothing
    }
    client = _apollo_client(lambda r: httpx.Response(200, json=orgs[r.url.params["domain"]]))
    seed = client.lookup("context.dev")
    assert (seed.employees, seed.funding_stage, seed.summary()) == (7, "Seed", "Seed, ~7 people")
    assert client.lookup("stripe.com").employees == 9400
    assert client.lookup("lim.run") is None

    calls = []

    def refuse(request):
        calls.append(request)
        return httpx.Response(403, json={"error": "not included in your Free plan", "error_code": "API_INACCESSIBLE"})
    blocked = _apollo_client(refuse)
    with caplog.at_level("WARNING"):
        assert blocked.lookup("a.dev") is None and blocked.lookup("b.dev") is None
    assert len(calls) == 1 and not blocked.enabled  # asked once, then stopped for the run
    assert sum("Apollo company lookups are off" in m for m in caplog.messages) == 1


def test_pipeline_skips_companies_apollo_says_are_too_big(tmp_path: Path):
    from types import SimpleNamespace as NS
    from src.enrichment.company_facts import CompanyFacts

    pipe = _traced_pipeline(tmp_path, "Shiplog was started by Ada.")
    sizes = {"useshiplog.com": CompanyFacts(employees=7, funding_stage="Seed"),
             "stripe.com": CompanyFacts(employees=9400)}
    pipe.company_facts = NS(enabled=True, lookup=lambda domain: sizes.get(domain))
    scraped = []
    real_fetch = pipe.scraper.fetch
    pipe.scraper = NS(fetch=lambda url: scraped.append(url) or real_fetch(url))

    big = pipe.process("https://stripe.com", "hn")
    assert big.outcome == "skipped" and big.detail == "too big: ~9400 employees (Apollo), over MAX_EMPLOYEES=100"
    assert scraped == []  # skipped before scraping: no page fetched, no LLM call

    small = pipe.process("https://useshiplog.com", "hn")
    assert small.outcome == "drafted" and small.detail.endswith("; Seed, ~7 people")
    assert "size" in pipe.traces[-1].ms

    (tmp_path / "off").mkdir()
    off = _traced_pipeline(tmp_path / "off", "Shiplog was started by Ada.")
    off.company_facts = NS(enabled=True, lookup=lambda domain: sizes.get(domain))
    off.settings = Settings(max_employees=0)  # check turned off: even a big company goes ahead
    assert off.process("https://stripe.com", "hn").outcome != "skipped"


def test_apollo_email_lookup_stops_after_the_plan_refuses_it(caplog, monkeypatch: pytest.MonkeyPatch):
    import httpx

    calls = []

    def refuse(request):
        calls.append(request)
        return httpx.Response(403, json={"error": "The api/v1/people/match API is not included in your Free plan"})
    monkeypatch.setattr(email_finder, "has_mx", lambda d: True)
    finder = EmailFinder(Settings(apollo_api_key="test-key"), client=httpx.Client(transport=httpx.MockTransport(refuse)))
    with caplog.at_level("WARNING"):
        first = finder.find("x.com", "X", "Ada", "Lovelace", ["ada@x.com"])
        finder.find("y.com", "Y", "Bob", None, ["bob@y.com"])
    assert first.email == "ada@x.com" and first.confidence == "on site"  # falls back to the site
    assert len(calls) == 1 and not finder.apollo_enabled
    assert sum("Apollo email lookups are off" in m for m in caplog.messages) == 1


# ---- run digest (phone summary) --------------------------------------------
def test_digest_lists_roles_founders_and_drafts_without_email_addresses():
    from src.digest import build_digest
    from src.pipeline import Result

    results = [
        Result("ramp.com", "drafted", "mailto: on site; role: SWE Intern", to="ada@ramp.com", company="Ramp",
               role="Software Engineering Intern, Backend", apply_url="https://jobs.ashbyhq.com/ramp/1",
               size="Seed, ~7 people"),
        Result("acme.dev", "skipped", "needs contact: only a generic inbox (hello@acme.dev); reach Ada directly",
               company="Acme", founder="Ada Lovelace", role="AI Intern", apply_url="https://jobs.lever.co/acme/2"),
        Result("news.com", "skipped", "not the company's own site"),
        Result("down.dev", "error", "extraction failed"),
    ]
    text = build_digest(results)
    assert "### Apply" in text and "- **Ramp** (ramp.com): [Software Engineering Intern, Backend](" \
        "https://jobs.ashbyhq.com/ramp/1), Seed, ~7 people" in text
    assert "- **Acme** (acme.dev): [AI Intern](https://jobs.lever.co/acme/2)" in text  # skipped, but can apply
    assert "### Reach the founder on LinkedIn" in text and "Ada Lovelace, [find on LinkedIn](" \
        "https://www.linkedin.com/search/results/people/?keywords=Ada+Lovelace+Acme)" in text
    assert "### Drafts to review in Gmail" in text and "- **Ramp** (ramp.com): Software Engineering Intern" in text
    assert "@" not in text and "news.com" not in text and "down.dev" not in text
    assert build_digest([Result("news.com", "skipped", "not the company's own site")]) == ""
    assert build_digest(results[:1], dry_run=True).startswith("_Dry run")


def test_skipped_companies_keep_their_open_role_for_the_digest(tmp_path: Path):
    from types import SimpleNamespace as NS
    from src.enrichment.email_finder import EmailResult
    from src.enrichment.job_boards import Board, BoardCheck, Role
    from src.intelligence.scraper import ScrapedPage

    pipe = _traced_pipeline(tmp_path, "Shiplog was started by Ada.")
    pipe.scraper = NS(fetch=lambda url: ScrapedPage(url=url, domain="useshiplog.com",
                                                    text="Shiplog was started by Ada.", pages=[url],
                                                    job_boards=[Board("ashby", "shiplog")]))
    pipe.boards = NS(check=lambda boards: BoardCheck(roles=[Role("Backend Intern", "https://jobs.ashbyhq.com/s/1", "x")]))
    pipe.finder = NS(find=lambda *a: EmailResult("hello@useshiplog.com", "mailto", "generic"))  # generic: skipped
    r = pipe.process("https://useshiplog.com", "hn")
    assert r.outcome == "skipped" and r.detail.startswith("needs contact")
    assert (r.company, r.founder, r.role, r.apply_url) == (
        "Shiplog", "Ada", "Backend Intern", "https://jobs.ashbyhq.com/s/1")



def test_old_openings_are_not_named_and_the_digest_shows_the_posting_date(tmp_path: Path):
    from datetime import date, timedelta
    from types import SimpleNamespace as NS
    from src.digest import build_digest
    from src.enrichment.email_finder import EmailResult
    from src.enrichment.job_boards import Board, BoardCheck, Role
    from src.intelligence.scraper import ScrapedPage
    from src.pipeline import Result

    def process(posted):
        pipe = _traced_pipeline(tmp_path, "Shiplog was started by Ada.")
        pipe.scraper = NS(fetch=lambda url: ScrapedPage(url=url, domain="useshiplog.com",
                                                        text="Shiplog was started by Ada.", pages=[url],
                                                        job_boards=[Board("ashby", "shiplog")]))
        pipe.boards = NS(check=lambda boards: BoardCheck(roles=[Role("Backend Intern", "https://j/1", "x", posted)]))
        pipe.finder = NS(find=lambda *a: EmailResult("hello@useshiplog.com", "mailto", "generic"))
        return pipe.process("https://useshiplog.com", "hn")

    today = date.today()
    fresh = process(today - timedelta(days=3))
    assert (fresh.role, fresh.role_posted) == ("Backend Intern", today - timedelta(days=3))
    old = process(today - timedelta(days=200))  # over MAX_ROLE_AGE_DAYS (60)
    assert (old.role, old.apply_url, old.role_posted) == (None, None, None)

    digest = build_digest([Result("ramp.com", "drafted", "ok", company="Ramp", role="Backend Intern",
                                  apply_url="https://j/1", role_posted=date(2026, 11, 3))])
    assert "[Backend Intern](https://j/1), posted 3 Nov 2026" in digest


def test_run_writes_the_digest_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace as NS
    from typer.testing import CliRunner
    import main
    from src.pipeline import Result

    found = [Result("ramp.com", "drafted", "ok", company="Ramp", role="Backend Intern",
                    apply_url="https://jobs.ashbyhq.com/ramp/1")]
    monkeypatch.setattr(main, "_pipeline", lambda db, dry_run: NS(run=lambda *a, **k: found, traces=[]))
    monkeypatch.setattr(main, "_db", lambda: None)
    out = tmp_path / "digest.md"
    assert CliRunner().invoke(main.app, ["run", "--dry-run", "--digest", str(out)]).exit_code == 0
    assert "[Backend Intern](https://jobs.ashbyhq.com/ramp/1)" in out.read_text(encoding="utf-8")

    found.clear()  # nothing for you: the file is still written, but empty
    assert CliRunner().invoke(main.app, ["run", "--dry-run", "--digest", str(out)]).exit_code == 0
    assert out.read_text(encoding="utf-8") == ""
