# Lead Puller (AnavClouds × Seamless.AI) — v1.1.1

Type who you want in plain English → Claude turns it into Seamless filters → preview a small sample →
collect the list → choose who to research → download an AnavMail-ready CSV.

**All settings live in `.env`.** To change one, edit that line and run `docker compose up -d`.
The Settings page in the app shows the current values (secrets masked) but can't change them.

## How a pull works

| Step | What happens | Seamless credits |
|---|---|---|
| 1. Filters | Claude fills the filter form and lists every assumption. Edit any filter, or adjust in words. Set company rules for this pull (cooldown days, max per company). | 0 |
| 2. Preview | One small search page. Non-US locations are checked against Seamless's location list first (free). If the same filters were run before, it **continues where that run stopped** (button to start from the top instead). | ~3 |
| 3. Collect | Pages until the target is reached. Skips: do-not-pull list, anyone pulled before, **companies pulled within the cooldown**, duplicates, per-company limit, skip words/industries/domains. Stops early if most results are being skipped. | 1 per 10 results returned (skipped ones count) |
| 4. Choose | Everyone / good + maybe fit / good only / first N, plus tick boxes. Checks the monthly budget and live balance first. | 0 |
| 5. Research | Batches sent for research and checked every few seconds. Right before paying, each person and company is re-checked (another pull may have taken them meanwhile). | 1 per person |
| 6. Results | Hit rate by country, AnavMail CSV, full CSV, Excel, "Run again, new people only", and **Fill in from Seamless saved contacts** for people Seamless says were already researched. | 0 |

## Company memory and cooldown
Every company someone is researched at is remembered (by website domain, else company name). New pulls skip
those companies for `COMPANY_COOLDOWN_DAYS` (default 90). The **Companies** page lists them with
"Skipped until <date>", **Allow again now**, and **Never pull** (adds the domain to the do-not-pull list).

## Money and data safety (what the tests prove)
`tests/test_money.py` runs every scenario against a deliberately awkward fake Seamless that charges exactly
as Seamless's docs say and keeps its own books. After each scenario it checks:
- the tool's credit ledger equals what "Seamless" charged, to the credit;
- nobody is ever charged for twice — across pulls, two pulls at once, restarts, pause/stop;
- every email/phone is attached to the right person (emails in the fake encode the person's id), even when
  results come back shuffled, partial, duplicated, without their searchResultId, or with requestIds reordered;
- exports have no duplicate emails, no invalid emails, nothing on the do-not-pull list, and no spreadsheet formulas;
- the monthly budget is never exceeded; the 60-calls/min limit is never exceeded;
- if Seamless times out or errors **after** a research request (so it may have charged), the tool pauses and
  never resends on its own.

The tests were also checked by deliberately breaking the tool 17 ways (matching by order, skipping the re-check,
ignoring the cooldown, retrying research, loose fill-in matching, …): every real breakage makes a test fail.
Run: `python -m pytest -q` (≈15 s). Run the chaos tests with other random seeds: `FAKE_SEED=7 python -m pytest -q`.

## Facts from Seamless's docs the tool relies on
- Search: 1 credit per 10 rows returned. Research: 1 credit per person submitted. Polling, location lookups
  and reading your org's saved contacts are free.
- Live balance comes back in the `X-PublicAPI-Credits` header on every call; the app shows it.
- `duplicate` / `contact-already-researched`: someone in your Seamless org researched that person before;
  Seamless's de-duplication normally doesn't charge them, and doesn't return their details. The ledger books
  them as 1 at submit and credits it back when the answer comes. The live balance is always the source of truth.
- Fill-in from saved contacts matches only on the exact LinkedIn profile (with one consistent email), or exact
  name + company when exactly one saved contact has it. Anything less certain is left blank.

## Before the first real pull (things only real data can confirm)
1. Run a pull of ~30 people and compare the Credits page with Seamless → Settings → Billing.
2. Open the AnavMail CSV and spot-check 5 people against their LinkedIn.
3. If anyone shows "Already researched before", try Fill-in and spot-check those too.

## Run it on the server (Docker)
```bash
cp .env.example .env        # fill in APP_PASSWORD, SEAMLESS_API_KEY, ANTHROPIC_API_KEY, MONTHLY_CREDIT_BUDGET
docker compose up -d --build
```
Put HTTPS in front (Caddy: `leads.yourdomain.com { reverse_proxy 127.0.0.1:8090 }`).
Data lives in `./data/leadpuller.db` — back it up: it holds history, the already-pulled people, the company
memory and the do-not-pull list. Run **one** app process only (the job runner lives inside it).

## Try it with fake data (no credits)
```bash
python -m tests.demo_server     # http://localhost:8765, password demo-pass-1
```

## Code map
- `app/config.py` — every setting, read from `.env`
- `app/filters.py` — Seamless search filters and allowed values
- `app/seamless.py` — API client: rate limiter, safe retries, never auto-resends research, balance header
- `app/claude.py` — request → filters, fit rating (matched by id)
- `app/worker.py` — preview, collect, research, company memory, search memory, fill-in, background loop
- `app/main.py` — login, REST API, exports, presets, companies, do-not-pull list
- `app/static/index.html` — the whole UI (no build step)
- `tests/` — awkward fake Seamless/Claude, money tests, demo server

Not in this version: pushing straight into AnavMail campaigns, reply-likelihood scoring, a daily automatic pull.
