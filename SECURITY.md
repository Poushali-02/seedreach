# Security policy

seedreach holds a Gmail OAuth token and acts on your mailbox, so security reports are taken seriously.

## Reporting a vulnerability

Please report privately, not in a public issue:

- Use GitHub's **Report a vulnerability** button on the repository's Security tab, or
- email poushalibhattacharyya02@gmail.com with "seedreach security" in the subject.

Include what you found, how to reproduce it, and its impact. You'll get a reply within a week.

## What counts

- Any way to make seedreach **send** an email, or act on mail other than creating drafts.
- Leaks of `token.json`, `credentials.json`, API keys or `profile.json` (in logs, errors, the
  database, or committed files).
- Prompt injection through scraped websites that changes who a draft goes to or what it contains
  beyond the expected personalisation.
- Workflow problems in `examples/github-actions-runner.yml` that could expose secrets or the database.

## Supported versions

Only the latest release on `main` gets fixes.
