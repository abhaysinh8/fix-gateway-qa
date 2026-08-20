"""Run the complete twelve-phase fix-gateway-qa demonstration."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fixgateway.audit.audit_checker import check_completeness, cross_check_against_state_machine
from fixgateway.client.fix_client import FixClient
from fixgateway.exchange.mock_exchange import MockExchange
from fixgateway.exchange.state_machine import OrderState, TERMINAL_STATES
from fixgateway.fix.precision import (
    average_fill_price,
    naive_average_fill_price,
    naive_notional,
    naive_pnl,
    naive_round_currency,
    notional,
    pnl,
    round_currency,
)
from fixgateway.marketdata.conformance_checker import feed_replay
from fixgateway.marketdata.md_message import (
    MDEntryType,
    MDUpdateAction,
    MarketDataEntry,
    MarketDataIncrementalRefresh,
    MarketDataSnapshotFullRefresh,
)
from fixgateway.reporting.latency import (
    LatencyRegressionError,
    calculate_latency_percentiles,
    compare_latency_baseline,
    format_latency_table,
)
from fixgateway.reporting.report_generator import generate_report
from fixgateway.reporting.run_latency_check import measure_orders
from fixgateway.reporting.soak_runner import SoakConfig, run_soak
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


def _timeline_event(order_id: str, action: str, timestamp_ms: float) -> dict[str, object]:
    return {
        "order_id": order_id,
        "action": action,
        "timestamp": timestamp_ms,
        "quantity": 100,
        "price": 200,
        "symbol": "AAPL",
        "trader_id": "DEMO_TRADER",
    }


def _precision_demo() -> dict[str, Any]:
    decimal_notional = notional("0.1", "3")
    float_notional = naive_notional(0.1, 3)
    decimal_currency = round_currency("2.675")
    float_currency = naive_round_currency(2.675)
    decimal_average = average_fill_price([("100.10", "2"), ("100.30", "1")])
    float_average = naive_average_fill_price([(100.10, 2), (100.30, 1)])
    decimal_pnl = pnl("100.10", "100.30", "100", "buy")
    float_pnl = naive_pnl(100.10, 100.30, 100, "buy")
    passed = (
        decimal_notional == Decimal("0.3")
        and float_notional != float(decimal_notional)
        and decimal_currency == Decimal("2.68")
        and float_currency == 2.67
        and decimal_pnl == Decimal("20.00")
    )
    return {
        "passed": passed,
        "summary": "Decimal arithmetic remained exact while binary-float drift was made visible.",
        "highlights": [
            {
                "operation": "Notional: 0.1 × 3",
                "decimal_result": str(decimal_notional),
                "naive_result": repr(float_notional),
                "observation": "Decimal is exactly 0.3; float retains a binary representation error.",
            },
            {
                "operation": "Currency: 2.675 to cents",
                "decimal_result": str(decimal_currency),
                "naive_result": repr(float_currency),
                "observation": "Explicit half-even Decimal rounding avoids the float input artifact.",
            },
            {
                "operation": "Weighted average fill price",
                "decimal_result": str(decimal_average),
                "naive_result": repr(float_average),
                "observation": "The Decimal result preserves source notional at high precision.",
            },
            {
                "operation": "Buy PnL: 100.10 → 100.30 × 100",
                "decimal_result": str(decimal_pnl),
                "naive_result": repr(float_pnl),
                "observation": "Fixed-point PnL is exactly 20.00.",
            },
        ],
    }


def _market_data_demo() -> dict[str, Any]:
    def entry(
        entry_type: MDEntryType,
        price: str,
        size: str,
        action: MDUpdateAction | None = None,
    ) -> MarketDataEntry:
        return MarketDataEntry(entry_type, price, size, action)

    messages = [
        MarketDataSnapshotFullRefresh(
            "AAPL",
            1,
            [
                entry(MDEntryType.BID, "100.00", "10"),
                entry(MDEntryType.OFFER, "101.00", "12"),
            ],
        ),
        MarketDataIncrementalRefresh(
            "AAPL", 2, [entry(MDEntryType.BID, "100.00", "15", MDUpdateAction.CHANGE)]
        ),
        MarketDataIncrementalRefresh(
            "AAPL", 3, [entry(MDEntryType.OFFER, "101.00", "0", MDUpdateAction.DELETE)]
        ),
        MarketDataIncrementalRefresh(
            "AAPL", 4, [entry(MDEntryType.OFFER, "101.50", "8", MDUpdateAction.NEW)]
        ),
    ]
    ordered = feed_replay([message.encode() for message in messages])
    dropped = feed_replay([messages[0], messages[1], messages[3]])
    final_book = ordered.final_books["AAPL"]
    ordered_passed = (
        not ordered.sequence_gaps
        and not ordered.request_resync
        and ordered.all_checksums_valid
        and final_book.bids == {Decimal("100.00"): Decimal("15")}
        and final_book.asks == {Decimal("101.50"): Decimal("8")}
    )
    gap_passed = (
        dropped.request_resync
        and dropped.stale_symbols == {"AAPL"}
        and len(dropped.sequence_gaps) == 1
        and dropped.sequence_gaps[0].first_missing == 3
    )
    return {
        "passed": ordered_passed and gap_passed,
        "summary": "Ordered replay reconstructed the book; a deliberate packet loss made it stale and requested snapshot resync.",
        "ordered_status": "PASS" if ordered_passed else "FAIL",
        "gap_detected": bool(dropped.sequence_gaps),
        "request_resync": dropped.request_resync,
        "checksum_status": "PASS" if ordered.all_checksums_valid else "FAIL",
        "gap_ranges": [
            f"{gap.symbol} {gap.first_missing}-{gap.last_missing}"
            for gap in dropped.sequence_gaps
        ],
        "final_book": (
            f"AAPL bids={dict(final_book.bids)}, asks={dict(final_book.asks)}, "
            f"last_seq={final_book.last_seq_num}"
        ),
    }


def _chaos_demo() -> dict[str, Any]:
    details: list[str] = []
    with MockExchange(
        processing_delay_ms=(0, 0),
        fill_strategy="two_partial_then_full",
        chaos_mode=True,
        chaos_rate=0,
        chaos_duplicate_rate=1,
        chaos_reorder_rate=0,
        random_seed=5,
    ) as exchange:
        with FixClient(exchange.host, exchange.port, timeout=0.5) as client:
            client.send_new_order("DEMO-DUPLICATE", "AAPL", "1", 100, "200")
            client.receive_message()
            client.receive_message()
            client.receive_message()
            duplicate_count = len(client.detected_duplicates)
            duplicate_safe = (
                client.order_states["DEMO-DUPLICATE"].state is OrderState.FILLED
                and not client.processing_errors
                and duplicate_count >= 3
            )
    details.append(f"Duplicate ExecutionReports ignored: {duplicate_count}.")

    with MockExchange(
        processing_delay_ms=(0, 0),
        fill_strategy="resting",
        chaos_mode=True,
        chaos_rate=0,
        chaos_duplicate_rate=0,
        chaos_reorder_rate=1,
        chaos_reorder_hold_ms=500,
        random_seed=9,
    ) as exchange:
        with FixClient(exchange.host, exchange.port, timeout=1) as client:
            client.send_new_order_nowait("DEMO-REORDER-1", "MSFT", "1", 10, "400")
            client.send_new_order_nowait("DEMO-REORDER-2", "NVDA", "2", 20, "180")
            arrival_order = [client.receive_message().get(11), client.receive_message().get(11)]
            reorder_safe = (
                arrival_order == ["DEMO-REORDER-2", "DEMO-REORDER-1"]
                and all(machine.state is OrderState.NEW for machine in client.order_states.values())
                and not client.processing_errors
            )
    details.append(f"Cross-order arrival order: {arrival_order}.")

    with MockExchange(
        processing_delay_ms=(0, 0),
        fill_strategy="full",
        chaos_mode=True,
        chaos_rate=0.5,
        chaos_drop_probability=1,
        chaos_duplicate_rate=0,
        chaos_reorder_rate=0,
        random_seed=31,
    ) as exchange:
        with FixClient(exchange.host, exchange.port, timeout=0.05, max_retries=1) as client:
            client.send_new_order("DEMO-RETRY", "IBM", "1", 25, "250")
            client.receive_message()
            retry_count = client.retry_attempts["DEMO-RETRY"]
            retry_state = client.order_states["DEMO-RETRY"].state
        retry_orders = exchange.order_book.orders
        retry_safe = (
            retry_count == 1
            and retry_state is OrderState.FILLED
            and list(retry_orders) == ["DEMO-RETRY"]
            and retry_orders["DEMO-RETRY"].cumulative_quantity == 25
        )
    details.append(f"Timed-out NewOrderSingle retried {retry_count} time and created one order.")
    return {
        "passed": duplicate_safe and reorder_safe and retry_safe,
        "summary": "Forced duplicates, cross-order reordering, and a dropped-response retry completed without state corruption.",
        "duplicates_detected": duplicate_count,
        "reorder_safe": reorder_safe,
        "orders_created": len(retry_orders),
        "details": details,
    }


def _soak_demo(duration_seconds: float) -> dict[str, Any]:
    config = SoakConfig(
        duration_seconds=duration_seconds,
        requests_per_second=float(os.getenv("DEMO_SOAK_RPS", "12")),
        sample_interval_seconds=max(0.5, min(2.0, duration_seconds / 3)),
        burst_multiplier=2,
        client_timeout_seconds=1,
        sender_comp_id="DEMO_SOAK_CLIENT",
    )
    with MockExchange(
        processing_delay_ms=(0.5, 1.5), fill_strategy="resting", random_seed=73
    ) as exchange:
        result = run_soak(config, exchange=exchange)
    print(result.format_summary())
    passed = (
        not result.errors
        and result.memory_growth_percent
        <= float(os.getenv("DEMO_SOAK_MEMORY_GROWTH_LIMIT", "25"))
        and result.latency_degradation_percent
        <= float(os.getenv("DEMO_SOAK_LATENCY_DEGRADATION_LIMIT", "200"))
    )
    return {
        "passed": passed,
        "summary": (
            f"{result.actual_duration_seconds:.1f}s duration-based smoke soak; "
            "production soak campaigns would run for hours."
        ),
        "total_requests": result.total_requests,
        "error_count": len(result.errors),
        "memory_growth_percent": round(result.memory_growth_percent, 2),
        "latency_degradation_percent": round(result.latency_degradation_percent, 2),
        "samples": [asdict(sample) for sample in result.samples],
    }


def run_demo(*, chaos_mode: bool = False, soak_seconds: float = 6.0) -> int:
    print("fix-gateway-qa twelve-phase end-to-end demo")
    print("\n[1/8] Full pytest suite")
    total_tests, passed_tests, failed_tests = _pytest_summary()
    subsystem_rows: list[dict[str, Any]] = []

    def record(name: str, passed: bool, details: str) -> None:
        subsystem_rows.append({"name": name, "passed": passed, "details": details})

    record(
        "Automated test suite",
        failed_tests == 0 and passed_tests > 0,
        f"{passed_tests}/{total_tests} tests passed.",
    )
    print("\n[2/8] Live FIX lifecycle, latency, surveillance, and audit")
    timeline: list[dict[str, object]] = []

    def fill_strategy(order):
        if order.order_id == "DEMO-FULL":
            return [order.quantity]
        if order.order_id == "DEMO-AUDIT":
            return [25, 75]
        return []

    with MockExchange(
        processing_delay_ms=(1.0, 3.0),
        fill_strategy=fill_strategy,
        chaos_mode=chaos_mode,
        chaos_rate=0.02,
        chaos_drop_probability=0.25,
        chaos_extra_delay_ms=(10, 30),
        chaos_duplicate_rate=0.05,
        chaos_reorder_rate=0.05,
        random_seed=42,
    ) as exchange:
        with FixClient(
            exchange.host,
            exchange.port,
            sender_comp_id="DEMO_CLIENT",
            timeout=1,
            max_retries=2,
        ) as client:
            acknowledgement = client.send_new_order("DEMO-FULL", "MSFT", "1", 100, "400")
            full_fill = client.receive_message()
            lifecycle_passed = acknowledgement.get(39) == "0" and full_fill.get(39) == "2"
            audit_ack = client.send_new_order("DEMO-AUDIT", "AAPL", "1", 100, "200")
            audit_partial = client.receive_message()
            audit_fill = client.receive_message()
            lifecycle_passed = lifecycle_passed and (
                audit_ack.get(39) == "0"
                and audit_partial.get(39) == "1"
                and audit_fill.get(39) == "2"
            )
            rapid_cancels_passed = True
            for index in range(6):
                order_id = f"DEMO-RAPID-{index}"
                placed_at = time.time_ns() / 1_000_000
                ack = client.send_new_order(order_id, "AAPL", "1", 100, "200")
                timeline.append(_timeline_event(order_id, "place", placed_at))
                canceled_at = time.time_ns() / 1_000_000
                cancel = client.send_cancel(f"CANCEL-{order_id}", order_id, "AAPL", "1")
                timeline.append(_timeline_event(order_id, "cancel", canceled_at))
                rapid_cancels_passed = rapid_cancels_passed and (
                    ack.get(39) == "0" and cancel.get(39) == "4"
                )
            latency_samples = measure_orders(
                client, int(os.getenv("DEMO_LATENCY_ORDER_COUNT", "50"))
            )
        audit_log = exchange.audit_log
        live_orders = exchange.order_book.orders

    record(
        "FIX order lifecycle",
        lifecycle_passed and rapid_cancels_passed,
        "Full fill, partial-to-full, and cancel flows completed over TCP.",
    )
    latency = calculate_latency_percentiles(latency_samples)
    print(format_latency_table(latency))
    threshold = float(os.getenv("LATENCY_REGRESSION_THRESHOLD", "0.20"))
    try:
        comparison = compare_latency_baseline(
            latency,
            BASELINE_PATH,
            regression_threshold=threshold,
            update=os.getenv("UPDATE_BASELINE") == "1",
        )
    except LatencyRegressionError as exc:
        baseline_payload = (
            json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
            if BASELINE_PATH.exists()
            else {}
        )
        baseline_p99 = float(baseline_payload.get("p99", 0))
        latency_baseline = {
            "passed": False,
            "action": "regression detected",
            "baseline_p99_ms": round(baseline_p99, 3),
            "allowed_p99_ms": round(baseline_p99 * (1 + threshold), 3),
            "details": str(exc),
        }
    else:
        latency_baseline = {
            "passed": True,
            "action": comparison.action,
            "baseline_p99_ms": round(comparison.baseline_p99, 3),
            "allowed_p99_ms": round(comparison.allowed_p99, 3),
            "details": "Current p99 is within the configured regression budget.",
        }
    record(
        "Latency regression",
        bool(latency_baseline["passed"]),
        f"p99={float(latency['p99']):.3f} ms; baseline action={latency_baseline['action']}.",
    )

    spoofing_matches = detect_quote_stuffing_or_spoofing(
        timeline, window_ms=1_000, cancel_threshold_ms=100
    )
    layering_matches = detect_high_cancel_ratio(
        timeline, window_ms=2_000, ratio_threshold=0.40, min_actions=5
    )
    surveillance_flags = list(spoofing_matches)
    if layering_matches:
        surveillance_flags.append(max(layering_matches, key=lambda match: match.cancel_ratio))
    record(
        "Surveillance heuristics",
        bool(spoofing_matches) and bool(layering_matches),
        f"{len(spoofing_matches)} spoofing-like and {len(layering_matches)} layering-like matches.",
    )

    audit_result = check_completeness(audit_log, live_orders)
    cross_checks = all(
        cross_check_against_state_machine(audit_log, order_id, order.state_machine)
        for order_id, order in live_orders.items()
    )
    audit_data = {
        "is_complete": audit_result.is_complete and cross_checks,
        "summary": "Wire-log reconstruction matched every live order state machine.",
        "entry_count": len(audit_log),
        "orders_checked": len(live_orders),
        "open_orders": sum(order.state not in TERMINAL_STATES for order in live_orders.values()),
        "issues": audit_result.issues
        + ([] if cross_checks else ["At least one state-machine cross-check failed"]),
    }
    record(
        "Audit completeness",
        bool(audit_data["is_complete"]),
        f"{len(audit_log)} entries reconstructed across {len(live_orders)} orders.",
    )

    print("\n[3/8] Decimal precision regression examples")
    precision_data = _precision_demo()
    record("Numerical precision", precision_data["passed"], precision_data["summary"])
    print("\n[4/8] Market data ordered replay and deliberate gap")
    market_data = _market_data_demo()
    record("Market data conformance", market_data["passed"], market_data["summary"])
    print("\n[5/8] Forced chaos/idempotency scenarios")
    chaos_data = _chaos_demo()
    record("Chaos and idempotency", chaos_data["passed"], chaos_data["summary"])
    print(f"\n[6/8] Duration-based soak sample ({soak_seconds:.1f}s)")
    soak_data = _soak_demo(soak_seconds)
    record("Soak health", soak_data["passed"], soak_data["summary"])

    print("\n[7/8] Unified HTML evidence report")
    generate_report(
        {
            "timestamp": datetime.now(UTC),
            "tests": {"total": total_tests, "passed": passed_tests, "failed": failed_tests},
            "subsystems": subsystem_rows,
            "latency": latency,
            "latency_baseline": latency_baseline,
            "precision": precision_data,
            "audit": audit_data,
            "market_data": market_data,
            "chaos": chaos_data,
            "soak": soak_data,
            "surveillance_flags": surveillance_flags,
        },
        REPORT_PATH,
    )
    print("\n[8/8] Final subsystem summary")
    for row in subsystem_rows:
        marker = "PASS" if row["passed"] else "FAIL"
        print(f"  {marker:4}  {row['name']}: {row['details']}")
    print(f"\nUnified report: {REPORT_PATH}")
    return 0 if all(row["passed"] for row in subsystem_rows) else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chaos",
        action="store_true",
        default=os.getenv("DEMO_CHAOS_MODE") == "1",
        help="enable low-rate chaos on the main mixed order flow",
    )
    parser.add_argument(
        "--soak-seconds",
        type=float,
        default=float(os.getenv("DEMO_SOAK_SECONDS", "6")),
        help="duration of the demo soak sample (default: 6 seconds)",
    )
    args = parser.parse_args()
    if args.soak_seconds <= 0:
        parser.error("--soak-seconds must be greater than zero")
    return args


if __name__ == "__main__":
    arguments = _parse_args()
    raise SystemExit(run_demo(chaos_mode=arguments.chaos, soak_seconds=arguments.soak_seconds))
