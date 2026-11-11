# Lead Puller 2.6.0

Pulls contacts in bulk from Seamless.AI for AnavMail, using Seamless's own filters. The tool's main job is to
**never spend a credit it doesn't have to**. It is a fresh build that replaces Lead Puller 1.x. Your existing
memory of everyone already paid for is copied over automatically (see "Moving from 1.x").

One container, one process, one SQLite file. FastAPI + a background pull runner + a single-page UI.

## New in 2.6: fewer MillionVerifier checks, same Seamless credits

Email guessing now spends fewer MillionVerifier checks. Seamless credits, emails found and guessed emails are
unchanged: the same people are guessed, in the same way, only with fewer checks.

- **The made-up address is checked only after a valid guess.** To spot a company whose server says "valid" to every
  address, a made-up address (`zq...x@company`) was checked at every new company. Now it's checked only when a guess
  there comes back valid. A company that rejects a guess can't be such a server, so the check isn't needed there.
  Such a company is still never guessed (tested).
- **The common formats are tried in your companies' order.** At a new company, first.last@, firstlast@ and first_last@
  are tried in the order most often right at the companies you already know (same three formats, new order), once at
  least `GUESS_PROBE_LEARN_MIN` (20) companies are known.
- **`GUESS_PROBE_PEOPLE`** (2): how many people at one new company get the common formats tried (1 - 3; it was
  fixed at 2).
- **The pull page** shows "N MillionVerifier checks per Seamless credit saved" under Seamless credits saved.
- **AnavClouds branding:** the AnavClouds logo in the side panel and on the login page, the browser tab icon, the
  AnavClouds blue, and the version under the name ("Version 2.6.0").

Measured on the same test data as 2.5.1 (5 data sets x 3 kinds of pull): Seamless research credits and emails
identical, MillionVerifier checks 13 - 16% lower. Most of the checks left are guesses at companies with a format
that can't be guessed; cutting those would mean paying Seamless for people a guess would have found, so they stay.

### Upgrading to 2.6 (from 2.5 or 2.5.1)

Replace the code, keep `data/` and `.env`. No database change; no `.env` change needed (the two new settings have
defaults; add them to `.env` only to change them):

```bash
cd lead-puller-2 && docker compose down
cp -r data ../lead-puller-2-data-backup            # backup first
# unzip lead-puller-2.6.0.zip over this folder (it never contains data/ or .env)
docker compose up -d --build
curl -s http://127.0.0.1:8090/healthz      # {"ok":true,"version":"2.6.0"}
```

Tested by opening a 2.5.1 database with 2.6: every table is unchanged (only a new login is added), every pull opens,
and a new pull reuses the email formats already learned.

## 2.5.1 fix

On a company list, "people Seamless showed" also counted people found by Icypeas's search, which made it look
as if Seamless was skipped. It now counts only Seamless's people, shows "+ N found by Icypeas's search" under it,
and the finish message says how many new people Seamless had before Icypeas's search was used. Display only: how
pulls run is unchanged.

Companies already pulled in the last `COMPANY_SKIP_DAYS` days can now be included again with one tick: **Include them
anyway** under the company list (ticked automatically when you use "Change filters" on a pull). Use it to search the
same companies with new job titles; only new people are pulled and nobody is paid for twice.

## New in 2.5: Icypeas replaces Prospeo (Seamless first, Icypeas second)

Prospeo is gone from the app. **Icypeas** now does everything Prospeo did, at a lower cost:

- **Seamless misses.** When Seamless has no usable email for a person (not found, no email, an invalid one or a
  duplicate), Lead Puller asks Icypeas in the background while the pull carries on. Icypeas answers lookups a little
  later (usually seconds to minutes), so the pull waits up to `ICYPEAS_WAIT_MINUTES` (20) for the last answers; anything
  later still fills in on its own for `ICYPEAS_LATE_DAYS` (3) days. Never for a pull you stopped.
- **Only safe emails are used:** Icypeas rates every email `ultra sure`, `very sure` (both under 1% bounce) or
  `probable` (under 5%). Only the first two are used, unless you set `ICYPEAS_ACCEPT_PROBABLE=1`. An email at another
  company's website is thrown away. Answers are matched to people by Lead Puller's own id, never by position.
- **People Seamless doesn't have (company lists).** When Seamless runs out of people at your companies, Icypeas's own
  people database is searched with the same job titles, exclusions and countries, never more per company than you
  asked for, and never anyone already paid for. `ICYPEAS_SEARCH=1` (0 = off). Filter pulls never use the search.
