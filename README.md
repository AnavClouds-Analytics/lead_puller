# Lead Puller (AnavClouds × Seamless.AI) — v1.2.0

Type who you want in plain English → Claude turns it into Seamless filters → preview a small sample →
collect the list → choose who to research → download AnavMail batches with the columns you pick.

**Every setting and on/off switch lives in `.env`.** To change one, edit that line and run
`docker compose up -d`. The Settings page shows the current values (secrets masked) but can't change them.
**Updating the code:** replace the code folder, keep `.env` and `data/`, run `docker compose up -d --build`.
The database upgrades itself on start and keeps all history.

## How a pull works

| Step | What happens | Seamless credits |
|---|---|---|
| 1. Filters | Claude fills the form and lists every assumption. Edit anything or adjust in words. New pulls start with the `.env` defaults (updated in last 12 months, must have a company website). | 0 |
| 2. Preview | One small search page. Places (countries, states, cities) are checked against Seamless's place list first (free). Same filters as before → continues where the last run stopped. | ~3 |
| 3. Collect | Pages until the target is reached, applying every skip rule (below). Stop at any time and keep what's collected. | 1 per 10 results returned (skipped ones count) |
| 4. Choose | Everyone / good + maybe fit / good only / **email chance ≥ X%** / first N, plus tick boxes. Budget and live balance checked first. | 0 |
| 5. Research | **Test batch** first (default 100): if fewer than 60% have an email, it pauses: *Continue anyway* or *Stop*. Then batches of 100. Every person and company is re-checked right before paying. | 1 per person |
| 6. Results | **Download next batch** (any number, columns you choose, one file or one per country), past batches, people without email, everything Seamless returned, charge check, free fill-in. | 0 |

## Skip rules (all before any research credit is spent)
Cheap checks first, network check last:
1. No company website (if ticked) · no LinkedIn (if ticked) · title doesn't contain your words (whole words: "CTO" never matches "Director")
2. Skip title words, company words, industries, domains, countries, states, seniority · time in current role
3. Duplicate inside the pull · **blocked company** (name or domain, incl. subdomains) · on the do-not-pull list
4. **People memory:** anyone ever researched is skipped forever (people with no email: only retried if `NO_EMAIL_RETRY_DAYS` > 0, paid, with a free saved-contacts check first)
5. **Company cooldown:** companies where someone *got an email* in the last 90 days. Companies where nobody got an email stay available.
6. Max people per company · **company domain can't receive email** (free DNS check; a timeout never skips anyone)

Seamless itself excludes (no credits): countries, states, cities, zip codes, SIC/NAICS codes marked "Seamless" on the filter screen.
Seamless's API cannot exclude companies, titles, seniority or industries, so those are skipped by this tool (small search cost, never research).

## Filters
Titles (+ exact match), seniority, department, profile keywords (all/any), countries, states, zip codes, **cities + radius**,
**time zones**, industries, **SIC / NAICS codes**, company size, revenue, technologies, specific companies/domains, public/private,
founded, funding round/date/total, company news, recent job change, **updated in last N months**, **used to work at**
(+ most recent job only), **title must contain**, **must have website / LinkedIn**, **time in role**, and every skip above.
**Seamless saved search:** build a search in the Seamless app (it has exclude on every filter), paste its ID; Seamless then uses
only that saved search, and this tool's own rules still apply.

## Downloads
- **AnavMail batches:** type how many → only people never downloaded before, from *any* pull. No email ever goes out twice.
  Invalid emails, do-not-pull entries and blocked companies are left out. "Risky" optional. One file or one file per country (zip).
  Each batch remembers its columns, so "Download again" gives the identical file.
- **One email per person:** `EMAIL_PICK=best` takes, from Seamless's up to 3 emails, the best status (valid > risky/unknown > invalid),
  then the highest confidence, then Seamless's primary. `EMAIL_PICK=seamless` always uses Seamless's primary.
- **Column picker:** tick the columns, order them with ↑ ↓, save the selection as a named set. The default comes from `DEFAULT_EXPORT_COLUMNS`.
  The email column is always in AnavMail batches.
- **People without email:** LinkedIn, phones, company, location: for LinkedIn outreach or calls.
- **Everything Seamless returned:** Excel or CSV, all 79 columns or the ones you choose.

All column names (use these in `DEFAULT_EXPORT_COLUMNS`):
`first_name middle_name last_name full_name title seniority department time_in_role time_at_company role_started email
email_status email_confidence email_picked_from seamless_primary_email email_1 email_1_status email_1_confidence email_2
email_2_status email_2_confidence email_3 email_3_status email_3_confidence personal_email phone phone_type phone_confidence
phone_2 phone_2_type company_phone company_phone_confidence company_phone_2 company_phone_3 linkedin sales_navigator
linkedin_recruiter company_linkedin city state country postcode time_zone local_time company domain company_description
industry sic naics employees company_size revenue_range revenue_estimate founded company_type stock_ticker hq_street hq_city
hq_state hq_postcode hq_country funding funding_total news job_change previous_company previous_title previous_start
previous_end previous_company_2 previous_title_2 fit fit_reason email_chance seamless_result result_note batch pull`

