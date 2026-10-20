"""Seamless accounts (one API key each), used in the order you set. Memory is shared by all of them."""
import time

from . import db
from .config import cfg


def mask(key):
    return ("•••• " + key[-4:]) if key else ""


def sync_env_key():
    """SEAMLESS_API_KEY in .env (if set) is kept as one more account."""
    key = cfg.SEAMLESS_API_KEY
    if not key:
        return
    if db.q1("SELECT id FROM accounts WHERE api_key=?", (key,)):
        return
    old = db.q1("SELECT id FROM accounts WHERE source='env'")
    if old:
        db.x("UPDATE accounts SET api_key=?, balance=NULL, last_error=NULL WHERE id=?", (key, old["id"]))
        return
    n = db.q1("SELECT COALESCE(MAX(priority),0) m FROM accounts")["m"]
    db.x("INSERT INTO accounts(name,api_key,priority,enabled,source,created_at) VALUES(?,?,?,?,?,?)",
         ("Account from .env", key, n + 1, 1, "env", time.time()))


def all_accounts():
    return db.q("SELECT * FROM accounts ORDER BY priority, id")


def enabled():
    return [a for a in all_accounts() if a["enabled"] and a["api_key"]]


def get(aid):
    return db.q1("SELECT * FROM accounts WHERE id=?", (aid,)) if aid else None


def budget_left(a):
    """Credits this account may still spend this month (None = no limit), counting the overall budget too."""
    lefts = []
    if a and a.get("monthly_budget"):
        lefts.append(max(0, a["monthly_budget"] - db.month_spend(a["id"])))
    if cfg.MONTHLY_CREDIT_BUDGET:
        lefts.append(max(0, cfg.MONTHLY_CREDIT_BUDGET - db.month_spend()))
    return min(lefts) if lefts else None


def usable(a):
    """Credits that may be spent on this account right now (None = balance not known yet and no budget)."""
    if not a or not a["enabled"] or not a["api_key"]:
        return 0
    vals = []
    if a.get("balance") is not None:
        vals.append(max(0, a["balance"] - cfg.ACCOUNT_RESERVE))
    b = budget_left(a)
    if b is not None:
        vals.append(b)
    return min(vals) if vals else None


def ordered_for(choice):
    """Accounts a pull may use, in order. choice: 'auto' (all, in your order) or an account id."""
    acc = enabled()
    if choice and choice != "auto":
        acc = [a for a in acc if str(a["id"]) == str(choice)]
    return acc


def summary(a):
    return {"id": a["id"], "name": a["name"], "key": mask(a["api_key"]), "priority": a["priority"],
            "enabled": bool(a["enabled"]), "monthly_budget": a["monthly_budget"] or 0, "source": a["source"],
            "balance": a["balance"], "balance_at": a["balance_at"], "last_error": a["last_error"],
            "spent_this_month": db.month_spend(a["id"]), "usable": usable(a),
            "low": bool(cfg.LOW_BALANCE_WARNING and a["balance"] is not None and a["balance"] < cfg.LOW_BALANCE_WARNING)}
