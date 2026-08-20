"""Run the complete fix-gateway-qa portfolio demonstration."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from fixgateway.client.fix_client import FixClient
from fixgateway.exchange.mock_exchange import MockExchange
from fixgateway.reporting.latency import (
    LatencyRegressionError,
    calculate_latency_percentiles,
    compare_latency_baseline,
    format_latency_table,
)
from fixgateway.reporting.report_generator import generate_report
from fixgateway.reporting.run_latency_check import measure_orders
from fixgateway.surveillance.pattern_detector import (
    detect_high_cancel_ratio,
    detect_quote_stuffing_or_spoofing,
)


PROJECT_ROOT = Path(__file__).resolve().parent
REPORT_PATH = PROJECT_ROOT / "reports" / "report.html"
BASELINE_PATH = PROJECT_ROOT / "reports" / "latency_baseline.json"


def _pytest_summary() -> tuple[int, int, int]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    print(output.strip())

    def count(label: str) -> int:
        match = re.search(rf"(\d+) {label}", output)
        return int(match.group(1)) if match else 0

    passed = count("passed")
    failed = count("failed") + count("error")
    skipped = count("skipped")
    if completed.returncode and failed == 0:
        failed = 1
    return passed + failed + skipped, passed, failed


def _timeline_event(
    order_id: str,
    action: str,
    timestamp_ms: float,
    *,
    quantity: int = 100,
    price: int = 200,
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "action": action,
        "timestamp": timestamp_ms,
        "quantity": quantity,
        "price": price,
        "symbol": "AAPL",
        "trader_id": "DEMO_TRADER",
    }


def run_demo() -> int:
    print("fix-gateway-qa end-to-end demo")
    print("\n1. Running the full pytest suite...")
    total_tests, passed_tests, failed_tests = _pytest_summary()

    print("\n2. Running live FIX order flows and latency sampling...")
    demo_checks: list[tuple[str, bool]] = []
    timeline: list[dict[str, object]] = []

    def record(name: str, condition: bool) -> None:
        demo_checks.append((name, condition))

    def fill_strategy(order):
        return [order.quantity] if order.order_id == "DEMO-FULL" else []

    with MockExchange(
        processing_delay_ms=(1.0, 3.0),
        fill_strategy=fill_strategy,
        random_seed=42,
    ) as exchange:
        with FixClient(exchange.host, exchange.port, sender_comp_id="DEMO_CLIENT") as client:
            acknowledgement = client.send_new_order(
                "DEMO-FULL", "MSFT", "1", 100, "400"
            )
            fill = client.receive_message()
            record("new order acknowledged", acknowledgement.get(39) == "0")
            record("order fully filled", fill.get(39) == "2" and fill.get(151) == "0")

            for index in range(6):
                order_id = f"DEMO-RAPID-{index}"
                placed_at = time.time_ns() / 1_000_000
                ack = client.send_new_order(order_id, "AAPL", "1", 100, "200")
                timeline.append(_timeline_event(order_id, "place", placed_at))
                canceled_at = time.time_ns() / 1_000_000
                cancel = client.send_cancel(
                    f"CANCEL-{order_id}", order_id, "AAPL", "1"
                )
                timeline.append(_timeline_event(order_id, "cancel", canceled_at))
                record(f"{order_id} acknowledged", ack.get(39) == "0")
                record(f"{order_id} canceled", cancel.get(39) == "4")

            latency_samples = measure_orders(
                client, int(os.getenv("DEMO_LATENCY_ORDER_COUNT", "50"))
            )

    latency = calculate_latency_percentiles(latency_samples)
    print(format_latency_table(latency))
    try:
        comparison = compare_latency_baseline(
            latency,
            BASELINE_PATH,
            regression_threshold=float(
                os.getenv("LATENCY_REGRESSION_THRESHOLD", "0.20")
            ),
            update=os.getenv("UPDATE_BASELINE") == "1",
        )
    except LatencyRegressionError as exc:
        record("latency baseline comparison", False)
        print(f"Latency baseline FAILED: {exc}")
    else:
        record("latency baseline comparison", True)
        print(f"Latency baseline {comparison.action}: p99 {latency['p99']:.3f} ms")

    print("\n3. Running simplified surveillance heuristics...")
    spoofing_matches = detect_quote_stuffing_or_spoofing(
        timeline, window_ms=1_000, cancel_threshold_ms=100
    )
    layering_matches = detect_high_cancel_ratio(
        timeline, window_ms=2_000, ratio_threshold=0.40, min_actions=5
    )
    record("spoofing-like pattern detected", bool(spoofing_matches))
    record("layering-like pattern detected", bool(layering_matches))

    surveillance_flags = list(spoofing_matches)
    if layering_matches:
        surveillance_flags.append(
            max(layering_matches, key=lambda match: match.cancel_ratio)
        )

    print("\n4. Generating HTML report...")
    generate_report(
        {
            "timestamp": datetime.now(UTC),
            "tests": {
                "total": total_tests,
                "passed": passed_tests,
                "failed": failed_tests,
            },
            "latency": latency,
            "surveillance_flags": surveillance_flags,
        },
        REPORT_PATH,
    )

    failed_checks = [name for name, passed in demo_checks if not passed]
    print("\nDemo summary")
    print(f"  Tests: {passed_tests}/{total_tests} passed")
    print(f"  Demo checks: {len(demo_checks) - len(failed_checks)}/{len(demo_checks)} passed")
    print(f"  Surveillance matches shown: {len(surveillance_flags)}")
    print(f"  Report: {REPORT_PATH}")
    if failed_checks or failed_tests:
        for name in failed_checks:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run_demo())
