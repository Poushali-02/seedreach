# How seedreach works

A guide for contributors: what happens during a run, where each piece lives, and how to add a new
lead source.

## One run, start to finish

```
python main.py run --limit 5
        │
        ▼
 queued targets (add-target) ──┐
                               ├─▶ lead ─▶ scrape site ─▶ LLM reads it ─▶ find founder email ─▶ write draft ─▶ Gmail draft
 sources take turns ───────────┘          (scraper)      (extractor)      (email_finder)        (generator)    (gmail_client)
 yc · launchhn · hn · producthunt                                                                                    │
                                                                                                                     ▼
                                                                                                     SQLite: drafted / skipped
```

1. **Leads.** `Pipeline.run` (in `src/pipeline.py`) first processes companies queued with `add-target`,
   then pulls leads from the sources, one from each in turn (`interleave`). Companies already in the
   database, or already seen in this run, are skipped at no cost.
2. **For each lead**, `Pipeline.process`:
   1. Normalises the URL to a domain and checks the database. A company that was drafted or skipped
      before counts as a `duplicate` and is never contacted twice.
   2. With an Apollo key, looks up the company's headcount and skips it if it's over `MAX_EMPLOYEES`,
      before any scraping or LLM call (`src/enrichment/company_facts.py`). Unknown companies are kept.
   3. Scrapes the homepage plus up to two About or Team pages (`src/intelligence/scraper.py`).
   4. Asks the LLM for a structured `CompanyProfile`: name, summary, domain, founder name and a
      one-line technical opener (`src/intelligence/extractor.py`). The output is validated with
      Pydantic and retried once if invalid.
   5. Finds an email, best first: Apollo-verified (if configured), the founder's own address on the
      site, the founder's name in the site's address format, anyone's address on the site, a generic
      inbox (`src/enrichment/email_finder.py`). A bare `{first}@domain` guess is recorded as
      "needs contact" instead of drafted, unless `ALLOW_GUESSED_EMAILS` is set. So is a generic inbox
      (`hello@`, `info@`) when the site names a founder, unless `GENERIC_INBOX` says otherwise.
   6. If the site links to a Greenhouse, Lever or Ashby job board, reads it for open internships and
      picks an engineering, AI or data one to name in the draft (`src/enrichment/job_boards.py`).
      A board that can't be read is logged with a reason and never stops the draft.
   7. Writes a plain-text draft from your `profile.json`, choosing the project paragraph that matches
      the company's domain (`src/outreach/generator.py`).
   8. Saves it to Gmail Drafts (`src/outreach/gmail_client.py`) and records it in SQLite (`src/db.py`).
3. The run stops after `--limit` drafts, or after `limit × 6` attempts to cap LLM spend.

### What gets recorded

| Outcome | When | Recorded? |
|---|---|---|
| `drafted` | A draft was saved to Gmail | Yes, never processed again |
| `skipped` | Site unreachable, or no email found | Yes, never retried (unless `target --force`) |
| `duplicate` | Already in the database | No, nothing to do |
| `error` | LLM or Gmail outage | **No**, so it's retried next run |

`--dry-run` prints drafts and records nothing.

## Where things live

| Path | Responsibility |
|---|---|
| `main.py` | Typer CLI: `run`, `target`, `add-target`, `status` |
| `src/config.py` | Settings from `.env` (Pydantic `BaseSettings`) |
| `src/profile.py` | Loads and validates `profile.json` (your details) |
| `src/db.py` | SQLite: companies, statuses, event log, dedupe, run traces |
| `src/trace.py` | `Trace`: one company's run, step by step (time, tokens, pages read, text cut, founder on the page) |
| `src/pipeline.py` | Orchestration, source registry (`build_providers`), `interleave` |
| `src/sourcing/` | Lead sources: one module per source |
| `src/intelligence/` | `scraper.py` (HTML to text) and `extractor.py` (LLM to `CompanyProfile`) |
| `src/enrichment/` | `email_finder.py`, `job_boards.py` (open internships on public job boards) and `company_facts.py` (company size from Apollo) |
| `src/outreach/` | `generator.py` (email text) and `gmail_client.py` (drafts only) |
| `tests/test_core.py`, `tests/test_job_boards.py` | Tests; every network and LLM call is faked (job-board fixtures are recorded real responses) |

## Adding a lead source

Every source turns some public listing into `Lead`s that point at a **company's own website**. The
rest of the pipeline is shared. `src/sourcing/launch_hn.py` and `src/sourcing/yc_directory.py` are
short, complete examples.

1. **Create `src/sourcing/<name>.py`** with a subclass of `BaseSourcingProvider`:

   ```python
   class ExampleProvider(BaseSourcingProvider):
       name = "example"

       def __init__(self, settings: Settings, client: Optional[httpx.Client] = None) -> None:
           self.settings = settings
           self.client = client or httpx.Client(timeout=settings.http_timeout)

       def iter_leads(self) -> Iterator[Lead]:
           try:
               resp = self.client.get(API)
               resp.raise_for_status()
               items = resp.json()
           except (httpx.HTTPError, ValueError) as exc:
               log.warning("Example source unavailable: %s", exc)
               return                      # never raise: other sources keep going
           for item in items:
               if item.get("website"):     # only leads with the company's own site
                   yield Lead(url=item["website"], source=self.name,
                              name_hint=item.get("name"), context=item.get("tagline"))
   ```

   Rules:
   - **Yield lazily**, best leads first. The pipeline may read far past the first few to find
     companies it hasn't handled.
   - **Never raise** on network or parsing problems: log a warning and stop.
   - **Only yield company websites**, never job boards, GitHub, social networks or news articles.
     `_is_company_host` in `src/sourcing/hacker_news.py` filters common non-company hosts.
   - Use official APIs or feeds. Don't scrape sites whose terms forbid it (LinkedIn, Wellfound, X).
2. **Register it** in `build_providers` in `src/pipeline.py`, and add its name to the `--source`
   help text in `main.py`.
3. **Make it configurable** if it needs settings: add a field to `src/config.py` and a line to
   `.env.example`.
4. **Test it** with a fake client instead of the network. See `_FakeResponse` and the YC and Launch HN
   tests in `tests/test_core.py`.
5. **Try it for real:** `python main.py run --limit 2 --dry-run --source <name>`.
6. **Document it** in the README's "How it works" list and in `CHANGELOG.md` under Unreleased.
