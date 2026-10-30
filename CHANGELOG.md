# Changelog

All notable changes to seedreach are listed here. Versions follow [semantic versioning](https://semver.org):
`0.1.x` for fixes, `0.x.0` for new features.

## [Unreleased]

### Changed
- The scheduled-runs template (`examples/github-actions-runner.yml`) now pins `SEEDREACH_REF` to a release
  instead of running `main`, because the job holds your Gmail token.

- CI uses `actions/checkout` and `actions/setup-python` v7 (off the deprecated Node 20 runtime).

### Added
- Dependabot: weekly grouped update PRs for Python packages and GitHub Actions. For Python it only
  changes a requirement when its `>=` range would block a new release, never just to raise minimums.
- This changelog.

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

[Unreleased]: https://github.com/Poushali-02/seedreach/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Poushali-02/seedreach/releases/tag/v0.1.0
