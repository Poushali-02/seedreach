# seedreach

A CLI that finds early-stage startups, reads their website with an LLM, finds a founder's email, and
saves a personalized plain-text cold email to your **Gmail Drafts** for you to review and send yourself.

It is built for people looking for internships or early jobs at small startups. You describe yourself once
in `profile.json`, and every draft picks the project of yours that best matches the company's domain.

**It never sends email.** The Gmail client only calls `drafts().create`, and a test fails if a `send`
call is ever added. Every message waits in your Drafts until you read it and press Send.

```
sources (HN / Product Hunt / manual) -> scraper -> LLM extractor -> email finder
        -> email generator -> Gmail draft -> SQLite (dedupe + status)
```

## Responsible use

This tool writes email to real people, so please use it with care:
- Read every draft before sending, and fix anything the model got wrong (founder names especially).
- Keep volume low and relevant. Send to people you have a real reason to contact, not to everyone you can find.
- Guessed addresses (`{first}@domain`) are unverified. Check them.
- You are responsible for following email and privacy laws where you and your recipients live
  (for example CAN-SPAM, GDPR). Honour any request to stop contacting someone.

## Requirements

- Python 3.10 or newer
- A Gmail account
- A Google Cloud project (free) for Gmail access
- An LLM API key: Gemini (free tier available) or Anthropic

## Setup

### 1. Install

```bash
git clone <your-fork-url>
cd seedreach
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# macOS / Linux:       source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, keep the project in a short path such as `C:\dev\seedreach`. Very long folder
paths make `pip` fail inside the venv.

### 2. Describe yourself in `profile.json`

```bash
cp profile.example.json profile.json     # Windows: copy profile.example.json profile.json
```

Edit `profile.json`. It is git-ignored, so your details stay on your machine.

| Field | Required | What it is |
|---|---|---|
| `name` | yes | Your full name. The first word is used in the intro. |
| `intro` | yes | Completes "I'm Alex, ..." e.g. `a Computer Science student (graduating 2028) based in Lisbon`. |
| `role_pitch` | yes | Completes "I'd love to join Company as ..." e.g. `a remote Backend intern.` |
| `experience` | yes | One or two sentences about your most relevant experience. |
| `stack` | yes | Your core technologies, as a phrase. |
| `projects` | yes | Paragraphs per company domain: `DevTools`, `Fintech`, `AI Agents`, `B2B SaaS`, and a required `Other` fallback. |
| `portfolio`, `github`, `linkedin`, `resume`, `phone` | no | Shown at the bottom. Blank ones are left out. |
| `subject` | no | Subject line; `{company}` is replaced with the company name. |
| `closing` | no | The closing line before your links. |

Plain text only: emails must not contain `*`, `#` or backticks, and the generator refuses to build a draft
that does.

### 3. Add an LLM API key

```bash
cp .env.example .env                     # Windows: copy .env.example .env
```

**Option A: Gemini (free tier).** Create a key at https://aistudio.google.com/apikey and set
`GEMINI_API_KEY=` in `.env`. This is the default (`LLM_PROVIDER=gemini`).

**Option B: Anthropic.** Create a key at https://console.anthropic.com (prepaid credits), then set
`LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=`.

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` or `anthropic` |
| `GEMINI_MODEL` | `gemini-flash-lite-latest` | An alias that survives model retirements. If requests keep failing as "busy", try another model id from AI Studio. |
| `ANTHROPIC_MODEL` | `claude-haiku-5-5` | Any Claude model id |
| `APOLLO_API_KEY` | empty | Optional. Apollo's person lookup is a paid-plan API, so most people leave it blank. |

### 4. Set up Gmail access (`credentials.json`)

1. Go to https://console.cloud.google.com and create a project (any name).
2. Open **APIs & Services > Library**, search for **Gmail API**, and click **Enable**.
3. Open **Google Auth Platform** (or APIs & Services > OAuth consent screen) and click **Get started**:
   - Enter an app name and your email, choose **External**, finish the wizard.
4. **Audience > Test users > Add users**: add the Gmail address you will draft from.
5. **Data access > Add or remove scopes**: add `https://www.googleapis.com/auth/gmail.compose`
   (paste it under "Manually add scopes" if the list is empty), then **Update** and **Save**.
6. **Clients > Create client**: choose **Desktop app**, create it, and click **Download JSON**.
7. Save the file as `credentials.json` in the project folder, next to `main.py`.

On the first real run, a browser opens for consent. Google warns the app is unverified because it is your
own test app. Choose **Advanced > Go to (app name)**. A `token.json` is saved so you are not asked again.

While the consent screen is in **Testing** mode, Google expires the login after 7 days. If you see an
auth error later, delete `token.json` and run again. Do not click "Publish app": you do not need it for
personal use.

`profile.json`, `.env`, `credentials.json`, `token.json` and `outreach.db` are all git-ignored.

## Usage

```bash
# Try one site without Gmail or the database: prints the draft
python main.py target https://example-startup.com --dry-run

# Try many: finds leads and prints drafts, saving nothing
python main.py run --limit 10 --dry-run

# Real run: saves drafts to Gmail and records each company so it is never drafted twice
python main.py run --limit 5
python main.py run --limit 5 --source hn          # hn | producthunt | all

# Queue your own list; the next `run` processes these before sourced leads
python main.py add-target site1.com site2.io

# One specific startup, start to finish
python main.py target https://example-startup.com

# Totals, recent companies and the event log
python main.py status
```

