"""Seamless accounts (API keys). Added on the "Seamless accounts" page; the SEAMLESS_API_KEY in .env, if set,
is kept as one more account.

Rules:
  * One account per pull. Search result IDs and research requests belong to the account that made them, so a
    pull searches, researches and polls with the same account from start to finish.
  * New pulls take the first enabled account (in your order) that has enough credits above ACCOUNT_RESERVE and
    room in its own monthly budget. A pull whose account runs low pauses; "Continue with <other account>" moves
    the people not yet researched to a new pull on the other account.
  * People, company and search memory is shared by every account, so nobody is paid for twice whichever key
    found them.
"""
import time

from . import db
from .config import cfg


class NoAccount(Exception):
    pass


def mask(key):
    return ("•••• " + key[-4:]) if key else ""


def all_accounts():
    return db.q("SELECT * FROM accounts ORDER BY priority, id")


def enabled():
    return [a for a in all_accounts() if a["enabled"] and a["api_key"]]


def get(account_id):
    return db.q1("SELECT * FROM accounts WHERE id=?", (account_id,)) if account_id else None


def budget_left(a):
    """Credits this account may still spend this month (None = no limit), counting the overall budget too."""
    lefts = []
    if a and a.get("monthly_budget"):
        lefts.append(max(0, a["monthly_budget"] - db.month_spend(a["id"])))
    if cfg.MONTHLY_CREDIT_BUDGET:
        lefts.append(max(0, cfg.MONTHLY_CREDIT_BUDGET - db.month_spend()))
    return min(lefts) if lefts else None


def usable(a):
    """Credits that can be spent on this account right now (None = unknown balance and no budget)."""
    if not a or not a["enabled"] or not a["api_key"]:
        return 0
    vals = []
    if a.get("balance") is not None:
        vals.append(max(0, a["balance"] - cfg.ACCOUNT_RESERVE))
    b = budget_left(a)
    if b is not None:
        vals.append(b)
    return min(vals) if vals else None


def pick(need=1, exclude=()):
    """First enabled account (in your order) that can spend `need` credits."""
    for a in enabled():
        if a["id"] in exclude:
            continue
        u = usable(a)
        if u is None or u >= need:
            return a
    return None


def for_pull(pull, need=1):
    """The pull's own account. Picks one (and saves it) for a pull that has none yet."""
    if pull.get("account_id"):
        a = get(pull["account_id"])
        if not a:
            raise NoAccount("The Seamless account this pull used was deleted. Use “Run again” to start a new pull.")
        return a
    choice = pull.get("account_choice") or "auto"
    if choice != "auto":
        a = get(int(choice))
        if not a or not a["enabled"]:
            raise NoAccount("The Seamless account chosen for this pull is switched off or deleted. Pick another one.")
    else:
        a = pick(need)
        if not a:
            raise NoAccount(no_account_message(need))
    db.update_pull(pull["id"], account_id=a["id"])
    pull["account_id"] = a["id"]
    return a


def no_account_message(need=1):
    acc = enabled()
    if not acc:
        return "No Seamless account is set up. Add your API key on the Seamless accounts page."
    parts = []
    for a in acc:
        u = usable(a)
        parts.append(f"{a['name']}: {'unknown' if u is None else f'{u:,}'} usable")
    return (f"No Seamless account has {need:,} credits to spare (above the ACCOUNT_RESERVE of {cfg.ACCOUNT_RESERVE:,} "
            f"and within its monthly budget). " + "; ".join(parts) + ".")


def others_with_room(account_id, need=1):
    out = []
    for a in enabled():
        if a["id"] == account_id:
            continue
        u = usable(a)
        if u is None or u >= need:
            out.append({"id": a["id"], "name": a["name"], "usable": u, "balance": a["balance"]})
    return out


