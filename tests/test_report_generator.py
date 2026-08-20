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

