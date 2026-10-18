# Lead Puller (AnavClouds × Seamless.AI) — v1.3.0

Type who you want in plain English → Claude turns it into Seamless filters → preview a small sample →
collect the list → choose who to research → download AnavMail batches with the columns you pick.

**Every setting and on/off switch lives in `.env`.** To change one, edit that line and run
`docker compose up -d`. The Settings page shows the current values (secrets masked) but can't change them.
The one exception: **Seamless API keys are added on the "Seamless accounts" page** (one per Seamless user), each with
its own optional monthly budget.
**Updating the code:** replace the code folder, keep `.env` and `data/`, run `docker compose up -d --build`.
The database upgrades itself on start and keeps all history.

**From v1.2 to v1.3:** replace the code, keep `.env` and `data/`, then add the new lines from `.env.example` to `.env`
(anything left out uses the default shown there). Your existing `SEAMLESS_API_KEY` becomes the first account on the
"Seamless accounts" page, with all its history; add the second user's key there. `SEARCH_SPEND_GUARD` is no longer used
(replaced by the `COLLECT_*` settings). A pull that was stuck waiting on Seamless finishes by itself after the upgrade.

## How a pull works

| Step | What happens | Seamless credits |
|---|---|---|
| 1. Filters | Claude fills the form and lists every assumption. Edit the request itself and **Rebuild filters from this** (your hand edits are listed first), or adjust in words. Lists are Seamless's own values; places are looked up in Seamless's place list as you type (free). Location defaults to **person's location only**. Pick the Seamless account, or leave it on Automatic. | 0 |
| 2. Preview | One small search page. Places (countries, states, cities) are checked against Seamless's place list first (free). Same filters as before → continues where the last run stopped. | ~3 |
| 3. Collect | **Cost estimate first** (from the preview's keep rate), which you confirm. Pages until the target, applying every skip rule (below). Finishes when within 1% of the target; stops after 3 pages that keep nobody; pauses and asks when the last 3 pages kept under 20%, or when search credits reach 150% of the estimate. Stop at any time and keep what's collected. | 1 per 10 results returned (skipped ones count) |
| 4. Choose | Everyone / good + maybe fit / good only / **email chance ≥ X%** / first N, plus tick boxes (page by page, or "all matching, on every page"). The button shows the exact number and credits, counted on the server with the same rule research uses; if the list changed, research refuses to start. | 0 |
| 5. Research | **Test batch** first (default 100): if fewer than 60% have an email, it pauses: *Continue anyway* or *Stop*. Then batches of 100. Every person and company is re-checked right before paying. **Downloads already work while it runs.** Waits up to `RESEARCH_WAIT_MINUTES` for slow answers, or press **Finish now**; late answers are filled in free for `LATE_ANSWER_HOURS`. | 1 per person |
| 6. Results | **Download next batch** (any number, columns you choose, one file or one per country), past batches, people without email, everything Seamless returned, charge check, free fill-in. **Edit filters (new copy)** starts a new pull from the same filters; people already paid for stay skipped. | 0 |

**Edit filters** is on every screen. Before research it goes back to the filters and clears the collected list (search
credits already spent aren't refunded). After research it opens a copy.

**Every long list is split into pages** (pulls, sample, collected list, results, skipped people, batches, companies,
do-not-pull list, presets, spending history), 50 rows by default (`TABLE_PAGE_SIZE`, changeable on screen). Search and
sort cover every page; your page and search are kept while the screen refreshes.

**Skip report:** after Preview, while collecting and on the results, "Why people were skipped" shows each reason with
the people and search credits it cost and what to change. Click a reason to see who was skipped.

## Two (or more) Seamless accounts
- Add each user's API key on **Seamless accounts** (checked with a free call; only the last 4 characters are shown).
  Put them in order, switch them on/off, give each a monthly budget.
- A pull uses **one account from start to finish** (Seamless's search IDs and research requests belong to the key that
  made them), and polls answers with that same key.
- New pulls take the first account that has credits to spare (balance above `ACCOUNT_RESERVE`, within its own budget and
  `MONTHLY_CREDIT_BUDGET`). When a running pull's account runs low, it pauses with **Continue with <other account>**:
  everyone already researched stays, and a new pull with the same filters continues on the other account.
- **One memory for every account:** anyone researched, downloaded, or waiting in another open pull, with either key, is
  skipped by every other account, so nobody is shown or paid for twice. Company cooldown, blocked companies, the
  do-not-pull list and "never the same email twice" in downloads are shared too. Fill-in reads every account's saved contacts.
- **Shared pool check:** Seamless's docs say credits belong to the whole organization. If two keys show exactly the same
  balance, the tool warns and counts it once. Ask Seamless whether your users have separate pools.
- **Balance check:** per account, Seamless's balance change this month (refills excluded) is compared with what this tool
  spent. A gap means credits used outside the tool (Seamless website, Chrome extension, another user on the same pool).

## Skip rules (all before any research credit is spent)
Cheap checks first, network check last:
1. No company website (if ticked) · no LinkedIn (if ticked) · title doesn't contain your words (whole words: "CTO" never matches "Director")
2. Skip title words, company words, industries, domains, countries, states, seniority · time in current role
3. Duplicate inside the pull · **blocked company** (name or domain, incl. subdomains) · on the do-not-pull list
4. **People memory (all accounts):** anyone ever researched, with any Seamless key, is skipped forever (people with no email: only retried if `NO_EMAIL_RETRY_DAYS` > 0, paid, with a free saved-contacts check first)
5. **In another open pull:** people collected (not yet researched) in another open pull, on any account, for `OPEN_PULL_HOLD_DAYS`
6. **Company cooldown:** companies where someone *got an email* in the last 90 days. Companies where nobody got an email stay available.
7. Max people per company · **company domain can't receive email** (free DNS check; a timeout never skips anyone)

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
- **Waiting on Seamless:** "Finish now without them" when only slow answers are left (late answers still fill in free).
- **Collect paused (low keep rate / spend cap):** "Keep collecting", "Stop and keep what's collected", "Change the filters".
- **Account ran low:** "Continue with <other account>", or "Stop".
- **Fill-in from saved contacts:** "Stop" (it's free; stopping just stops reading more pages).
- **Sidebar: "Emergency stop: all pulls"** stops every running pull at once.
- **`SPENDING_ENABLED=0` in `.env`:** nothing is searched or researched; running pulls pause (results already paid for still arrive).

## Credits
- **By account:** balance, spent this month, what it can still spend, and Seamless's own count next to the tool's.
- **Spending history:** every search page and research call, by account, page by page.
- **Charge check** (results screen): the live balance before and after research tells you whether Seamless charged for people
  with no email ("charged for every person sent" / "did NOT charge for the N with no email").
- **Cost per email by country** (Credits page), across all pulls.
- **Email chance:** learned from your own past pulls. Always at least the same country (then size and LinkedIn where
  there's enough history), with a note like "based on 412 United States people". Without `EMAIL_CHANCE_MIN_SAMPLES`
  researched people of that country it shows "—"; it never borrows the average of other countries.
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
`python -m pytest -q` (≈65 s, 93 tests) runs every scenario against a deliberately awkward fake Seamless that charges exactly as the docs say
and keeps its own books. It sends shuffled, partial and duplicated answers, rows without IDs, reordered request IDs, 429s, 500s,
timeouts after charging, expired positions, short pages. After each scenario the tests check:
- the tool's credit ledger equals what "Seamless" charged, to the credit;
- nobody is charged twice: across pulls, two pulls at once, restarts, pause/stop, test batches, stop-all, spending switch;
- every email and phone is attached to the right person, including when picking between 3 emails;
- across **all** batches no email appears twice; no invalid emails; columns, risky toggle and blocked companies respected;
- budget and rate limit never exceeded; a research call that may have charged is never resent automatically;
- a v1.1 and a v1.2 database upgrade with all data and memory intact (including a pull stuck waiting on Seamless);
- with two accounts: each account's ledger equals what that account was charged; one account's search results are never
  sent to the other; people found with one key never appear with the other; budgets and reserves per account hold;
- collecting: the estimate, close-enough finish, empty-page stop, low-keep pause and spend cap each work and resume correctly;
- select-all on every page and the count on the button always equal what is sent and charged.

Checked by deliberately breaking the tool in 54 money-relevant ways (32 from v1.2, 22 new in v1.3): every breakage makes a test fail.
Other random seeds: `FAKE_SEED=7 python -m pytest -q`.

## Before the first real pull (things only real data can confirm)
1. Pull ~30 people; compare the Credits page with Seamless → Settings → Billing, and read the charge check.
2. Spot-check 5 people in the batch file against LinkedIn.
3. Try one "Seamless saved search" ID and one city filter, to confirm the new API fields on your plan.
4. Add your second Seamless key and press "Check balance" on both. If both show the same number, it's one shared pool.

## Run it on the server (Docker)
```bash
cp .env.example .env        # fill in APP_PASSWORD, ANTHROPIC_API_KEY, MONTHLY_CREDIT_BUDGET
                            # (then add the Seamless keys on the "Seamless accounts" page)
docker compose up -d --build
```
Put HTTPS in front (Caddy: `leads.yourdomain.com { reverse_proxy 127.0.0.1:8090 }`). Back up `./data/leadpuller.db`:
it holds history, people and company memory, batches and the do-not-pull list. Run **one** app process only.
The free email-server check needs outbound DNS from the server (normal on any VPS).

## Try it with fake data (no credits)
```bash
python -m tests.demo_server     # http://localhost:8765, password demo-pass-1
                                # second fake Seamless key for the accounts page: demo-second-key-2222
```

## Code map
- `app/config.py`: every setting, read from `.env`
- `app/filters.py`: Seamless filters, native excludes, saved search
- `app/accounts.py`: Seamless accounts: picking, budgets, reserve, shared-pool and balance checks
- `app/seamless.py`: API client (per account): rate limiter, safe retries, never auto-resends research, balance header
- `app/claude.py`: request → filters, fit rating (works with models that refuse forced tool use)
- `app/worker.py`: skip rules, people/company memory, preview, collect, test batch, research, retries, fill-in, stop
- `app/dnscheck.py`: free email-server (MX) check, cached 30 days
- `app/exports.py`: column catalog, batches, downloads
- `app/main.py`: login, REST API, presets, column sets, companies, do-not-pull list
- `app/static/index.html`: the whole UI (no build step)
- `tests/`: awkward fake Seamless/Claude, money tests, v1.2 tests, demo server
