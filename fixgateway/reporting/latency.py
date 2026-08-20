"""Latency percentile calculation and persisted-baseline comparison."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class LatencyRegressionError(AssertionError):
    """Raised when measured p99 latency exceeds the configured regression budget."""


@dataclass(frozen=True)
class BaselineComparison:
    action: str
    baseline_p99: float
    current_p99: float
    allowed_p99: float


def _percentile(sorted_samples: list[float], percentile: float) -> float:
    """Calculate a linearly interpolated percentile."""

    if not sorted_samples:
        raise ValueError("At least one latency sample is required")
    position = (len(sorted_samples) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_samples[lower]
    fraction = position - lower
    return sorted_samples[lower] + (
        sorted_samples[upper] - sorted_samples[lower]
    ) * fraction


def calculate_latency_percentiles(samples_ms: list[float]) -> dict[str, float | int]:
    if not samples_ms:
        raise ValueError("At least one latency sample is required")
    if any(not math.isfinite(sample) or sample < 0 for sample in samples_ms):
        raise ValueError("Latency samples must be finite, non-negative values")
    ordered = sorted(float(sample) for sample in samples_ms)
    return {
        "sample_count": len(ordered),
        "p50": _percentile(ordered, 0.50),
        "p99": _percentile(ordered, 0.99),
        "p99_9": _percentile(ordered, 0.999),
        "max": ordered[-1],
    }


def format_latency_table(metrics: dict[str, float | int]) -> str:
    rows = [
        ("p50", float(metrics["p50"])),
        ("p99", float(metrics["p99"])),
        ("p99.9", float(metrics["p99_9"])),
        ("max", float(metrics["max"])),
    ]
    lines = [
        "+------------+--------------+",
        "| Percentile | Latency (ms) |",
        "+------------+--------------+",
    ]
    lines.extend(f"| {name:<10} | {value:>12.3f} |" for name, value in rows)
    lines.append("+------------+--------------+")
    lines.append(f"Samples: {int(metrics['sample_count'])}")
    return "\n".join(lines)


def _baseline_payload(metrics: dict[str, float | int]) -> dict[str, Any]:
    return {
        **metrics,
        "unit": "milliseconds",
        "updated_at": datetime.now(UTC).isoformat(),
    }


def save_latency_baseline(
    baseline_path: str | Path, metrics: dict[str, float | int]
) -> None:
    path = Path(baseline_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_baseline_payload(metrics), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def compare_latency_baseline(
    metrics: dict[str, float | int],
    baseline_path: str | Path,
    *,
    regression_threshold: float = 0.20,
    update: bool = False,
) -> BaselineComparison:
    """Create, update, or compare a p99 latency baseline.

    ``regression_threshold`` is a fractional allowance: ``0.20`` permits a current
    p99 up to 20 percent above the saved p99.
    """

    if regression_threshold < 0:
        raise ValueError("regression_threshold must be non-negative")
    path = Path(baseline_path)
    current_p99 = float(metrics["p99"])

    if update or not path.exists():
        action = "updated" if path.exists() else "created"
        save_latency_baseline(path, metrics)
        return BaselineComparison(action, current_p99, current_p99, current_p99)

    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
        baseline_p99 = float(baseline["p99"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid latency baseline file {path}: {exc}") from exc
    allowed_p99 = baseline_p99 * (1 + regression_threshold)
    comparison = BaselineComparison(
        "compared", baseline_p99, current_p99, allowed_p99
    )
    if current_p99 > allowed_p99:
        degradation = (
            math.inf
            if baseline_p99 == 0
            else ((current_p99 / baseline_p99) - 1) * 100
        )
        raise LatencyRegressionError(
            f"p99 latency regression: current {current_p99:.3f} ms exceeds "
            f"baseline {baseline_p99:.3f} ms by {degradation:.1f}% "
            f"(allowed {regression_threshold * 100:.1f}%, limit {allowed_p99:.3f} ms)"
        )
    return comparison

