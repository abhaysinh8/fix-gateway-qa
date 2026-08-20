from __future__ import annotations

import math

import pytest

from fixgateway.reporting.benchmark_runner import summarize_samples


def test_summarize_samples_reports_percentiles_dispersion_and_confidence_interval() -> None:
    result = summarize_samples([1, 2, 3, 4, 5])

    assert result["count"] == 5
    assert result["mean"] == 3
    assert result["p50"] == 3
    assert result["p95"] == pytest.approx(4.8)
    assert result["p99"] == pytest.approx(4.96)
    assert result["standard_deviation"] == pytest.approx(math.sqrt(2.5))
    assert result["mean_ci95_low"] < result["mean"] < result["mean_ci95_high"]


def test_summarize_single_sample_has_zero_dispersion() -> None:
    result = summarize_samples([2.5])

    assert result["standard_deviation"] == 0
    assert result["coefficient_of_variation_percent"] == 0
    assert result["mean_ci95_low"] == result["mean_ci95_high"] == 2.5
    assert result["min"] == result["max"] == 2.5


@pytest.mark.parametrize("samples", [[], [-1], [float("nan")], [float("inf")]])
def test_summarize_samples_rejects_invalid_input(samples: list[float]) -> None:
    with pytest.raises(ValueError):
        summarize_samples(samples)
