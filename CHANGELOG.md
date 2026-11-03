# Changelog

All notable changes to seedreach are listed here. Versions follow [semantic versioning](https://semver.org):
`0.1.x` for fixes, `0.x.0` for new features.

## [Unreleased]

## [0.3.1] - 2026-11-03

Reach real people: no drafts to blind email guesses or to founders the site doesn't name, shorter
first emails, and no drafts for news articles or job-board pages.

### Changed
- No more drafts to bare guesses. Every address now has a confidence (`verified`, `on site`,
  `pattern from site`, `generic`, `guess`), and a `{first}@domain` guess with nothing behind it is
  recorded as "needs contact" instead of drafted; `status` lists these with the guessed address
  (#3). In v0.3.0's runs, 54% of drafts went to such guesses. `ALLOW_GUESSED_EMAILS=1` restores the
  old behaviour.
- If the site shows someone's address in a `first.last@` or `first@` format, the founder's address
  follows that format instead of being a blind guess.
- A founder the model names is only used if the site names them too; otherwise the draft greets the
  team (#32).
- Greetings and subjects drop legal endings: "Hi Vernius Systems team", not "Vernius Systems, Inc." (#32).
- Drafts are short by default (#31): the opener, who you are, the first sentence of your best-matching
  project, one ask and two links, about 100–130 words (they were about 190–220 words in 8–9
  paragraphs). `"email_style": "long"` in `profile.json` keeps the previous email; `"ask"` changes the
  last line.
- News articles and job-board pages are skipped instead of drafted (#30). Known job-board and news hosts
  (Teamtailor, Factorial, Lever, Greenhouse, TechCrunch, …) are skipped before any scraping or LLM call,
  and the model now labels every other page (`page_type`: company, news, job board, other). The skip
  reason names the company so you can add its real website with `target`. The evals report how many
  wrong-website leads are caught and how many company sites are wrongly flagged.

## [0.3.0] - 2026-11-01

Measured, then fixed: on 30 hand-checked startups, ready-to-send drafts went from 43% to 63–70% and
drafts naming a made-up founder from 11% to 0–4%.

### Added
- Evals: `python -m evals.snapshot` saves company pages and pre-fills an answer key to check by hand;
  `python -m evals.run_evals` scores extraction against it (see `evals/README.md`). Current scores are
  in the README under "How well it works". The answer key's `other_founders` column lists every other
  founder a site names (naming any of them counts as correct), and `--pages` scores another set of saved
  pages, for before/after comparisons.
- Run traces: every real run records, per company, the time each step took, LLM tokens and retries,
  pages read, whether the text was cut at the length limit, and whether a named founder appears on
  the page. `run` prints a summary at the end; `status` shows recent runs. Set
  `LLM_PRICE_INPUT_PER_M` / `LLM_PRICE_OUTPUT_PER_M` to see cost.

### Changed
- A person counts as the founder only if the site calls them a founder or co-founder; a CEO or CTO
  alone no longer does. More drafts now greet the team instead of a person.
- The scraper finds founders more often:
  - It ranks same-site links by how likely they are to name the founders: team, founders,
    leadership, "about us", "Über uns" first, then about/company/vision, then contact.
  - It matches whole words, so "how teams use X" no longer counts as a team page.
  - It skips careers, blog, press and legal pages, and reads up to 3 extra pages instead of 2.
  - Each extra page keeps its own 3,000 characters, starting at the first mention of a founder, so a
    long homepage can't push the team page out. The total limit is now 16,000 characters (was 12,000).

### For contributors
- A CLI smoke test: CI fails if `main.py` can't start.
- CONTRIBUTING and the PR template ask you to wait to be assigned before opening a PR for an issue,
  and to paste the output of a dry run.

## [0.2.0] - 2026-10-31

More places to find startups, and a project that's ready for contributors.

### Added
- Two new lead sources: the **YC directory** (`--source yc`: hiring, active YC companies of 25 people or
  fewer, remote-friendly first) and **Launch HN** (`--source launchhn`: recent launches on Hacker News).
- `--source all` (the default) now takes turns between sources instead of draining one before the next,
  and never processes the same company twice in a run.
- Dependabot: weekly grouped update PRs for Python packages and GitHub Actions. For Python it only
  changes a requirement when its `>=` range would block a new release, never just to raise minimums.
- This changelog.
- Contributor docs: `docs/ARCHITECTURE.md` (how a run works, how to add a source), a pull-request
  template and a code of conduct.

### Changed
- The scheduled-runs template (`examples/github-actions-runner.yml`) pins `SEEDREACH_REF` to a release
  instead of running `main`, because the job holds your Gmail token, and defaults to `all` sources.
- CI uses `actions/checkout` and `actions/setup-python` v7 (off the deprecated Node 20 runtime).

## [0.1.0] - 2026-10-28

First public release.

### Added
- Lead sourcing from Hacker News "Who is hiring?" threads, Product Hunt (best effort) and your own list
  (`add-target`). Sources are read lazily, so runs keep going past companies already handled.
- LLM extraction with Gemini (default, free tier) or Claude, validated against a Pydantic schema, with
  retries and timeouts for busy or slow APIs.
- Founder email discovery: Apollo (optional), `mailto:` links on the site, then an MX-checked
  `{first}@domain` guess. The source and confidence are stored for every company.
- Plain-text drafts whose project paragraph matches the company's domain, built from your `profile.json`.
- Gmail drafts only: a test fails if any code ever tries to send.
- SQLite dedupe by domain and email, so nobody is contacted twice.
- `HEADLESS` mode and a GitHub Actions template for scheduled runs from a private repo.
- Contributing guide, security policy, issue templates and CI on Python 3.10 and 3.13.

[Unreleased]: https://github.com/Poushali-02/seedreach/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/Poushali-02/seedreach/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Poushali-02/seedreach/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Poushali-02/seedreach/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Poushali-02/seedreach/releases/tag/v0.1.0
