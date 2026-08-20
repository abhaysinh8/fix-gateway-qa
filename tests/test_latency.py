from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fixgateway.client.fix_client import FixClient
from fixgateway.exchange.mock_exchange import MockExchange
from fixgateway.reporting.latency import (
    LatencyRegressionError,
    calculate_latency_percentiles,
    compare_latency_baseline,
    format_latency_table,
)
from fixgateway.reporting.run_latency_check import measure_orders


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = Path(
    os.getenv(
        "LATENCY_BASELINE_PATH",
        PROJECT_ROOT / "reports" / "latency_baseline.json",
    )
)


def test_order_gateway_latency_against_baseline() -> None:
    order_count = int(os.getenv("LATENCY_ORDER_COUNT", "200"))
    regression_threshold = float(os.getenv("LATENCY_REGRESSION_THRESHOLD", "0.20"))
    update_baseline = os.getenv("UPDATE_BASELINE") == "1"

    with MockExchange(
        processing_delay_ms=(2.0, 5.0), fill_strategy="resting"
    ) as exchange:
        with FixClient(
            exchange.host, exchange.port, sender_comp_id="PYTEST_LATENCY"
        ) as client:
            samples = measure_orders(client, order_count)

    metrics = calculate_latency_percentiles(samples)
    print("\nFIX order gateway latency")
    print(format_latency_table(metrics))
    comparison = compare_latency_baseline(
        metrics,
        BASELINE_PATH,
        regression_threshold=regression_threshold,
        update=update_baseline,
    )
    if comparison.action == "created":
        print(f"No previous baseline found; created {BASELINE_PATH}")
    elif comparison.action == "updated":
        print(f"UPDATE_BASELINE=1; updated {BASELINE_PATH}")
    else:
        print(
            f"Compared with p99 baseline {comparison.baseline_p99:.3f} ms; "
            f"allowed maximum is {comparison.allowed_p99:.3f} ms"
        )


def test_latency_percentile_calculation_is_deterministic() -> None:
    metrics = calculate_latency_percentiles([1.0, 2.0, 3.0, 4.0, 5.0])
    assert metrics == {
        "sample_count": 5,
        "p50": 3.0,
        "p99": 4.96,
        "p99_9": 4.996,
        "max": 5.0,
    }


def test_p99_regression_over_threshold_fails(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"p99": 10.0}), encoding="utf-8")
    current = {
        "sample_count": 1,
        "p50": 13.0,
        "p99": 13.0,
        "p99_9": 13.0,
        "max": 13.0,
    }

    with pytest.raises(LatencyRegressionError, match="30.0%"):
        compare_latency_baseline(current, baseline, regression_threshold=0.20)


def test_update_baseline_overwrites_regression(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"p99": 1.0}), encoding="utf-8")
    current = {
        "sample_count": 1,
        "p50": 5.0,
        "p99": 5.0,
        "p99_9": 5.0,
        "max": 5.0,
    }

    comparison = compare_latency_baseline(current, baseline, update=True)

    assert comparison.action == "updated"
    assert json.loads(baseline.read_text(encoding="utf-8"))["p99"] == 5.0
