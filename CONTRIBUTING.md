# Contributing to seedreach

Thanks for helping. seedreach is a small project maintained in spare time, so short, focused pull
requests are the easiest to review.

## Ground rules

These are what make seedreach safe to point at real people's inboxes. Pull requests that break them
won't be merged.

1. **Drafts only.** Nothing may send email. The Gmail client only calls `drafts().create`, and
   `test_gmail_client_never_sends` fails if a `send` call appears. Don't work around it.
2. **A human stays in the loop.** No auto-send, no scheduling of sends, no bulk "approve all".
3. **Plain text emails.** No markdown or HTML in drafts; the generator enforces this.
4. **No personal data in the repo.** Your details belong in `profile.json`, keys in `.env`; both are
   git-ignored. Use `profile.example.json` in tests.
5. **Never contact the same person twice.** Changes to the pipeline or database must keep the
   per-domain and per-email dedupe.

## Setup

```bash
git clone https://github.com/Poushali-02/seedreach.git
cd seedreach
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
```

The tests mock every network and LLM call, so they need no API keys. They must pass on Python 3.10
and 3.13 (CI runs both).

## Making a change

- Open an issue first for anything bigger than a bug fix, so we can agree on the approach.
- Keep the style of the surrounding code: type hints, small functions, comments only where the
  reason isn't obvious.
- Add or update a test for any behaviour change.
- Try it end to end with `python main.py target <url> --dry-run` (prints the draft, touches nothing).

Good first areas: new lead sources (implement `BaseSourcingProvider.iter_leads`), better company-stage
filtering, and more reliable email discovery.

## Reporting security issues

Please don't open a public issue for anything that could expose tokens or send email. See
[SECURITY.md](SECURITY.md).