- **Enrich contacts** uses Icypeas instead of Prospeo (after the free checks and Seamless).
- **Cost (Icypeas):** 1 credit per email found (Icypeas may also charge for some misses: these are counted), 0.02 per
  person a people search returns. All of it counts towards `ICYPEAS_MAX_CREDITS_PER_PULL` (500); below
  `ICYPEAS_MIN_CREDITS_LEFT` (20) Icypeas isn't asked. Icypeas credits are logged apart from Seamless's.
- **Errors never become emails.** A failure, timeout or unreadable answer leaves the person to be asked again;
  3 errors in a row (`ICYPEAS_ERRORS_PAUSE`) pause Icypeas for 30 minutes; "too many requests" is only a wait.
  An unpaid Icypeas plan (the API needs a paid plan) is shown on the Accounts page in plain words.
- **Accounts page:** Icypeas API key (checked with a free call, a wrong key is never saved), the email you log in to
  Icypeas with (used to show your credits), on/off and people-search switches, and "Icypeas's last answers (for your
  developer)": the raw shape of the last answers, in case Icypeas changes its format.
- **Companies already pulled are left out of new company lists.** A company that was in a company-list pull in the
  last `COMPANY_SKIP_DAYS` (90) days shows as "already pulled, left out" when you check a new list, and is not searched
  again (0 = off). "Run again" on a pull is not affected (it only looks for new people there). People already paid for
  were never paid for twice, and still aren't.
- **Old data is kept.** Prospeo's tables, logs and emails stay in the database; old Prospeo emails still download,
  marked `prospeo (verified, before 2.5)`.

### Upgrading to 2.5 (from 2.1 - 2.4.1)

Replace the code, keep `data/` and `.env`. Nothing is redone from scratch:

```bash
cd lead-puller-2 && docker compose down
cp -r data ../lead-puller-2-data-backup            # backup first
# unzip lead-puller-2.5.1.zip over this folder (it never contains data/ or .env)
docker compose up -d --build
curl -s http://127.0.0.1:8090/healthz      # {"ok":true,"version":"2.5.1"}
```

New tables and columns are added on start (tested on a 2.4.1 database with Prospeo data: every pull, person, credit
and setting is kept). No `.env` change is needed; the `PROSPEO_*` and `SOURCE_ORDER` lines can be deleted (they are
ignored). Optionally add `ICYPEAS_API_KEY=` and `ICYPEAS_ACCOUNT_EMAIL=` to `.env`, or add both on the Accounts page.

## Before 2.5

2.3 - 2.4.1 used Prospeo as the second source (email lookups, people search, Enrich contacts); 2.5 replaces it with
Icypeas and keeps everything else those versions added (Enrich contacts, the company list improvements).

## New in 2.2 (fewer Seamless credits per email on company lists)

- **No more paying for the first person at every new company.** In 2.1, the first person at a company was
  always paid for in Seamless to learn its email format. Now Lead Puller first tries the common full-name
  formats (`john.smith@`, `johnsmith@`, `john_smith@`) with MillionVerifier. If one is "valid", that person
  costs no Seamless credit and the format is learned for everyone else there. If none is valid, the person is
  paid for as before. Each company is tried at most twice (`GUESS_PROBE`, `GUESS_PROBE_FORMATS`).
- **Made-up address check.** Before trying formats at a company, one made-up address (like
  `zq3f9a1c2bx@company.com`) is checked, once per company ever. If even that comes back "valid", the
  company's server says yes to anything, so nothing there is guessed. An "unknown" answer also means no guess.
- **Big companies are never guessed** (more people share a name there): over 5,000 employees by default
  (`GUESS_MAX_COMPANY_SIZE`, 0 = no limit).
- **Full companies leave the search sooner.** As soon as leaving them out costs fewer search credits than it
  saves, the other companies are searched again on their own (`RESLICE_SMART`, `RESLICE_MAX_TIMES`). This
  matters most when a few big companies fill the first pages: 2.1 gave up there, 2.2 reaches the others.
- **Only people with a usable email count towards "full".** If someone at a company comes back with no email,
  one more person there may be paid for (`EXTRA_TRIES_PER_COMPANY=1`; 0 = never, as in 2.1). Companies left out
  of the search come back once, all together, after every answer is in.
- **Free vs paid report.** The pull page shows how many emails were guessed (no Seamless credit) and how many
  of those came from trying the common formats. The download's "How we got the email" column says
  "seamless", "guessed" or "guessed: common format tried".
- **People per company now defaults to 2** (`DEFAULT_PER_COMPANY`), and a "People at my companies" pull with
  no job titles or seniority asks before starting.
