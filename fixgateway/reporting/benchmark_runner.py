"""Reproducible protocol, lifecycle, and TCP benchmarks with statistics.

The runner is intentionally a local QA benchmark rather than a claim about production
exchange capacity. Results include the host/Python metadata and configurable workload
sizes so they can be reproduced and compared honestly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import statistics
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from fixgateway.client.fix_client import FixClient
from fixgateway.exchange.mock_exchange import MockExchange
from fixgateway.exchange.state_machine import OrderEvent, OrderStateMachine
from fixgateway.fix.message import FixMessage, decode
from fixgateway.fix.validator import validate
from fixgateway.marketdata.conformance_checker import feed_replay
from fixgateway.marketdata.md_message import (
    MDEntryType,
    MDUpdateAction,
    MarketDataEntry,
    MarketDataIncrementalRefresh,
    MarketDataSnapshotFullRefresh,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "benchmark_results.json"


def _percentile(ordered: Sequence[float], percentile: float) -> float:
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def summarize_samples(samples: Sequence[float]) -> dict[str, float | int]:
    """Return descriptive latency statistics and a 95% CI for the sample mean."""

    values = [float(value) for value in samples]
    if not values:
        raise ValueError("At least one sample is required")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Samples must be finite and non-negative")
    ordered = sorted(values)
    mean = statistics.fmean(ordered)
    standard_deviation = statistics.stdev(ordered) if len(ordered) > 1 else 0.0
    margin = 1.96 * standard_deviation / math.sqrt(len(ordered))
    return {
        "count": len(ordered),
        "mean": mean,
        "standard_deviation": standard_deviation,
        "coefficient_of_variation_percent": (
            0.0 if mean == 0 else standard_deviation / mean * 100
        ),
        "mean_ci95_low": max(0.0, mean - margin),
        "mean_ci95_high": mean + margin,
        "min": ordered[0],
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "p99_9": _percentile(ordered, 0.999),
        "max": ordered[-1],
    }


def _microbenchmark(
    operation: Callable[[], Any], *, iterations: int, rounds: int, warmup: int
) -> dict[str, Any]:
    for _ in range(warmup):
        operation()

    latency_us: list[float] = []
    throughput: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter_ns()
        for _ in range(iterations):
            operation()
        elapsed_seconds = (time.perf_counter_ns() - started) / 1_000_000_000
        latency_us.append(elapsed_seconds * 1_000_000 / iterations)
        throughput.append(iterations / elapsed_seconds)
    return {
        "iterations_per_round": iterations,
        "rounds": rounds,
        "latency_us_per_operation": summarize_samples(latency_us),
        "throughput_operations_per_second": summarize_samples(throughput),
    }


def _fix_fixture() -> tuple[FixMessage, bytes]:
    message = FixMessage(
        {
            8: "FIX.4.4",
            35: "D",
            49: "BENCH_CLIENT",
            56: "MOCK_EXCHANGE",
            34: "1",
            52: "20260820-12:00:00.000",
            11: "BENCH-FIXTURE",
            55: "AAPL",
            54: "1",
            38: "100",
            40: "2",
            44: "200.25",
            59: "0",
        }
    )
    return message, message.encode()


def _market_data_fixture() -> list[bytes]:
    return [
        MarketDataSnapshotFullRefresh(
            "AAPL",
            1,
            [
                MarketDataEntry(MDEntryType.BID, "100", "10"),
                MarketDataEntry(MDEntryType.OFFER, "101", "12"),
            ],
        ).encode(),
        MarketDataIncrementalRefresh(
            "AAPL",
            2,
            [
                MarketDataEntry(
                    MDEntryType.BID, "100", "15", MDUpdateAction.CHANGE
                )
            ],
        ).encode(),
    ]


def _microbenchmarks(
    *, iterations: int, market_iterations: int, rounds: int
) -> dict[str, Any]:
    message, raw = _fix_fixture()
    market_messages = _market_data_fixture()

    def lifecycle() -> None:
        machine = OrderStateMachine()
        machine.apply(OrderEvent.ACK)
        machine.apply(OrderEvent.FULL_FILL)

    return {
        "fix_encode": _microbenchmark(
            message.encode, iterations=iterations, rounds=rounds, warmup=500
        ),
        "fix_decode": _microbenchmark(
            lambda: decode(raw), iterations=iterations, rounds=rounds, warmup=500
        ),
        "message_validation": _microbenchmark(
            lambda: validate(message),
            iterations=iterations,
            rounds=rounds,
            warmup=500,
        ),
        "order_lifecycle": _microbenchmark(
            lifecycle, iterations=iterations, rounds=rounds, warmup=500
        ),
        "market_data_replay": _microbenchmark(
            lambda: feed_replay(market_messages),
            iterations=market_iterations,
            rounds=rounds,
            warmup=50,
        ),
    }


def _tcp_benchmarks(
    *, sequential_orders: int, concurrent_orders: int, concurrency: int
) -> dict[str, Any]:
    with MockExchange(
        processing_delay_ms=(0, 0),
        fill_strategy="resting",
        random_seed=101,
    ) as exchange:
        with FixClient(
            exchange.host,
            exchange.port,
            sender_comp_id="BENCH_SEQUENTIAL",
            timeout=2,
        ) as client:
            started = time.perf_counter()
            for index in range(sequential_orders):
                client.send_new_order(
                    f"BENCH-SEQ-{index}", "AAPL", "1", 10, "200"
                )
            sequential_elapsed = time.perf_counter() - started
            sequential_latencies = [
                sample.latency_ms for sample in client.latency_samples
            ]

        orders = [
            {
                "cl_ord_id": f"BENCH-CONCURRENT-{index}",
                "symbol": "MSFT",
                "side": "2",
                "quantity": 10,
                "price": "400",
            }
            for index in range(concurrent_orders)
        ]
        concurrent_client = FixClient(
            exchange.host,
            exchange.port,
            sender_comp_id="BENCH_CONCURRENT",
            timeout=3,
        )
        started = time.perf_counter()
        concurrent_results = asyncio.run(
            concurrent_client.send_orders_concurrently(
                orders, max_concurrency=concurrency
            )
        )
        concurrent_elapsed = time.perf_counter() - started

    return {
        "sequential": {
            "orders": sequential_orders,
            "wall_time_seconds": sequential_elapsed,
            "throughput_orders_per_second": sequential_orders / sequential_elapsed,
            "latency_ms": summarize_samples(sequential_latencies),
        },
        "concurrent": {
            "orders": concurrent_orders,
            "max_concurrency": concurrency,
            "wall_time_seconds": concurrent_elapsed,
            "throughput_orders_per_second": concurrent_orders / concurrent_elapsed,
            "latency_ms": summarize_samples(
                [result.latency_ms for result in concurrent_results]
            ),
        },
    }


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    return value


def run_benchmarks(
    *,
    micro_iterations: int = 10_000,
    market_iterations: int = 1_000,
    rounds: int = 7,
    sequential_orders: int = 200,
    concurrent_orders: int = 100,
    concurrency: int = 20,
) -> dict[str, Any]:
    """Run all local benchmarks and return a JSON-serializable result."""

    positive_values = {
        "micro_iterations": micro_iterations,
        "market_iterations": market_iterations,
        "rounds": rounds,
        "sequential_orders": sequential_orders,
        "concurrent_orders": concurrent_orders,
        "concurrency": concurrency,
    }
    if any(value <= 0 for value in positive_values.values()):
        raise ValueError("All benchmark workload values must be greater than zero")

    process = psutil.Process()
    metadata = {
        "generated_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "processor": platform.processor() or os.getenv("PROCESSOR_IDENTIFIER", "unknown"),
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "system_memory_gib": psutil.virtual_memory().total / (1024**3),
        "process_rss_mib_at_start": process.memory_info().rss / (1024**2),
    }
    result = {
        "metadata": metadata,
        "configuration": positive_values,
        "microbenchmarks": _microbenchmarks(
            iterations=micro_iterations,
            market_iterations=market_iterations,
            rounds=rounds,
        ),
        "tcp_order_entry": _tcp_benchmarks(
            sequential_orders=sequential_orders,
            concurrent_orders=concurrent_orders,
            concurrency=concurrency,
        ),
        "notes": [
            "Local loopback benchmark; not a production capacity claim.",
            "Mock exchange artificial processing delay was disabled for TCP measurements.",
            "Concurrent latency excludes Logon but batch throughput includes connection setup.",
            "95% confidence intervals describe the sampled mean, not future-tail guarantees.",
        ],
    }
    return _rounded(result)


def format_benchmark_summary(result: dict[str, Any]) -> str:
    lines = [
        "Local FIX gateway benchmark",
        "+----------------------+------------+------------+----------------+",
        "| In-process operation | mean us/op | p95 us/op | median ops/s   |",
        "+----------------------+------------+------------+----------------+",
    ]
    for name, metrics in result["microbenchmarks"].items():
        latency = metrics["latency_us_per_operation"]
        throughput = metrics["throughput_operations_per_second"]
        lines.append(
            f"| {name:<20} | {latency['mean']:>10.3f} | "
            f"{latency['p95']:>10.3f} | {throughput['p50']:>14,.0f} |"
        )
    lines.extend(
        [
            "+----------------------+------------+------------+----------------+",
            "",
            "+------------+--------+----------+----------+----------+----------+------------+",
            "| TCP mode   | orders | p50 ms   | p95 ms   | p99 ms   | mean ms  | orders/s   |",
            "+------------+--------+----------+----------+----------+----------+------------+",
        ]
    )
    for name, metrics in result["tcp_order_entry"].items():
        latency = metrics["latency_ms"]
        lines.append(
            f"| {name:<10} | {metrics['orders']:>6} | {latency['p50']:>8.3f} | "
            f"{latency['p95']:>8.3f} | {latency['p99']:>8.3f} | "
            f"{latency['mean']:>8.3f} | "
            f"{metrics['throughput_orders_per_second']:>10.1f} |"
        )
    lines.append(
        "+------------+--------+----------+----------+----------+----------+------------+"
    )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--micro-iterations", type=int, default=10_000)
    parser.add_argument("--market-iterations", type=int, default=1_000)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--sequential-orders", type=int, default=200)
    parser.add_argument("--concurrent-orders", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = run_benchmarks(
        micro_iterations=args.micro_iterations,
        market_iterations=args.market_iterations,
        rounds=args.rounds,
        sequential_orders=args.sequential_orders,
        concurrent_orders=args.concurrent_orders,
        concurrency=args.concurrency,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(format_benchmark_summary(result))
    print(f"\nMachine-readable results: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
