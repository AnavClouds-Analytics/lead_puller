"""Lead Puller 2.7: split a pull evenly across the company sizes and countries you pick.

"Evenly": each size (and/or country) gets an equal share of the target. A group that runs out of people, or gets too
expensive, keeps what it found and its unused share goes to the others. Never more than the target in total.
"In order" (and every pull made before 2.7): cheapest first, exactly as before.
Every test ends with check_books / check_people (books match Seamless to the credit, nobody paid twice, every email is
that person's own mailbox)."""
import collections
import json

import pytest

from app import db, engine
from tests.conftest import add_verifier, check_books, check_people, run

SENIOR = {"seniority": {"in": ["C-Level", "VP"]}}


def pull(client, filters, target, split_sizes="", split_countries="", max_credits=2000, **kw):
    r = client.post("/api/pulls", json={"filters": filters, "target": target, "max_credits": max_credits,
                                        "split_sizes": split_sizes, "split_countries": split_countries, **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def by_group(pid):
    rows = db.q(f"""SELECT s.grp g, COUNT(*) n FROM contacts c JOIN pull_slices s ON s.id=c.slice_id
                    WHERE c.pull_id=? AND {engine.C_DELIVERABLE} GROUP BY s.grp""", (pid,))
    return {r["g"]: r["n"] for r in rows}


def by_size(pid):
    return collections.Counter(r["size"] for r in db.q(f"SELECT size FROM contacts WHERE pull_id=? AND {engine.DELIVERABLE}",
                                                      (pid,)))


def by_country(pid):
    return collections.Counter(r["country"] for r in db.q(f"SELECT country FROM contacts WHERE pull_id=? AND "
                                                         f"{engine.DELIVERABLE}", (pid,)))


def hi(target):
    """Like any pull, a few extra emails can land at the very end (answers already on their way); never many."""
    return target + max(3, round(target * 0.15))


def emails(pid):
    return engine.counts(pid)["emails"]


def test_in_order_fills_from_one_size_first(env):
    """Today's behaviour, kept for "In order": the cheapest size fills the target."""
    client, fake, clock = env
    pid = pull(client, {**SENIOR, "companySize": {"in": ["51 - 200", "201 - 500"]}}, 30)
    run(client, pid)
    s = by_size(pid)
    assert emails(pid) >= 30 and max(s.values()) >= 25, s
    assert not db.q1("SELECT 1 x FROM pull_slices WHERE pull_id=? AND grp IS NOT NULL", (pid,))
    check_books(fake)
    check_people(fake)


def test_even_across_two_sizes(env):
    client, fake, clock = env
    pid = pull(client, {**SENIOR, "companySize": {"in": ["51 - 200", "201 - 500"]}}, 40, split_sizes="even")
    p = run(client, pid)
    s = by_size(pid)
    assert 40 <= emails(pid) <= hi(40), s                                  # like any pull: a few extra can land at the end
    assert abs(s["51 - 200"] - s["201 - 500"]) <= 4, s                 # 20 each, give or take the last answers
    assert {g["group"] for g in p["groups"]} == {"51 - 200", "201 - 500"}
    assert by_group(pid) == {"51 - 200": s["51 - 200"], "201 - 500": s["201 - 500"]}
    check_books(fake)
    check_people(fake)


def test_even_across_two_countries(env):
    client, fake, clock = env
    pid = pull(client, {**SENIOR, "contactCountry": {"in": ["United States", "Canada"]}}, 50, split_countries="even")
    run(client, pid)
    c = by_country(pid)
    assert 50 <= emails(pid) <= hi(50) and set(c) == {"United States", "Canada"}, c
    assert abs(c["United States"] - c["Canada"]) <= 4, c
    # each country group is still split by size inside, cheapest first, as before
    labels = [r["label"] for r in db.q("SELECT label FROM pull_slices WHERE pull_id=? ORDER BY idx", (pid,))]
    assert any(l.startswith("Canada · ") for l in labels) and any(l.startswith("United States · ") for l in labels)
    check_books(fake)
    check_people(fake)


def test_even_sizes_and_countries_together(env):
    client, fake, clock = env
    f = {**SENIOR, "contactCountry": {"in": ["United States", "Canada"]}, "companySize": {"in": ["11 - 50", "201 - 500"]}}
    pid = pull(client, f, 40, split_sizes="even", split_countries="even")
    p = run(client, pid)
    g = by_group(pid)
    assert set(g) == {"11 - 50 · United States", "11 - 50 · Canada", "201 - 500 · United States", "201 - 500 · Canada"}
    assert 40 <= sum(g.values()) <= hi(40) and max(g.values()) - min(g.values()) <= 4, g     # 10 each
    assert len(p["groups"]) == 4 and all(x["planned"] == 10 for x in p["groups"])
    check_books(fake)
    check_people(fake)


def test_short_group_passes_its_share_to_the_others(env):
    """Israel has only a handful of people at 201–500: it keeps what it finds, the US makes up the rest."""
    client, fake, clock = env
    f = {**SENIOR, "contactCountry": {"in": ["United States", "Israel"]}, "companySize": {"in": ["201 - 500"]}}
    pid = pull(client, f, 40, split_countries="even")
    p = run(client, pid)
    c = by_country(pid)
    assert 40 <= emails(pid) <= hi(40), c
    assert c["Israel"] < 20 and c["United States"] >= 20 + (20 - c["Israel"]) - 2, c
    isr = next(x for x in p["groups"] if x["group"] == "Israel")
    assert not isr["open"]                                                 # ran out of people
    check_books(fake)
    check_people(fake)


def test_never_more_than_the_target_even_when_every_group_is_big(env):
    client, fake, clock = env
    f = {**SENIOR, "companySize": {"in": ["2 - 10", "11 - 50", "51 - 200", "201 - 500", "501 - 1,000"]}}
    for target in (5, 7, 13):
        pid = pull(client, f, target, split_sizes="even")
        run(client, pid)
        assert target <= emails(pid) <= hi(target), (target, by_size(pid))
    check_books(fake)
    check_people(fake)


def test_a_group_running_out_finishes_the_pull_cleanly(env):
    """Every group runs out before the target: the pull finishes with what exists, nothing hangs."""
    client, fake, clock = env
    f = {**SENIOR, "contactCountry": {"in": ["Israel", "Germany"]}, "companySize": {"in": ["201 - 500"]}}
    pid = pull(client, f, 500, split_countries="even")
    p = run(client, pid)
    assert p["status"] == "done" and emails(pid) < 20 and "no more new people" in (p["stop_reason"] or "")
    check_books(fake)
    check_people(fake)


def test_one_value_means_nothing_to_split(env):
    client, fake, clock = env
    pid = pull(client, {**SENIOR, "companySize": {"in": ["51 - 200"]}, "contactCountry": {"in": ["Canada"]}}, 10,
               split_sizes="even", split_countries="even")
    run(client, pid)
    assert not db.q1("SELECT 1 x FROM pull_slices WHERE pull_id=? AND grp IS NOT NULL", (pid,))
    assert client.get(f"/api/pulls/{pid}").json()["groups"] == []
    check_books(fake)


def test_excluded_country_stays_excluded_in_every_group(env):
    client, fake, clock = env
    f = {**SENIOR, "contactCountry": {"in": ["United States", "Canada"], "out": ["Israel"]}}
    pid = pull(client, f, 20, split_countries="even")
    run(client, pid)
    p = db.get_pull(pid)
    for sl in db.q("SELECT * FROM pull_slices WHERE pull_id=?", (pid,)):
        body = engine._slice_body(p, sl)
        assert body["contactCountry"][0] == sl["country_value"] and "-Israel" in body["contactCountry"]
        assert len([v for v in body["contactCountry"] if not v.startswith("-")]) == 1
    assert "Israel" not in by_country(pid)
    check_books(fake)


def test_garbage_split_values_mean_in_order(env):
    client, fake, clock = env
    pid = pull(client, {**SENIOR, "companySize": {"in": ["51 - 200", "201 - 500"]}}, 10, split_sizes="EVENLY<script>",
               split_countries=123)
    p = db.get_pull(pid)
    assert p["split_sizes"] == "" and p["split_countries"] == ""
    pid2 = pull(client, {**SENIOR, "companySize": {"in": ["51 - 200", "201 - 500"]}}, 10, split_sizes="EVEN")
    assert db.get_pull(pid2)["split_sizes"] == "even"


def test_company_lists_are_never_split(env):
    client, fake, clock = env
    doms = sorted({p["domain"] for p in fake.people if p["domain"]})[:20]
    r = client.post("/api/pulls", json={"filters": SENIOR, "target": 20, "max_credits": 500, "max_per_company": 2,
                                        "split_sizes": "even", "split_countries": "even",
                                        "companies": [{"name": "", "domain": d, "signal": ""} for d in doms]})
    assert r.status_code == 200, r.text
    p = db.get_pull(r.json()["id"])
    assert p["split_sizes"] == "" and p["split_countries"] == ""


def test_run_again_keeps_the_split(env):
    client, fake, clock = env
    pid = pull(client, {**SENIOR, "companySize": {"in": ["51 - 200", "201 - 500"]}}, 10, split_sizes="even")
    run(client, pid)
    new = client.post(f"/api/pulls/{pid}/rerun").json()["id"]
    assert db.get_pull(new)["split_sizes"] == "even"


def test_restart_mid_pull_carries_on_evenly(env):
    """The split is saved with the pull: stopping the server halfway and starting again changes nothing."""
    client, fake, clock = env
    pid = pull(client, {**SENIOR, "companySize": {"in": ["51 - 200", "201 - 500"]}}, 30, split_sizes="even")
    for _ in range(6):
        engine.tick()
    db._local.con = None                                  # a fresh connection, as after a restart
    run(client, pid)
    s = by_size(pid)
    assert 30 <= emails(pid) <= hi(30) and abs(s["51 - 200"] - s["201 - 500"]) <= 4, s
    check_books(fake)
    check_people(fake)


def test_with_guessing_on(env):
    client, fake, clock = env
    add_verifier(client)
    pid = pull(client, {**SENIOR, "contactCountry": {"in": ["United States", "Canada"]}}, 40, split_countries="even")
    run(client, pid)
    c = by_country(pid)
    assert 40 <= emails(pid) <= hi(40) and abs(c["United States"] - c["Canada"]) <= 4, c
    check_books(fake)
    check_people(fake)


def test_page_has_the_switch_and_progress(env):
    client, fake, clock = env
    html = client.get("/").text
    for s in ('data-split="${ax}|even"', "split_sizes:", "split_countries:", "gets an equal share of the target", "function splitHtml",
              "function groupsCard", "Shares of the target"):
        assert s in html, s


def test_too_many_groups_for_the_target_falls_back(env):
    """Every group's first page costs at least a credit: under 10 emails per group the pull splits one way only, or
    runs in order, so a small target never pays for dozens of tiny searches."""
    client, fake, clock = env
    f = {**SENIOR, "contactCountry": {"in": ["United States", "Canada", "Israel"]},
         "companySize": {"in": ["2 - 10", "11 - 50", "51 - 200", "201 - 500"]}}
    both = pull(client, f, 120, split_sizes="even", split_countries="even")      # 12 groups x 10
    assert engine._split_axes(db.get_pull(both)) == (["2 - 10", "11 - 50", "51 - 200", "201 - 500"],
                                                    ["United States", "Canada", "Israel"])
    sizes_only = pull(client, f, 40, split_sizes="even", split_countries="even")  # 12 groups too many; 4 sizes fit
    assert engine._split_axes(db.get_pull(sizes_only)) == (["2 - 10", "11 - 50", "51 - 200", "201 - 500"], [None])
    countries_only = pull(client, f, 35, split_countries="even")                    # 3 countries fit
    assert engine._split_axes(db.get_pull(countries_only)) == ([None], ["United States", "Canada", "Israel"])
    tiny = pull(client, f, 15, split_sizes="even", split_countries="even")
    assert engine._split_axes(db.get_pull(tiny)) == ([None], [None])
    p = run(client, tiny)
    assert 15 <= emails(tiny) <= hi(15) and p["groups"] == []


def test_stop_and_finish_now_mid_split(env):
    client, fake, clock = env
    f = {**SENIOR, "companySize": {"in": ["51 - 200", "201 - 500"]}}
    a = pull(client, f, 150, split_sizes="even")
    engine.tick()
    client.post(f"/api/pulls/{a}/stop")
    p = run(client, a)
    assert p["status"] == "done" and emails(a) < 150 and "Stopped by you" in p["stop_reason"]
    b = pull(client, f, 150, split_sizes="even")
    engine.tick()
    client.post(f"/api/pulls/{b}/finish-now")
    assert run(client, b)["status"] == "done"
    check_books(fake)
    check_people(fake)
