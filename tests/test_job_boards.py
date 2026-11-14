"""Job boards (#13): finding board links, reading Greenhouse/Lever/Ashby, naming the role in the draft.

The JSON fixtures are trimmed copies of real public responses (Vercel on Greenhouse, Palantir on Lever,
Ramp on Ashby), recorded on 2026-11-13. No test makes a network call.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from src.enrichment.job_boards import Board, BoardCheck, JobBoardChecker, Role, find_boards
from src.intelligence.scraper import _parse

FIXTURES = Path(__file__).parent / "fixtures" / "job_boards"
API_FILES = {
    "boards-api.greenhouse.io": "greenhouse_vercel.json",
    "api.lever.co": "lever_palantir.json",
    "api.ashbyhq.com": "ashby_ramp.json",
}


def _checker(handler) -> JobBoardChecker:
    return JobBoardChecker(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _recorded(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=json.loads((FIXTURES / API_FILES[request.url.host]).read_text("utf-8")))


def test_find_boards_reads_every_link_style_and_ignores_the_rest():
    urls = [
        "https://boards.greenhouse.io/acme",
        "https://job-boards.greenhouse.io/acme/jobs/123",  # same board again: deduplicated
        "https://boards.greenhouse.io/embed/job_board?for=widgets",  # embedded board
        "https://jobs.lever.co/palantir/6ed76ce8",
        "https://jobs.ashbyhq.com/ramp/embed?version=2",
        "https://jobs.ashbyhq.com/deaa6ad2-2b2b-4c4c-8d8d-1234567890ab",  # Ashby names can be UUIDs
        "https://jobs.lever.co/",  # no company in the path
        "https://boards.greenhouse.io/embed/job_board",  # embed with no company
        "https://acme.dev/careers",
        "not a url at all",
    ]
    assert find_boards(urls) == [
        Board("greenhouse", "acme"), Board("greenhouse", "widgets"), Board("lever", "palantir"),
        Board("ashby", "ramp"), Board("ashby", "deaa6ad2-2b2b-4c4c-8d8d-1234567890ab"),
    ]


def test_scraper_collects_board_links_from_links_iframes_and_scripts():
    html = """<html><body><p>Acme builds agents.</p>
    <a href="https://jobs.lever.co/acme">Jobs</a>
    <iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>
    <script src="https://jobs.ashbyhq.com/acme/embed"></script>
    <a href="/careers">Careers</a><a href="/about">About</a></body></html>"""
    page, extra = _parse(html, "https://acme.dev")
    assert [b.label for b in page.job_boards] == ["lever:acme", "greenhouse:acme", "ashby:acme"]
    assert page.careers_url == "https://acme.dev/careers"
    assert extra == ["https://acme.dev/about"]  # the careers page still isn't read for founders


def test_scraper_finds_board_urls_inside_page_data():
    # e.g. a careers page that renders its openings from embedded JSON (Ramp's does this with Ashby)
    html = """<html><body><p>Join us.</p><script id="__NEXT_DATA__" type="application/json">
    {"jobs":[{"url":"https:\\/\\/jobs.ashbyhq.com\\/ramp\\/0378cc97"}]}</script>
    <div data-board="https://job-boards.greenhouse.io/acme/jobs/1"></div></body></html>"""
    page, _ = _parse(html, "https://ramp.com/careers")
    assert [b.label for b in page.job_boards] == ["ashby:ramp", "greenhouse:acme"]


def test_reads_internships_from_recorded_greenhouse_lever_and_ashby_responses():
    boards = [Board("greenhouse", "vercel"), Board("lever", "palantir"), Board("ashby", "ramp")]
    check = _checker(_recorded).check(boards)
    assert check.problems == []
    assert [(r.board, r.title.strip()) for r in check.roles] == [
        ("greenhouse:vercel", "Software Engineering Intern - Summer '27"),
        ("lever:palantir", "Finance Operator Intern"),
        ("lever:palantir", "Deployment Strategist, Internship"),
        ("lever:palantir", "Meritocracy Fellowship"),  # Lever marks it "Internship" though the title doesn't
        ("ashby:ramp", "Software Engineering Intern, Backend"),
        ("ashby:ramp", "Applied Scientist Intern"),
    ]
    assert check.roles[0].url.startswith("https://job-boards.greenhouse.io/vercel/jobs/")


def test_best_role_names_engineering_internships_only_and_prefers_backend_ai():
    def role(title):
        return Role(title, "", "x")

    roles = [role("Finance Operator Intern"), role("Software Engineering Intern, Frontend"),
             role("Software Engineering Intern, Backend")]
    assert BoardCheck(roles).best_role().title == "Software Engineering Intern, Backend"
    assert BoardCheck([role("Applied Scientist Intern")]).best_role().title == "Applied Scientist Intern"
    assert BoardCheck([role("Finance Operator Intern"), role("Meritocracy Fellowship")]).best_role() is None
    assert BoardCheck([]).best_role() is None


def test_whole_word_matching_skips_internal_and_international_roles():
    data = {"jobs": [{"title": "Internal Tools Engineer", "absolute_url": "u1"},
                     {"title": "International Sales Lead", "absolute_url": "u2"},
                     {"title": "Backend Engineering Co-op", "absolute_url": "u3"},
                     {"title": "New Grad Software Engineer", "absolute_url": "u4"}]}
    check = _checker(lambda r: httpx.Response(200, json=data)).check([Board("greenhouse", "acme")])
    assert [r.title for r in check.roles] == ["Backend Engineering Co-op", "New Grad Software Engineer"]


def test_ashby_skips_unlisted_roles_and_empty_boards_are_not_errors():
    data = {"jobs": [{"title": "Software Engineering Intern", "jobUrl": "u", "isListed": False,
                      "employmentType": "Intern"}], "apiVersion": "1"}
    check = _checker(lambda r: httpx.Response(200, json=data)).check([Board("ashby", "acme")])
    assert check.roles == [] and check.problems == []
    empty = _checker(lambda r: httpx.Response(200, json={"jobs": [], "apiVersion": "1"}))
    assert empty.check([Board("ashby", "vercel")]) == BoardCheck()  # a name with no board: just nothing found


def test_unreadable_boards_are_reported_never_raised():
    responses = {
        "greenhouse": httpx.Response(404, json={"status": 404, "error": "Job not found"}),
        "lever": httpx.Response(404, json={"ok": False, "error": "Document not found"}),
        "ashby": httpx.Response(200, text="<html>not json</html>"),
    }

    def handler(request):
        if "slow" in request.url.path:
            raise httpx.ReadTimeout("slow", request=request)
        return responses[next(k for k in responses if k in request.url.host)]

    boards = [Board("greenhouse", "gone"), Board("lever", "gone"), Board("ashby", "acme"), Board("lever", "slow")]
    check = _checker(handler).check(boards)
    assert check.roles == []
    assert check.problems == ["greenhouse:gone not_found", "lever:gone not_found", "ashby:acme bad_json",
                              "lever:slow timeout"]


def test_roles_keep_the_date_each_board_says_they_were_first_posted():
    from datetime import date
    from src.enrichment.job_boards import _roles_from

    # field names and formats as the live APIs returned them on 2026-11-13
    greenhouse = {"jobs": [{"title": "Software Engineering Intern", "absolute_url": "g",
                            "updated_at": "2026-10-30T14:45:41-04:00",  # changes on every sync: ignored
                            "first_published": "2026-05-21T09:26:09-04:00"}]}
    lever = [{"text": "Backend Intern", "hostedUrl": "l", "categories": {"commitment": "Internship"},
              "createdAt": 1786469891368}]
    ashby = {"jobs": [{"title": "Data Intern", "jobUrl": "a", "isListed": True, "employmentType": "Intern",
                       "publishedAt": "2025-07-31T21:58:34.674+00:00"},
                      {"title": "ML Intern", "jobUrl": "b", "isListed": True, "publishedAt": "not a date"}]}
    assert [r.posted for r in _roles_from(Board("greenhouse", "v"), greenhouse)] == [date(2026, 5, 21)]
    assert [r.posted for r in _roles_from(Board("lever", "p"), lever)] == [date(2026, 8, 11)]
    assert [r.posted for r in _roles_from(Board("ashby", "r"), ashby)] == [date(2025, 7, 31), None]


def test_best_role_skips_old_openings_and_prefers_the_newest():
    from datetime import date, timedelta

    today = date(2026, 11, 13)

    def role(title, days_ago):
        return Role(title, title, "x", None if days_ago is None else today - timedelta(days=days_ago))

    roles = [role("Backend Intern (old)", 400), role("Backend Intern (new)", 10), role("Backend Intern (mid)", 45)]
    assert BoardCheck(roles).best_role(60, today).title == "Backend Intern (new)"
    # the limit counts whole days: 60 days ago is still in, 61 is out
    assert BoardCheck([role("Backend Intern", 60)]).best_role(60, today) is not None
    assert BoardCheck([role("Backend Intern", 61)]).best_role(60, today) is None
    # a preferred (backend/AI/data) role still beats a newer frontend one, as long as it's recent enough
    assert BoardCheck([role("Frontend Engineering Intern", 1), role("Backend Intern", 30)]).best_role(
        60, today).title == "Backend Intern"
    # no date: kept, but a dated recent role wins over it
    assert BoardCheck([role("Backend Intern", None)]).best_role(60, today).posted is None
    assert BoardCheck([role("AI Intern", None), role("AI Intern", 5)]).best_role(60, today).posted is not None
    # 0 turns the limit off
    assert BoardCheck([role("Backend Intern", 900)]).best_role(0, today) is not None
    stale = BoardCheck(roles)
    assert len(stale.recent(stale.engineering_roles(), 60, today)) == 2