def record_balance(account_id, balance, now=None):
    """Called on every Seamless answer that carries the live balance."""
    now = now or time.time()
    a = get(account_id)
    if not a:
        return
    db.x("UPDATE accounts SET balance=?, balance_at=?, last_error=NULL WHERE id=?", (balance, now, account_id))
    month = db.month_key(now)
    m = db.q1("SELECT * FROM balance_marks WHERE account_id=? AND month=?", (account_id, month))
    if not m:
        db.x("INSERT OR IGNORE INTO balance_marks(account_id, month, first_balance, first_at, last_balance, last_at, added) "
             "VALUES(?,?,?,?,?,?,0)", (account_id, month, balance, now, balance, now))
        return
    added = m["added"] or 0
    if m["last_balance"] is not None and balance > m["last_balance"]:
        added += balance - m["last_balance"]          # a top-up or monthly refill
    db.x("UPDATE balance_marks SET last_balance=?, last_at=?, added=? WHERE account_id=? AND month=?",
         (balance, now, added, account_id, month))


def balance_check(a, now=None):
    """Seamless's own numbers vs this tool's count, since the first balance seen this month."""
    m = db.q1("SELECT * FROM balance_marks WHERE account_id=? AND month=?", (a["id"], db.month_key(now)))
    if not m or m["last_balance"] is None:
        return None
    seamless_used = m["first_balance"] + (m["added"] or 0) - m["last_balance"]
    # Spending logged before the first balance of the month was seen is not in Seamless's number.
    tool_used = db.spend_since(a["id"], m["first_at"] + 0.0001)
    diff = seamless_used - tool_used
    out = {"since": m["first_at"], "start_balance": m["first_balance"], "added": m["added"] or 0,
           "balance": m["last_balance"], "seamless_used": seamless_used, "tool_used": tool_used, "difference": diff,
           "warn": abs(diff) > cfg.BALANCE_MISMATCH_WARN}
    if diff > cfg.BALANCE_MISMATCH_WARN:
        out["message"] = (f"Seamless's balance went down by {seamless_used:,} credits but this tool spent {tool_used:,}: "
                          f"{diff:,} credits were used outside this tool (the Seamless website or Chrome extension, "
                          f"another tool, or another user on the same credit pool).")
    elif diff < -cfg.BALANCE_MISMATCH_WARN:
        out["message"] = (f"This tool counted {tool_used:,} credits but Seamless's balance only went down by "
                          f"{seamless_used:,}: Seamless didn't charge for {-diff:,} of them.")
    else:
        out["message"] = f"Matches: Seamless and this tool both show about {tool_used:,} credits used."
    return out


def shared_pool_groups():
    """Accounts whose last balances are identical, read within 15 minutes of each other: probably one shared pool
    (Seamless's docs say credits belong to the whole organization)."""
    acc = [a for a in enabled() if a["balance"] is not None and a["balance_at"]]
    groups = {}
    for a in acc:
        groups.setdefault(a["balance"], []).append(a)
    out = []
    for bal, accs in groups.items():
        if len(accs) > 1 and max(x["balance_at"] for x in accs) - min(x["balance_at"] for x in accs) < 900:
            out.append({"balance": bal, "names": [x["name"] for x in accs]})
    return out


def total_balance():
    """Sum over enabled accounts, counting a shared pool once."""
    acc = [a for a in enabled() if a["balance"] is not None]
    if not acc:
        return None
    shared = {g["balance"] for g in shared_pool_groups()}
    seen, total = set(), 0
    for a in acc:
        if a["balance"] in shared:
            if a["balance"] in seen:
                continue
            seen.add(a["balance"])
        total += a["balance"]
    return total


def summary(a):
    return {"id": a["id"], "name": a["name"], "key": mask(a["api_key"]), "priority": a["priority"],
            "enabled": bool(a["enabled"]), "monthly_budget": a["monthly_budget"] or 0, "source": a["source"],
            "balance": a["balance"], "balance_at": a["balance_at"], "last_error": a["last_error"],
            "spent_this_month": db.month_spend(a["id"]), "budget_left": budget_left(a), "usable": usable(a),
            "low": bool(cfg.LOW_BALANCE_WARNING and a["balance"] is not None and a["balance"] < cfg.LOW_BALANCE_WARNING),
            "check": balance_check(a)}