- The first-batch email-rate pause now waits for at least 10 people paid for (guessing can make the first
  batch tiny).

### Upgrading from 2.1

Same as every upgrade: replace the code, keep `data/` and `.env`:

```bash
cd lead-puller-2 && docker compose down
cp -r data ../lead-puller-2-data-backup            # backup first
# unzip lead-puller-2.2.0.zip over this folder (it never contains data/ or .env)
docker compose up -d --build
curl -s http://127.0.0.1:8090/healthz      # {"ok":true,"version":"2.2.0"}
```

The database gets its new columns on start. No `.env` change is needed: every new setting has a default, and
all of them are in `.env.example` (marked "2.2"). To get 2.1's behaviour back for any part, set it to 0:
`GUESS_PROBE=0`, `RESLICE_SMART=0`, `EXTRA_TRIES_PER_COMPANY=0`, `GUESS_MAX_COMPANY_SIZE=0`.
Rollback: unzip 2.1.0 over the folder and `docker compose up -d --build` (the new columns are simply ignored).

## New in 2.1

- **People at my companies.** On New pull, switch "Who to pull" to "People at my companies". Then paste
  websites or upload a CSV (any file with a website/domain column, e.g. a Hiring Signal Finder or Funding
  Finder download) and choose people per company (default 3, most senior first). Lead Puller searches those
  companies in batches of 100 and pays only for people at them. Companies that already have enough people
  are left out of later searches. The file's signal column goes into the download.
- **Email guessing (saves Seamless research credits).** Lead Puller learns each company's email format from
  real emails (earlier pulls, Seamless saved contacts, emails shown in search results). For the next person
  there, it builds the address and checks it with MillionVerifier. Only a "valid" answer is used, with no
  Seamless credit spent; anything else is paid for in Seamless as before. At a new company, one person is
  paid for first to learn the format. Only formats with the full first and last name are guessed
  (`john.smith@`, `johnsmith@`, `john_smith@`…). `john@` and `jsmith@` could belong to someone else, so
  people at those companies are paid for in Seamless as before (`GUESS_INITIAL_FORMATS=1` turns on a guarded
  version for initials). It also works on normal pulls wherever a company's format is already known.
- The **MillionVerifier key goes on the Accounts page** (like the Seamless keys; the AnavMail key works).
  With no key, guessing is simply off and 2.1 behaves exactly like 2.0.
- Fix: when Seamless returns an answer without saying who it is for, it is now matched by name **and**
  company (two people with the same name at two companies could swap emails before).

### Upgrading from 2.0

Replace the code, keep `data/` and `.env`:

```bash
cd lead-puller-2 && docker compose down
cp -r data ../lead-puller-2-data-backup            # backup first
# unzip lead-puller-2.1.0.zip over this folder (it never contains data/ or .env)
docker compose up -d --build
curl -s http://127.0.0.1:8090/healthz      # {"ok":true,"version":"2.1.0"}
```

The database gets its new columns automatically on start. No `.env` change is needed: every new setting has a
safe default (all of them are listed under "Email guessing" and "Company lists" in `.env.example`). Then add the
MillionVerifier key on the Accounts page.

## Deploy (fresh install)

```bash
unzip lead-puller-2.1.0.zip && cd lead-puller-2
cp .env.example .env          # then edit .env (see below)
mkdir -p data
docker compose up -d --build
curl -s http://127.0.0.1:8090/healthz      # {"ok":true,"version":"2.1.0"}
```

The app listens on `127.0.0.1:8090`. Put Nginx/Caddy with HTTPS in front, exactly like 1.x. Run **one**
worker only: the pull runner lives inside the web process, so `--workers 1` must stay.

### What to set in `.env`

Every setting is in `.env`, with comments in `.env.example`. The only ones you must set are these:

| Setting | Why |
|---|---|
| `APP_PASSWORD` | Login password for the UI. |
| `COOKIE_SECURE=1` | Keep at 1 behind HTTPS (set 0 only for plain-http local testing). |
| `ANTHROPIC_API_KEY` | Optional. Powers the "describe who you want" box. Leave it empty to turn that off. |
| `MONTHLY_CREDIT_BUDGET` | Total Seamless credits all accounts may spend per calendar month (default 9500). |

**Seamless API keys are NOT in `.env`.** They are added on the **Accounts** page in the UI, which takes two
or more accounts. If an old `SEAMLESS_API_KEY` is still set in `.env`, it shows up as an account
automatically, but the Accounts page is the place to manage keys.

After changing `.env`: `docker compose up -d` (it restarts with the new values).

## Moving from 1.x

