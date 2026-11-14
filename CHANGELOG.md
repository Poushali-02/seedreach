# Changelog

All notable changes to seedreach are listed here. Versions follow [semantic versioning](https://semver.org):
`0.1.x` for fixes, `0.x.0` for new features.

## [Unreleased]

## [0.5.0] - 2026-11-14

Fewer wrong companies: no drafts to companies that don't build software.

### Added
- Companies that clearly don't sell software are skipped (#46). The model already reads each site, so it
  now also says what the company mainly sells (software, hardware, services or other). A company is
  recorded as skipped, before the job board and email lookups, only when that answer isn't software
  **and** its domain is "Other", with the reason ("not a software company: sells a service (Laundry
  pickup and delivery.)"). Both must agree because the model sometimes calls an AI device "hardware"
  while filing it under AI Agents. Software sold to any industry counts as software, and when the model
  is unsure it answers software. `SKIP_NON_SOFTWARE=0` turns it off. The evals report software startups
  that would be skipped, which must stay at zero.

## [0.4.0] - 2026-11-13

The right companies: Indian and remote startups first, open internships with fresh apply links, no
drafts to generic inboxes when there's a founder to reach, and a digest of each run on your phone.

### Added
- `PREFER_REGIONS` ranks leads from every source by region, most wanted first, for example
  `PREFER_REGIONS=India,Remote` (#35): companies listing India come first, then remote ones, then the
  rest. Nothing is dropped. YC companies carry their YC regions, Hacker News roles count as `Remote`
  (that source only keeps remote roles), and Launch HN and Product Hunt leads count as the rest. Empty
  (the default) keeps the old order. The scheduled-runs template passes `YC_REGIONS` and
  `PREFER_REGIONS` from repository variables.
- `YC_REGIONS` keeps only YC companies that list one of the given regions, for example
  `YC_REGIONS=India,Remote` (#35). Names match YC's own region names whole and ignore case, so `Remote`
  doesn't also pick up `Partly Remote`. A name no company uses is logged as a warning. Empty (the
  default) keeps every region, as before.
- Drafts name an open internship when the company has one (#13). If the site links to a public Greenhouse,
  Lever or Ashby job board (on the pages already read, or once on its careers page), seedreach reads the
  board's public API and names an engineering, AI or data internship in the draft: "I saw your Backend
  Engineering Intern opening." Finance or sales internships aren't mentioned, "Internal Tools" and
  "International" don't count as internships, and a board that can't be read never stops the draft.
  `CHECK_JOB_BOARDS=0` turns it off.
- Internships first posted more than 60 days ago aren't named (`MAX_ROLE_AGE_DAYS`, 0 for no limit).
  Job boards list every posting the company hasn't taken down, and some stay up for years: Palantir's
  Lever board still lists openings from 2024. seedreach now reads each posting's date (Greenhouse
  `first_published`, Lever `createdAt`, Ashby `publishedAt`), prefers the newest, and shows the date in
  the digest and the run log. A role whose board gives no date is kept.
- `run --digest FILE` writes a short Markdown summary of the run: open internships with apply links,
  founders to reach on LinkedIn when only a generic or guessed address was found, and drafts to review.
  It holds no email addresses. The scheduled-runs template posts it as a comment on an issue titled
  "seedreach: apply and reach out" and @-mentions you, so it reaches your phone through the GitHub app
  (the workflow now needs `issues: write`). Companies skipped for lack of a good address keep their apply
  link: the job board is now read before the email checks.
- Optional run summary to Telegram and/or Discord after `run` ("3 new drafts: HillStar, Alter, Rhem Labs
  · 2 skipped"), so scheduled runs are easy to review later (#19, by @musmanghani13). Set
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, or `DISCORD_WEBHOOK_URL`. It lists company names and counts
  only, never founder addresses, draft text or skip reasons. Not sent on `--dry-run`; a failed send only
  logs a warning.
- With an Apollo key, companies with more than `MAX_EMPLOYEES` people (100 by default) are skipped
  before any scraping or LLM call, using Apollo's organization lookup, which the free plan allows.
  Companies Apollo doesn't know are kept, and each draft shows the stage and size it found ("Seed, ~7
  people"). Headcount is used rather than funding stage, because Apollo labels Ramp, Vercel and Stripe
  all "Venture (Round not Specified)".

### Changed
- If Apollo refuses the key (a paid-only endpoint on the free plan, or a bad key), seedreach logs it once
  and stops asking for the rest of the run, instead of failing on every company.
- No more drafts to `hello@`, `info@`, `contact@` or `team@` when the site names a founder: those
  inboxes rarely reach the founder. The company goes under *Needs a contact* instead, and `status` shows
  the founder with a LinkedIn search link so you can reach them directly. In the scheduled runner's first
  108 drafts, 41% went to such generic inboxes. Without a named founder the generic inbox is still drafted,
  and `founders@` always counts as the founders' own. `GENERIC_INBOX=always` restores the old behaviour;
  `GENERIC_INBOX=never` never drafts to one.
- Tests no longer read your `.env` or shell settings, so a personal setting such as `PREFER_REGIONS`
  can't make them fail locally.

## [0.3.2] - 2026-11-06

Gemini works again on fresh installs, and the scheduled-runs template runs only pinned code.

### Fixed
- Every Gemini call failed with `404 NOT_FOUND` on new installs. `google-genai` 3.0.0 (released
  2026-11-06) changed its default API version from `v1beta` to `v1`, where `gemini-flash-lite-latest`
  isn't available. `requirements.txt` now caps it at `<3`. If you're stuck on 3.x, set
  `GOOGLE_GENAI_API_VERSION=v1beta`.

### Changed
- GitHub Actions in `tests.yml` and the scheduled-runs template are pinned to commit IDs (with the
  version as a comment), and the template pins `SEEDREACH_REF` to the release's commit instead of its
  tag. A tag can be moved to different code; a commit can't, and the runner job holds your Gmail token.
  The template also moves to `actions/checkout` and `actions/setup-python` v7, matching `tests.yml`.

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

[Unreleased]: https://github.com/Poushali-02/seedreach/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Poushali-02/seedreach/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Poushali-02/seedreach/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/Poushali-02/seedreach/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/Poushali-02/seedreach/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Poushali-02/seedreach/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Poushali-02/seedreach/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Poushali-02/seedreach/releases/tag/v0.1.0