Then open Gmail > Drafts, review each draft, and send the ones you like yourself.

## How it works

- **Hacker News**: finds the latest two "Ask HN: Who is hiring?" threads, then searches their comments for
  remote roles mentioning backend, Python, AI/ML, intern, forward-deployed or systems. Links to job boards
  are ignored; only the company's own site is used. (The `whoishiring` account only posts the thread, so
  searching its own comments returns nothing; the job posts are replies by other users.)
- **Product Hunt**: reads the public feed and follows each post's outbound link. Best effort: Product Hunt
  sometimes blocks scrapers, in which case this source yields nothing.
- **Extraction**: the LLM must return JSON matching a Pydantic schema (company, summary, domain, founder
  name, one-sentence technical hook). Invalid output is retried once, and busy errors are retried with
  backoff. Founder names are only taken if the site states them (About and Team pages are fetched too).
- **Email finding**: Apollo (if you set a key), then `mailto:` addresses on the site, then
  `{first}@domain` with an MX record check. The source and confidence are stored per company.
- **Email copy**: your `projects` paragraph for the company's domain, plus your experience, stack and links.
- **Dedupe**: one row per registrable domain, plus a unique index on drafted emails. Drafted and skipped
  companies are never processed again (use `target <url> --force` to retry a skipped one). LLM or Gmail
  outages are not recorded as skips, so those companies are retried next time.

## Run it on a schedule (free, GitHub Actions)

Your laptop does not need to be on. A small **private** repo runs seedreach on a schedule, pulls this
code at run time, and keeps `outreach.db` on a `state` branch so nobody is drafted twice.
It must be private: Actions logs and the database contain founder email addresses.

1. **Stop the Gmail token expiring.** In Google Cloud > Google Auth Platform > Audience, click
   **Publish app** (status "In production"). You stay the only user and no verification is needed for
   personal use. Tokens issued while the app was in Testing still expire after 7 days, so delete
   `token.json` and run `python main.py run --limit 1` once locally: it opens the browser to sign in
   again and writes a fresh, long-lived `token.json`.
2. **Create the private repo** with the workflow:
   ```bash
   gh repo create seedreach-runner --private
   ```
   Copy [`examples/github-actions-runner.yml`](examples/github-actions-runner.yml) to
   `.github/workflows/seedreach.yml` in it, and push to `main`.
3. **Create the `state` branch** holding your current database (or an empty one):
   ```bash
   git switch --orphan state
   cp /path/to/seedreach/outreach.db .
   git add outreach.db && git commit -m "initial state" && git push -u origin state
   ```
4. **Add secrets** (Settings > Secrets and variables > Actions), from your local files:
   ```bash
   gh secret set GMAIL_TOKEN       -R you/seedreach-runner < token.json
   gh secret set GMAIL_CREDENTIALS -R you/seedreach-runner < credentials.json
   gh secret set PROFILE_JSON      -R you/seedreach-runner < profile.json
   gh secret set GEMINI_API_KEY    -R you/seedreach-runner      # paste the key when prompted
   ```
   Optional: `ANTHROPIC_API_KEY`, `APOLLO_API_KEY`, and repo variables `LLM_PROVIDER` / `GEMINI_MODEL`.
5. **Test it:** Actions tab > seedreach > Run workflow, with "dry run" ticked.

Control: change the `cron:` line for timing, use **Run workflow** for an on-demand run with its own
limit and source, and **Disable workflow** to pause. GitHub emails you when a run fails (for example,
a Gmail token that needs renewing: the run fails fast with `HEADLESS` in the error, never hangs).

Once the runner is live, do your real runs there rather than locally, or your local `outreach.db` and the
runner's copy drift apart and could draft the same founder twice.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `profile.json not found` | Copy `profile.example.json` to `profile.json` and edit it. |
| `GEMINI_API_KEY is not set` | Add the key to `.env`, or switch `LLM_PROVIDER`. |
| Gemini `404 ... no longer available` | Google retired that model. Set `GEMINI_MODEL` to a current id. |
| Gemini `503` or timeouts | Google is overloaded. The tool retries; try again later or use a lite model. |
| `no founder email found` for most sites | Normal without a paid lookup API. Run more companies, or use `add-target` for sites that name their founders. |
| Google "access denied" at sign-in | Add your Gmail under Audience > Test users. |
| Auth error after a week | Delete `token.json` and run again. |
| `pip install` fails on Windows | Move the project to a shorter folder path and recreate `.venv`. |

## Development

```bash
pytest
```

Tests use `profile.example.json` and mock all network and LLM calls, so they need no keys. CI runs them on
Python 3.10 and 3.13. Pull requests are welcome; please keep drafts-only behaviour intact.

```
main.py                      Typer CLI
profile.example.json         template for your personal details
src/config.py                Pydantic settings (.env)
src/profile.py               loads and validates profile.json
src/db.py                    SQLite dedupe + status + event log
src/pipeline.py              orchestration
src/sourcing/                hacker_news.py, product_hunt.py (BaseSourcingProvider)
src/intelligence/            scraper.py, extractor.py
src/enrichment/              email_finder.py
src/outreach/                generator.py, gmail_client.py
tests/
```

## License

MIT. See [LICENSE](LICENSE).
