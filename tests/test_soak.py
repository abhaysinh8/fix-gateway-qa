from __future__ import annotations

import os

from fixgateway.exchange.mock_exchange import MockExchange
from fixgateway.reporting.soak_runner import SoakConfig, run_soak


def test_short_soak_has_stable_memory_connections_and_latency() -> None:
    # This 30-second run is a CI/demo smoke proxy. A production-grade soak would run
    # for hours, replay venue-specific traffic curves, retain longer rolling windows,
    # and send memory/latency/connection alerts to an observability platform.
    duration = float(os.getenv("SOAK_DURATION_SECONDS", "30"))
    memory_growth_limit = float(os.getenv("SOAK_MEMORY_GROWTH_PERCENT", "25"))
    latency_degradation_limit = float(
        os.getenv("SOAK_LATENCY_DEGRADATION_PERCENT", "75")
    )
    config = SoakConfig(
        duration_seconds=duration,
        requests_per_second=float(os.getenv("SOAK_REQUESTS_PER_SECOND", "12")),
        sample_interval_seconds=5,
        burst_multiplier=2,
        client_timeout_seconds=1,
    )

    with MockExchange(
        processing_delay_ms=(1, 1),
        fill_strategy="resting",
        random_seed=101,
    ) as exchange:
        result = run_soak(config, exchange=exchange)

    print("\n" + result.format_summary())
    assert result.errors == []
    assert len(result.samples) >= 5
    assert result.total_requests > duration * config.requests_per_second
    assert all(sample.open_connections == 1 for sample in result.samples)
    assert result.memory_growth_percent <= memory_growth_limit, (
        f"RSS grew {result.memory_growth_percent:.2f}%, above the "
        f"{memory_growth_limit:.2f}% short-soak threshold"
    )
    assert result.latency_degradation_percent <= latency_degradation_limit, (
        f"Final-third p99 degraded {result.latency_degradation_percent:.2f}%, above "
        f"the {latency_degradation_limit:.2f}% short-soak threshold"
    )