Every text cell is protected against spreadsheet formulas (a cell starting with `= + - @` gets a leading `'`).

## Stop buttons
- **Collecting:** "Stop collecting": keeps what's collected; nothing more is searched.
- **Test batch failed:** "Continue anyway" / "Stop: spend nothing more".
- **Research:** "Pause" and "Stop: send nobody else". People already sent (already paid for) still come back.
- **Fill-in from saved contacts:** "Stop" (it's free; stopping just stops reading more pages).
- **Sidebar: "Emergency stop: all pulls"** stops every running pull at once.
- **`SPENDING_ENABLED=0` in `.env`:** nothing is searched or researched; running pulls pause (results already paid for still arrive).

## Credits
- **Charge check** (results screen): the live balance before and after research tells you whether Seamless charged for people
  with no email ("charged for every person sent" / "did NOT charge for the N with no email").
- **Cost per email by country** (Credits page), across all pulls.
- **Email chance:** learned from your own past pulls (country, company size, LinkedIn, record age); needs
  `EMAIL_CHANCE_MIN_SAMPLES` researched people before it shows.
- **Low-balance banner** below `LOW_BALANCE_WARNING`.

## Facts from Seamless's docs the tool relies on
- Search: 1 credit per 10 rows returned. Research: 1 credit per person submitted. Polling, place lookups and reading your
  org's saved contacts are free. The live balance comes back in the `X-PublicAPI-Credits` header on every call.
- 60 requests/min per endpoint for the whole Seamless org; the tool stays at 55 and backs off on 429.
- "Already researched" (`duplicate`): Seamless doesn't return or normally charge them. `skipDeduplicationCheck` forces a fresh,
  charged look (used only for paid retries).
- Excludes with a `-` prefix work on contactCountry, contactState, contactZipCode, locations, industrySicCodes, industryNaicsCodes.
- `savedSearchId` runs a saved search and can't be combined with other Seamless filters.

## Money and data safety (what the tests prove)
`python -m pytest -q` (≈40 s, 63 tests) runs every scenario against a deliberately awkward fake Seamless that charges exactly as the docs say
and keeps its own books. It sends shuffled, partial and duplicated answers, rows without IDs, reordered request IDs, 429s, 500s,
timeouts after charging, expired positions, short pages. After each scenario the tests check:
- the tool's credit ledger equals what "Seamless" charged, to the credit;
- nobody is charged twice: across pulls, two pulls at once, restarts, pause/stop, test batches, stop-all, spending switch;
- every email and phone is attached to the right person, including when picking between 3 emails;
- across **all** batches no email appears twice; no invalid emails; columns, risky toggle and blocked companies respected;
- budget and rate limit never exceeded; a research call that may have charged is never resent automatically;
- a v1.1 database upgrades with all data and memory intact.

Checked by deliberately breaking the tool in 31 money-relevant ways: every breakage makes a test fail.
Other random seeds: `FAKE_SEED=7 python -m pytest -q`.

## Before the first real pull (things only real data can confirm)
1. Pull ~30 people; compare the Credits page with Seamless → Settings → Billing, and read the charge check.
2. Spot-check 5 people in the batch file against LinkedIn.
3. Try one "Seamless saved search" ID and one city filter, to confirm the new API fields on your plan.

## Run it on the server (Docker)
```bash
cp .env.example .env        # fill in APP_PASSWORD, SEAMLESS_API_KEY, ANTHROPIC_API_KEY, MONTHLY_CREDIT_BUDGET
docker compose up -d --build
```
Put HTTPS in front (Caddy: `leads.yourdomain.com { reverse_proxy 127.0.0.1:8090 }`). Back up `./data/leadpuller.db`:
it holds history, people and company memory, batches and the do-not-pull list. Run **one** app process only.
The free email-server check needs outbound DNS from the server (normal on any VPS).

## Try it with fake data (no credits)
```bash
python -m tests.demo_server     # http://localhost:8765, password demo-pass-1
```

## Code map
- `app/config.py`: every setting, read from `.env`
- `app/filters.py`: Seamless filters, native excludes, saved search
- `app/seamless.py`: API client: rate limiter, safe retries, never auto-resends research, balance header
- `app/claude.py`: request → filters, fit rating (works with models that refuse forced tool use)
- `app/worker.py`: skip rules, people/company memory, preview, collect, test batch, research, retries, fill-in, stop
- `app/dnscheck.py`: free email-server (MX) check, cached 30 days
- `app/exports.py`: column catalog, batches, downloads
- `app/main.py`: login, REST API, presets, column sets, companies, do-not-pull list
- `app/static/index.html`: the whole UI (no build step)
- `tests/`: awkward fake Seamless/Claude, money tests, v1.2 tests, demo server
