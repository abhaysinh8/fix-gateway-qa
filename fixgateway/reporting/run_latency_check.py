"""Run the FIX latency check without pytest.

Example:
    python -m fixgateway.reporting.run_latency_check --orders 200
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from fixgateway.client.fix_client import FixClient
from fixgateway.exchange.mock_exchange import MockExchange
from fixgateway.reporting.latency import (
    LatencyRegressionError,
    calculate_latency_percentiles,
    compare_latency_baseline,
    format_latency_table,
)


DEFAULT_BASELINE = Path(__file__).resolve().parents[2] / "reports" / "latency_baseline.json"


def measure_orders(client: FixClient, order_count: int) -> list[float]:
    if order_count <= 0:
        raise ValueError("order_count must be greater than zero")
    samples: list[float] = []
    for index in range(order_count):
        client.send_new_order(
            f"LATENCY-{index}", "AAPL", "1", 1, "100.00"
        )
        assert client.last_latency_ms is not None
        samples.append(client.last_latency_ms)
    return samples


def run_latency_check(
    *,
    order_count: int = 200,
    host: str | None = None,
    port: int | None = None,
    baseline_path: str | Path = DEFAULT_BASELINE,
    regression_threshold: float = 0.20,
    update_baseline: bool = False,
) -> dict[str, float | int]:
    """Measure a gateway, print percentiles, and enforce its saved p99 baseline."""

    local_exchange: MockExchange | None = None
    if port is None:
        local_exchange = MockExchange(
            host=host or "127.0.0.1",
            processing_delay_ms=(2.0, 5.0),
            fill_strategy="resting",
        ).start()
        host, port = local_exchange.host, local_exchange.port
    elif host is None:
        host = "127.0.0.1"

    try:
        with FixClient(host, port, sender_comp_id="LATENCY_CLIENT") as client:
            samples = measure_orders(client, order_count)
        metrics = calculate_latency_percentiles(samples)
        print(format_latency_table(metrics))
        comparison = compare_latency_baseline(
            metrics,
            baseline_path,
            regression_threshold=regression_threshold,
            update=update_baseline,
        )
        if comparison.action == "created":
            print(f"Baseline created at {Path(baseline_path)}")
        elif comparison.action == "updated":
            print(f"Baseline updated at {Path(baseline_path)}")
        else:
            print(
                f"Baseline comparison passed: current p99 "
                f"{comparison.current_p99:.3f} ms <= "
                f"{comparison.allowed_p99:.3f} ms allowed"
            )
        return metrics
    finally:
        if local_exchange is not None:
            local_exchange.stop()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--orders",
        type=int,
        default=int(os.getenv("LATENCY_ORDER_COUNT", "200")),
        help="number of orders to measure (default: 200)",
    )
    parser.add_argument("--host", help="existing exchange host; omit to start a local one")
    parser.add_argument("--port", type=int, help="existing exchange port")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(os.getenv("LATENCY_BASELINE_PATH", DEFAULT_BASELINE)),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=float(os.getenv("LATENCY_REGRESSION_THRESHOLD", "0.20")),
        help="permitted fractional p99 regression (default: 0.20)",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        default=os.getenv("UPDATE_BASELINE") == "1",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        run_latency_check(
            order_count=args.orders,
            host=args.host,
            port=args.port,
            baseline_path=args.baseline,
            regression_threshold=args.threshold,
            update_baseline=args.update_baseline,
        )
    except LatencyRegressionError as exc:
        print(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

