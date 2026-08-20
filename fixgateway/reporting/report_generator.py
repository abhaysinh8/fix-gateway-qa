"""Generate a self-contained HTML summary of FIX gateway QA results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape


TEMPLATE_DIRECTORY = Path(__file__).with_name("templates")


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{name} must be a mapping or dataclass")


def _format_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float, Decimal)):
        if float(value) >= 100_000_000_000:
            return datetime.fromtimestamp(float(value) / 1_000, UTC).isoformat()
        return f"{float(value):.3f} ms"
    return str(value)


def _format_detail(key: str, value: Any) -> str:
    if key in {"placed_at_ms", "window_start_ms"}:
        return _format_timestamp(value)
    if key.endswith("_ms") and isinstance(value, (int, float, Decimal)):
        return f"{float(value):.3f} ms"
    if "ratio" in key and isinstance(value, (int, float, Decimal)):
        return f"{float(value):.3f}"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _surveillance_rows(flags: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for flag in flags or []:
        data = _mapping(flag, "surveillance flag")
        timestamp = data.get(
            "timestamp",
            data.get("canceled_at_ms", data.get("window_end_ms", "—")),
        )
        excluded = {
            "order_id",
            "pattern",
            "pattern_type",
            "timestamp",
            "canceled_at_ms",
            "window_end_ms",
            "details",
        }
        details = data.get("details")
        if details is None:
            details = ", ".join(
                f"{key}={_format_detail(key, value)}"
                for key, value in data.items()
                if key not in excluded
            )
        rows.append(
            {
                "order_id": str(data.get("order_id", "—")),
                "pattern": str(
                    data.get("pattern", data.get("pattern_type", "unspecified"))
                ),
                "timestamp": _format_timestamp(timestamp),
                "details": str(details),
            }
        )
    return rows


def _latency_rows(latency: Mapping[str, Any]) -> list[dict[str, float | str]]:
    values = [
        ("p50", float(latency.get("p50", 0))),
        ("p99", float(latency.get("p99", 0))),
        ("p99.9", float(latency.get("p99_9", latency.get("p99.9", 0)))),
        ("max", float(latency.get("max", 0))),
    ]
    maximum = max((value for _, value in values), default=0)
    return [
        {
            "label": label,
            "value": value,
            "width": 0 if maximum == 0 else max(2, value / maximum * 100),
        }
        for label, value in values
    ]


def _optional_mapping(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    return _mapping(value, f"results['{key}']") if value else {}


def _display_status(passed: Any, *, empty: str = "NOT RUN") -> str:
    if passed is None:
        return empty
    return "PASS" if bool(passed) else "FAIL"


def _subsystem_rows(data: Mapping[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in data.get("subsystems", []):
        subsystem = _mapping(item, "subsystem")
        passed = subsystem.get("passed")
        rows.append(
            {
                "name": str(subsystem.get("name", "Unnamed subsystem")),
                "status": _display_status(passed),
                "status_class": "pass" if passed else "fail",
                "details": str(subsystem.get("details", "")),
            }
        )
    return rows


def _precision_rows(precision: Mapping[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in precision.get("highlights", []):
        highlight = _mapping(item, "precision highlight")
        rows.append(
            {
                "operation": str(highlight.get("operation", "")),
                "decimal_result": str(highlight.get("decimal_result", "")),
                "naive_result": str(highlight.get("naive_result", "")),
                "observation": str(highlight.get("observation", "")),
            }
        )
    return rows


def _soak_rows(soak: Mapping[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in soak.get("samples", []):
        sample = _mapping(item, "soak sample")
        rows.append(
            {
                "elapsed": f"{float(sample.get('elapsed_seconds', 0)):.1f}",
                "memory": f"{float(sample.get('memory_mb', 0)):.2f}",
                "connections": str(sample.get("open_connections", 0)),
                "requests": str(
                    sample.get("requests_completed", sample.get("request_count", 0))
                ),
                "p50": f"{float(sample.get('p50_ms', 0)):.3f}",
                "p99": f"{float(sample.get('p99_ms', 0)):.3f}",
            }
        )
    return rows


def generate_report(
    results: Mapping[str, Any] | Any,
    output_path: str | Path = "reports/report.html",
) -> Path:
    """Render structured test, latency, and surveillance results as HTML.

    Accepted keys include ``tests``, ``latency``, ``latency_baseline``, ``precision``,
    ``audit``, ``market_data``, ``chaos``, ``soak``, ``surveillance_flags``, and
    optional ``subsystems``/``timestamp``. For convenience, test counts may instead be
    provided as top-level ``total_tests``, ``passed``, and ``failed``.
    """

    data = _mapping(results, "results")
    test_data = _mapping(data.get("tests", {}), "results['tests']")
    passed = int(test_data.get("passed", data.get("passed", 0)))
    failed = int(test_data.get("failed", data.get("failed", 0)))
    total = int(
        test_data.get("total", data.get("total_tests", data.get("total", passed + failed)))
    )
    if min(total, passed, failed) < 0 or passed + failed > total:
        raise ValueError("Test counts must be non-negative and passed + failed <= total")

    latency = _mapping(
        data.get("latency", data.get("latency_percentiles", {})),
        "results['latency']",
    )
    timestamp = data.get("timestamp", datetime.now(UTC))
    if isinstance(timestamp, datetime):
        timestamp = timestamp.isoformat()

    baseline = _optional_mapping(data, "latency_baseline")
    precision = _optional_mapping(data, "precision")
    audit = _optional_mapping(data, "audit")
    market_data = _optional_mapping(data, "market_data")
    chaos = _optional_mapping(data, "chaos")
    soak = _optional_mapping(data, "soak")

    context = {
        "generated_at": str(timestamp),
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": total - passed - failed,
        "success_rate": 0 if total == 0 else passed / total * 100,
        "status": "PASS" if failed == 0 else "FAIL",
        "latency_rows": _latency_rows(latency),
        "sample_count": int(latency.get("sample_count", 0)),
        "baseline": baseline,
        "baseline_status": _display_status(
            baseline.get("passed") if baseline else None
        ),
        "precision": precision,
        "precision_status": _display_status(
            precision.get("passed") if precision else None
        ),
        "precision_rows": _precision_rows(precision),
        "audit": audit,
        "audit_status": _display_status(audit.get("is_complete") if audit else None),
        "audit_issues": [str(issue) for issue in audit.get("issues", [])],
        "market_data": market_data,
        "market_status": _display_status(
            market_data.get("passed") if market_data else None
        ),
        "market_gaps": [str(gap) for gap in market_data.get("gap_ranges", [])],
        "chaos": chaos,
        "chaos_status": _display_status(chaos.get("passed") if chaos else None),
        "chaos_details": [str(detail) for detail in chaos.get("details", [])],
        "soak": soak,
        "soak_status": _display_status(soak.get("passed") if soak else None),
        "soak_rows": _soak_rows(soak),
        "subsystem_rows": _subsystem_rows(data),
        "surveillance_rows": _surveillance_rows(
            data.get("surveillance_flags", data.get("surveillance", []))
        ),
    }

    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIRECTORY),
        autoescape=select_autoescape(("html", "xml", "j2"), default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = environment.get_template("report.html.j2").render(**context)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")
    return destination
