# Lead Puller 2.2

Pulls contacts in bulk from Seamless.AI for AnavMail, using Seamless's own filters. The tool's main job is to
**never spend a credit it doesn't have to**. It is a fresh build that replaces Lead Puller 1.x. Your existing
memory of everyone already paid for is copied over automatically (see "Moving from 1.x").

One container, one process, one SQLite file. FastAPI + a background pull runner + a single-page UI.

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
python -m pytest -q                       # 107 tests, ~15 s, against a fake Seamless + fake MillionVerifier
FAKE_SEED=7 python -m pytest -q           # same tests on a differently shuffled fake
LP_TEST_GUESSING=1 python -m pytest -q    # every test again with email guessing switched on
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
