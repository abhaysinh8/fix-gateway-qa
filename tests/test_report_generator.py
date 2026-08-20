from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fixgateway.reporting.report_generator import generate_report
from fixgateway.surveillance.pattern_detector import SpoofingLikeMatch


def test_generate_report_renders_all_sections_and_escapes_content(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "report.html"
    results = {
        "timestamp": "2026-08-20T14:30:00+00:00",
        "tests": {"total": 12, "passed": 11, "failed": 1},
        "latency": {
            "sample_count": 200,
            "p50": 3.2,
            "p99": 6.4,
            "p99_9": 8.5,
            "max": 9.0,
        },
        "surveillance_flags": [
            SpoofingLikeMatch(
                pattern="spoofing-like",
                order_id="ORDER-<script>",
                symbol="AAPL",
                trader_id="T-1",
                placed_at_ms=100,
                canceled_at_ms=107,
                elapsed_ms=7,
                quantity=Decimal("100"),
                price=Decimal("200.50"),
            )
        ],
    }

    returned_path = generate_report(results, destination)
    html = destination.read_text(encoding="utf-8")

    assert returned_path == destination
    assert "FIX Gateway QA Report" in html
    assert "<strong>11</strong>" in html
    assert "6.400 ms" in html
    assert "spoofing-like" in html
    assert "ORDER-&lt;script&gt;" in html
    assert "ORDER-<script>" not in html


def test_generate_report_supports_empty_surveillance_results(tmp_path: Path) -> None:
    destination = generate_report(
        {
            "total_tests": 5,
            "passed": 5,
            "failed": 0,
            "latency_percentiles": {},
            "surveillance": [],
        },
        tmp_path / "report.html",
    )

    html = destination.read_text(encoding="utf-8")
    assert "No heuristic pattern matches were raised" in html
    assert "status-pass" in html


def test_generate_report_renders_unified_subsystem_evidence(tmp_path: Path) -> None:
    destination = generate_report(
        {
            "tests": {"total": 70, "passed": 70, "failed": 0},
            "subsystems": [
                {"name": "Market data", "passed": True, "details": "Gap detected"}
            ],
            "latency_baseline": {
                "passed": True,
                "action": "compared",
                "baseline_p99_ms": 5.0,
                "allowed_p99_ms": 6.0,
            },
            "precision": {
                "passed": True,
                "summary": "Exact Decimal arithmetic",
                "highlights": [
                    {
                        "operation": "0.1 × 3",
                        "decimal_result": "0.3",
                        "naive_result": "0.30000000000000004",
                        "observation": "float drift",
                    }
                ],
            },
            "audit": {"is_complete": True, "entry_count": 12, "issues": []},
            "market_data": {
                "passed": True,
                "ordered_status": "PASS",
                "gap_detected": True,
                "request_resync": True,
            },
            "chaos": {"passed": True, "duplicates_detected": 4},
            "soak": {
                "passed": True,
                "total_requests": 20,
                "samples": [
                    {
                        "elapsed_seconds": 1,
                        "memory_mb": 50,
                        "open_connections": 1,
                        "requests_completed": 20,
                        "p50_ms": 1.2,
                        "p99_ms": 2.3,
                    }
                ],
            },
        },
        tmp_path / "unified.html",
    )

    html = destination.read_text(encoding="utf-8")
    for heading in (
        "Numerical precision and rounding",
        "Audit trail completeness",
        "Market data conformance and gap recovery",
        "Chaos, retries, and idempotency",
        "Short soak-test time series",
    ):
        assert heading in html
    assert "0.30000000000000004" in html
    assert "Gap detected" in html
