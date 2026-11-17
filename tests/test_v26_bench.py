"""2.6: Seamless credits and emails never worse than 2.5.1 on the same data; fewer MillionVerifier checks."""
import json
import os

import pytest

from tests.bench_guess import BASELINE, SCENARIOS, SEEDS, measure

RESULTS = {}


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_guessing_benchmark(env, scenario, seed):
    got = measure(env, scenario, seed)
    key = f"{scenario}/{seed}"
    if os.environ.get("LP_BENCH_RECORD") == "1":
        old = json.load(open(BASELINE)) if os.path.exists(BASELINE) else {}
        old[key] = got
        json.dump(old, open(BASELINE, "w"), indent=1, sort_keys=True)
        return
    base = json.load(open(BASELINE))[key]
    RESULTS[key] = (base, got)
    print(f"\n{key}: 2.5.1 {base}\n{' ' * len(key)}  now   {got}")
    target = got["target"]
    if max(1, int(os.environ.get("GUESS_PARALLEL", "10") or 10)) <= 1:
        # one at a time (GUESS_PARALLEL=1): exactly as 2.6/2.7, never worse than 2.5.1 in any single run
        assert got["research"] <= base["research"], (key, base, got)
        # (a pull that reaches its target can stop one or two emails past it either way: that's not a loss)
        assert got["emails"] >= min(base["emails"], target), (key, base, got)
        assert got["emails"] >= base["emails"] or got["emails"] >= target, (key, base, got)
        assert got["research"] / max(1, got["emails"]) <= base["research"] / max(1, base["emails"]) + 0.005, (key, base, got)
        assert got["mv_checks"] < base["mv_checks"], (key, base, got)
        return
    # 2.8, companies worked on side by side: the order people are handled in changes, so a single run can land a
    # credit or two either way (a different page order in Seamless). Each run stays within that; the totals over all
    # 15 runs must be no worse than 2.5.1 (test_benchmark_totals).
    assert got["research"] <= base["research"] + 2, (key, base, got)
    assert got["emails"] >= min(base["emails"], target) - 3, (key, base, got)
    assert got["research"] / max(1, got["emails"]) <= base["research"] / max(1, base["emails"]) + 0.02, (key, base, got)


def test_benchmark_totals():
    """All 15 runs together: never more Seamless credits (research or search), never fewer emails, fewer
    MillionVerifier checks than 2.5.1."""
    if os.environ.get("LP_BENCH_RECORD") == "1" or len(RESULTS) < len(SEEDS) * len(SCENARIOS):
        pytest.skip("needs the whole benchmark run")
    tot = lambda i, k: sum(v[i][k] for v in RESULTS.values())
    assert tot(1, "research") <= tot(0, "research"), (tot(1, "research"), tot(0, "research"))
    assert tot(1, "search") <= tot(0, "search"), (tot(1, "search"), tot(0, "search"))
    # emails up to each run's target (stopping one past the target instead of two is not a loss)
    capped = lambda i: sum(min(v[i]["emails"], v[1]["target"]) for v in RESULTS.values())
    assert capped(1) >= capped(0), (capped(1), capped(0))
    assert tot(1, "mv_checks") < tot(0, "mv_checks"), (tot(1, "mv_checks"), tot(0, "mv_checks"))
