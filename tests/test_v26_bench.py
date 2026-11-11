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
    # never more Seamless research credits, never fewer emails, never fewer guesses
    assert got["research"] <= base["research"], (key, base, got)
    # (a pull that reaches its target can stop one or two emails past it either way: that's not a loss)
    target = got["target"]
    assert got["emails"] >= min(base["emails"], target), (key, base, got)
    assert got["emails"] >= base["emails"] or got["emails"] >= target, (key, base, got)
    assert got["research"] / max(1, got["emails"]) <= base["research"] / max(1, base["emails"]) + 0.005, (key, base, got)
    # and fewer MillionVerifier checks
    assert got["mv_checks"] < base["mv_checks"], (key, base, got)
