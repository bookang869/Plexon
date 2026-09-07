"""Unit tests for benchmarks/common.py's pure helpers. Not marked
`benchmark` -- these don't talk to a live gateway, so they run under plain
`pytest` like any other test.
"""

from __future__ import annotations

from benchmarks.common import histogram_percentiles_from_metrics_text, percentiles


def test_percentiles_known_sorted_list():
    samples = [float(x) for x in range(1, 101)]  # 1..100

    result = percentiles(samples, points=(0.5, 0.95, 0.99))

    assert result[0.5] == 51.0
    assert result[0.95] == 96.0
    assert result[0.99] == 100.0


def test_percentiles_empty_list_returns_zeros():
    result = percentiles([], points=(0.5, 0.99))

    assert result == {0.5: 0.0, 0.99: 0.0}


_METRICS_TEXT = """\
# HELP fake_latency_seconds A fake histogram for testing.
# TYPE fake_latency_seconds histogram
fake_latency_seconds_bucket{provider="anthropic",le="0.005"} 0
fake_latency_seconds_bucket{provider="anthropic",le="0.01"} 20
fake_latency_seconds_bucket{provider="anthropic",le="0.05"} 90
fake_latency_seconds_bucket{provider="anthropic",le="0.1"} 100
fake_latency_seconds_bucket{provider="anthropic",le="+Inf"} 100
fake_latency_seconds_sum{provider="anthropic"} 4.2
fake_latency_seconds_count{provider="anthropic"} 100
fake_latency_seconds_bucket{provider="openai",le="0.005"} 0
fake_latency_seconds_bucket{provider="openai",le="0.01"} 5
fake_latency_seconds_bucket{provider="openai",le="0.05"} 5
fake_latency_seconds_bucket{provider="openai",le="0.1"} 5
fake_latency_seconds_bucket{provider="openai",le="+Inf"} 5
fake_latency_seconds_sum{provider="openai"} 0.03
fake_latency_seconds_count{provider="openai"} 5
"""


def test_histogram_percentiles_from_metrics_text_filters_by_label():
    result = histogram_percentiles_from_metrics_text(
        _METRICS_TEXT,
        "fake_latency_seconds",
        {"provider": "anthropic"},
        points=(0.5, 0.99),
    )

    # median (50th of 100) falls inside the (0.01, 0.05] bucket (20 -> 90)
    assert 0.01 < result[0.5] < 0.05
    # p99 (99th of 100) falls inside the (0.05, 0.1] bucket (90 -> 100)
    assert 0.05 < result[0.99] <= 0.1


def test_histogram_percentiles_from_metrics_text_no_label_filter_aggregates_all_series():
    result = histogram_percentiles_from_metrics_text(
        _METRICS_TEXT,
        "fake_latency_seconds",
        None,
        points=(0.5,),
    )

    # 105 total samples across both series; median still falls within the
    # buckets populated by the "anthropic" series.
    assert 0.0 < result[0.5] <= 0.1


def test_histogram_percentiles_from_metrics_text_unknown_metric_returns_zeros():
    result = histogram_percentiles_from_metrics_text(
        _METRICS_TEXT, "does_not_exist", None, points=(0.5, 0.99)
    )

    assert result == {0.5: 0.0, 0.99: 0.0}
