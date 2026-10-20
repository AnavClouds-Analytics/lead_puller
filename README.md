# Lead Puller 2.0

Pulls contacts in bulk from Seamless.AI for AnavMail, using Seamless's own filters. The tool's main job is to
**never spend a credit it doesn't have to**. It is a fresh build that replaces Lead Puller 1.x. Your existing
memory of everyone already paid for is copied over automatically (see "Moving from 1.x").

One container, one process, one SQLite file. FastAPI + a background pull runner + a single-page UI.

## Deploy (fresh install)

```bash
unzip lead-puller-2.0.0.zip && cd lead-puller-2
cp .env.example .env          # then edit .env (see below)
mkdir -p data
docker compose up -d --build
curl -s http://127.0.0.1:8090/healthz      # {"ok":true,"version":"2.0.0"}
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
python -m pytest -q                       # 49 tests, ~10 s, against a fake Seamless (no real credits)
FAKE_SEED=7 python -m pytest -q           # same tests on a differently shuffled fake
```

Or inside the container: `docker compose exec leadpuller python -m pytest -q`.

## Files

- `app/engine.py`: the pull runner (search, skip, research, answers, all credit rules).
- `app/seamless.py`: the Seamless API client (rate limit, safe retries, ledger).
- `app/memory.py`: memory, do-not-pull, free saved-contacts sync.
- `app/importer.py`: the one-time 1.x import.
- `app/config.py`: every `.env` setting with its default and help text.
- `app/static/index.html`: the whole UI.
- `data/leadpuller2.db`: all state. Back up this file.