1. Stop 1.x: `cd <old folder> && docker compose down`. This also makes its database file complete.
2. Unzip 2.0 into a **new** folder. Copy the values you had set in the old `.env` (password, Anthropic
   key, budget) into the new `.env`.
3. Copy the old database into the new data folder:
   `cp <old folder>/data/leadpuller.db lead-puller-2/data/`
4. `docker compose up -d --build` in the new folder.

On the first start, 2.0 reads `data/leadpuller.db` (read-only) and copies over everyone already researched,
every downloaded email, the do-not-pull list and the Seamless accounts. 2.0 never pays for any of them again.
The Settings page shows what was copied. The import runs once. 2.0 keeps its own data in
`data/leadpuller2.db`, and the old file is left untouched. Set `IMPORT_OLD_DATABASE=0` to skip the import.

On top of that, 2.0 also reads each Seamless account's saved contacts for free. That means people already
researched on the Seamless website (or by anyone else on that account) are skipped too.

Rollback: stop 2.0 and start the old folder again. 1.x's database was never changed.

## How credits are saved

Seamless charges 1 credit per 10 search rows and 1 credit per person researched (the research call is the one
that gets the email). Here is how Lead Puller avoids paying:

- **Shared memory across accounts.** Everyone researched with any key is remembered by email, LinkedIn slug
  and name+company. Nobody found with one Seamless account is ever paid for with another account. People
  already in memory are skipped automatically and never stop the pull.
- **Free sources first.** Seamless saved contacts (free to read) and emails already shown in search results
  are checked against memory before anything is paid for.
- **Searching is paid, so search less.** The pull scans in company-size slices and remembers each slice's
  position per account. A re-run continues where the last one stopped. Once a slice is fully read, only
  contacts updated since then are fetched. A slice that keeps returning old people is left (`MOVE_ON_*`).
  Page size fits what is still needed. People found but not needed go to a "bank" and are used first by the
  next pull (`BANK_DAYS`).
- **Research only what's needed.** The pull researches the number needed to reach the target at the
  observed email hit rate, starting with a small first batch (`FIRST_BATCH`). It pauses if the email rate is
  poor (`PAUSE_BELOW_EMAIL_RATE`). Right before paying, it re-checks memory and the do-not-pull list.
- **Skip people unlikely to pay off:** no company website, a domain with no mail server (free DNS check),
  more than `MAX_PER_COMPANY` people at one company, and anyone on do-not-pull (email, domain including
  subdomains, LinkedIn, company).
- **Email guessing before research** (2.1, needs the MillionVerifier key). See "New in 2.1". Each
  MillionVerifier answer is reused for `GUESS_CACHE_DAYS`, so the same address is never paid for twice.
  Catch-all companies cost at most one check and are then never guessed. A company whose guesses keep
  failing is given up (`GUESS_STOP_BELOW_RATE`). MillionVerifier errors, timeouts or a low balance
  (`GUESS_MIN_CHECKS_LEFT`) just mean "pay Seamless as usual".
- **Hard stops:** max credits per pull, monthly budgets (overall and per account) and `ACCOUNT_RESERVE`
  credits left on each account. `SPENDING_ENABLED=0` is the kill switch.
- **No blind retries.** If Seamless's answer to a research call is lost, the tool checks the balance (free)
  to see whether it was charged before doing anything else. Duplicates Seamless doesn't charge for are
  credited back in the ledger.

Every credit is in the ledger (Accounts page). The tests check, to the credit, that the ledger equals what
Seamless charged each key.

## Tests

```bash
pip install -r requirements.txt
python -m pytest -q                       # 157 tests, ~90 s, against fake Seamless, MillionVerifier and Icypeas
FAKE_SEED=7 python -m pytest -q           # same tests on a differently shuffled fake
LP_TEST_GUESSING=1 python -m pytest -q    # every test again with email guessing switched on
LP_TEST_ICYPEAS=1 python -m pytest -q     # every test again with Icypeas switched on
```

Or inside the container: `docker compose exec leadpuller python -m pytest -q`.

## Files

- `app/engine.py`: the pull runner (search, skip, research, answers, all credit rules).
- `app/seamless.py`: the Seamless API client (rate limit, safe retries, ledger).
- `app/memory.py`: memory, do-not-pull, free saved-contacts sync.
- `app/guess.py`: email formats, the guessing rules. `app/verifier.py`: the MillionVerifier client.
- `app/companies.py`: company lists (reading files, checking, per-company report).
- `app/importer.py`: the one-time 1.x import.
- `app/config.py`: every `.env` setting with its default and help text.
- `app/static/index.html`: the whole UI.
- `data/leadpuller2.db`: all state. Back up this file.
