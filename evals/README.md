# Evals

An answer key of real startups, so a change to seedreach can be measured instead of guessed at.

The dataset lives in `evals/data/`, which is git-ignored: it holds real founder names, email
addresses and copies of company websites, so it never goes in the public repo. Only these scripts
and the final scores are public.

## 1. List the companies

Create `evals/data/companies.csv`:

```csv
url,source,case,name_hint
https://useshiplog.com,hacker_news,normal,Shiplog
```

- `url`: the page seedreach scraped (the same URL a run would use, even if it's a careers page).
- `source`: where the lead came from (`hacker_news`, `yc`, `launchhn`, `producthunt`).
- `case`: why it's in the set, e.g. `normal`, `no-founder`, `wrong-website`.
- `name_hint`: the company name the source gave, as a run would pass it. Can be blank.

## 2. Save the pages and pre-fill the answer key

```bash
python -m evals.snapshot
```

Each website is scraped once and saved to `evals/data/pages/<domain>.json`, with the model's answer.
Later runs read the saved copy, so a score only changes when the code changes. Use `--refresh` to
download the pages again.

## 3. Correct the answer key

Open `evals/data/labels.csv` (Excel or Google Sheets is fine). Each row starts with the model's
answer; visit the site and fix what's wrong:

| Column | Put the true value |
|---|---|
| `right_website` | `yes` if this is the company's own site, `no` if it's a news article, job board, etc. |
| `company_name` | The company's name |
| `core_domain` | `DevTools`, `AI Agents`, `Fintech`, `B2B SaaS` or `Other`. Two allowed if both fit: `DevTools\|AI Agents` |
| `founder_first_name`, `founder_last_name` | Only if the site calls the person a founder or co-founder (a CEO who isn't called a founder doesn't count). Leave blank if it doesn't |
| `other_founders` | Any other founders the site names, separated by `;` (e.g. `Sam Bobb; Max`). Naming any of them also counts as correct |
| `hook_ok` | `yes` if every claim in `model_hook` is true and on the site, `no` otherwise |
| `checked` | `yes` once you've checked the row |

`emails_on_site` lists the addresses found on the pages, for reference. Re-running the script keeps
every row you've edited.
