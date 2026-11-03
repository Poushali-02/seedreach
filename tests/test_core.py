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
