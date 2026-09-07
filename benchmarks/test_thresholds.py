"""Sanity check that every threshold constant is defined and a sane
positive number -- the values themselves are calibration choices (see the
comment above each in thresholds.py), not something to assert exact
figures for.
"""

from __future__ import annotations

from benchmarks import thresholds


def test_all_threshold_constants_are_positive_numbers():
    names = [
        "OVERHEAD_P95_MS",
        "THROUGHPUT_MIN_RPS",
        "THROUGHPUT_P95_MS",
        "FAILOVER_RELIABILITY_MIN_PCT",
        "FAILOVER_SWITCH_MAX_SECONDS",
        "RECOVERY_MAX_SECONDS_OVER_COOLDOWN",
        "RATELIMIT_ADMIT_TOLERANCE",
        "BUDGET_OVERSHOOT_MAX_PCT",
    ]
    for name in names:
        value = getattr(thresholds, name)
        assert isinstance(value, (int, float))
        assert value > 0
