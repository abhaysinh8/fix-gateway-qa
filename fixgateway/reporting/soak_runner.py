"""Duration-based FIX gateway soak driver and time-series health summary."""

from __future__ import annotations

import statistics
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import psutil

from fixgateway.client.fix_client import FixClient
from fixgateway.exchange.mock_exchange import MockExchange

from .latency import calculate_latency_percentiles


@dataclass(frozen=True)
class SoakConfig:
    duration_seconds: float = 180.0
    requests_per_second: float = 20.0
    sample_interval_seconds: float = 10.0
    burst_multiplier: float = 3.0
    burst_fraction: float = 0.15
    client_timeout_seconds: float = 2.0
    sender_comp_id: str = "SOAK_CLIENT"

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")
        if self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be greater than zero")
        if self.sample_interval_seconds <= 0:
            raise ValueError("sample_interval_seconds must be greater than zero")
        if self.burst_multiplier < 1:
            raise ValueError("burst_multiplier must be at least one")
        if not 0 <= self.burst_fraction < 0.5:
            raise ValueError("burst_fraction must be between zero and 0.5")
        if self.client_timeout_seconds <= 0:
            raise ValueError("client_timeout_seconds must be greater than zero")


@dataclass(frozen=True)
class SoakSample:
    elapsed_seconds: float
    memory_mb: float
    open_connections: int
    requests_completed: int
    window_requests: int
    p50_ms: float
    p99_ms: float
    p99_9_ms: float
    max_ms: float


@dataclass
class SoakResult:
    configured_duration_seconds: float
    actual_duration_seconds: float
    total_requests: int
    submitted_orders: int
    canceled_orders: int
    samples: list[SoakSample] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def memory_growth_percent(self) -> float:
        if len(self.samples) < 2 or self.samples[0].memory_mb == 0:
            return 0.0
        return (
            (self.samples[-1].memory_mb / self.samples[0].memory_mb) - 1
        ) * 100

    @property
    def early_p99_ms(self) -> float:
        values = self._third_values(first=True)
        return statistics.median(values) if values else 0.0

    @property
    def final_p99_ms(self) -> float:
        values = self._third_values(first=False)
        return statistics.median(values) if values else 0.0

    @property
    def latency_degradation_percent(self) -> float:
        early = self.early_p99_ms
        if early == 0:
            return 0.0 if self.final_p99_ms == 0 else float("inf")
        return ((self.final_p99_ms / early) - 1) * 100

    def _third_values(self, *, first: bool) -> list[float]:
        populated = [sample.p99_ms for sample in self.samples if sample.window_requests]
        if not populated:
            return []
        third_size = max(1, len(populated) // 3)
        return populated[:third_size] if first else populated[-third_size:]

    def format_summary(self) -> str:
        lines = [
            "FIX gateway soak time series",
            "+----------+------------+-------------+----------+---------+---------+---------+---------+",
            "| Elapsed  | Memory MB  | Connections | Requests | p50 ms  | p99 ms  | p99.9   | max ms  |",
            "+----------+------------+-------------+----------+---------+---------+---------+---------+",
        ]
        for sample in self.samples:
            lines.append(
                f"| {sample.elapsed_seconds:>7.1f}s | {sample.memory_mb:>10.2f} | "
                f"{sample.open_connections:>11} | {sample.requests_completed:>8} | "
                f"{sample.p50_ms:>7.3f} | {sample.p99_ms:>7.3f} | "
                f"{sample.p99_9_ms:>7.3f} | {sample.max_ms:>7.3f} |"
            )
        lines.extend(
            [
                "+----------+------------+-------------+----------+---------+---------+---------+---------+",
                f"Duration: {self.actual_duration_seconds:.2f}s; requests: {self.total_requests} "
                f"({self.submitted_orders} submissions, {self.canceled_orders} cancels)",
                f"Memory growth: {self.memory_growth_percent:+.2f}%",
                f"p99 early/final: {self.early_p99_ms:.3f}/{self.final_p99_ms:.3f} ms "
                f"({self.latency_degradation_percent:+.2f}%)",
                f"Errors: {len(self.errors)}",
            ]
        )
        return "\n".join(lines)


class SoakRunner:
    """Run mixed order/cancel traffic for wall-clock duration, not an order count."""

    def __init__(
        self,
        host: str,
        port: int,
        config: SoakConfig | None = None,
        *,
        connection_count: Callable[[], int] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.config = config or SoakConfig()
        self.connection_count = connection_count or self._process_tcp_connections

    @staticmethod
    def _process_tcp_connections() -> int:
        process = psutil.Process()
        try:
            connections = process.net_connections(kind="tcp")
        except (psutil.AccessDenied, psutil.Error):
            return -1
        return sum(connection.status != psutil.CONN_CLOSE for connection in connections)

    def run(self) -> SoakResult:
        config = self.config
        process = psutil.Process()
        started = time.perf_counter()
        next_sample = started + config.sample_interval_seconds
        next_request = started
        rolling_latencies: list[float] = []
        samples: list[SoakSample] = []
        errors: list[str] = []
        resting_orders: deque[tuple[str, str, str]] = deque()
        total_requests = submitted_orders = canceled_orders = 0
        order_number = cancel_number = 0

        with FixClient(
            self.host,
            self.port,
            sender_comp_id=config.sender_comp_id,
            timeout=config.client_timeout_seconds,
        ) as client:
            while True:
                now = time.perf_counter()
                elapsed = now - started
                if elapsed >= config.duration_seconds:
                    break

                if now >= next_sample:
                    samples.append(
                        self._sample(
                            process,
                            elapsed,
                            total_requests,
                            rolling_latencies,
                        )
                    )
                    rolling_latencies.clear()
                    while next_sample <= now:
                        next_sample += config.sample_interval_seconds

                if now < next_request:
                    time.sleep(min(next_request - now, 0.05))
                    continue

                in_burst = (
                    elapsed < config.duration_seconds * config.burst_fraction
                    or elapsed
                    >= config.duration_seconds * (1 - config.burst_fraction)
                )
                rate = config.requests_per_second * (
                    config.burst_multiplier if in_burst else 1
                )
                next_request = now + (1 / rate)

                try:
                    if resting_orders:
                        order_id, symbol, side = resting_orders.popleft()
                        cancel_number += 1
                        client.send_cancel(
                            f"SOAK-CANCEL-{cancel_number}", order_id, symbol, side
                        )
                        canceled_orders += 1
                    else:
                        order_number += 1
                        order_id = f"SOAK-ORDER-{order_number}"
                        symbol = ("AAPL", "MSFT", "NVDA")[order_number % 3]
                        side = "1" if order_number % 2 else "2"
                        client.send_new_order(
                            order_id,
                            symbol,
                            side,
                            10 + order_number % 90,
                            str(100 + order_number % 50),
                        )
                        resting_orders.append((order_id, symbol, side))
                        submitted_orders += 1
                    total_requests += 1
                    if client.last_latency_ms is not None:
                        rolling_latencies.append(client.last_latency_ms)
                except Exception as exc:  # retained in the result for long unattended runs
                    errors.append(f"request {total_requests + 1}: {type(exc).__name__}: {exc}")

            final_elapsed = time.perf_counter() - started
            if rolling_latencies or not samples:
                samples.append(
                    self._sample(
                        process,
                        final_elapsed,
                        total_requests,
                        rolling_latencies,
                    )
                )

        actual_duration = time.perf_counter() - started
        return SoakResult(
            configured_duration_seconds=config.duration_seconds,
            actual_duration_seconds=actual_duration,
            total_requests=total_requests,
            submitted_orders=submitted_orders,
            canceled_orders=canceled_orders,
            samples=samples,
            errors=errors,
        )

    def _sample(
        self,
        process: psutil.Process,
        elapsed: float,
        requests_completed: int,
        latencies: list[float],
    ) -> SoakSample:
        if latencies:
            metrics = calculate_latency_percentiles(latencies)
        else:
            metrics = {"p50": 0, "p99": 0, "p99_9": 0, "max": 0}
        return SoakSample(
            elapsed_seconds=elapsed,
            memory_mb=process.memory_info().rss / (1024 * 1024),
            open_connections=self.connection_count(),
            requests_completed=requests_completed,
            window_requests=len(latencies),
            p50_ms=float(metrics["p50"]),
            p99_ms=float(metrics["p99"]),
            p99_9_ms=float(metrics["p99_9"]),
            max_ms=float(metrics["max"]),
        )


def run_soak(
    config: SoakConfig | None = None,
    *,
    exchange: MockExchange | None = None,
) -> SoakResult:
    """Run against a supplied exchange, or create a local resting-order exchange."""

    owned_exchange = exchange is None
    active_exchange = exchange or MockExchange(fill_strategy="resting")
    if owned_exchange:
        active_exchange.start()
    elif not active_exchange.is_running:
        raise RuntimeError("The supplied MockExchange must already be running")
    try:
        runner = SoakRunner(
            active_exchange.host,
            active_exchange.port,
            config,
            connection_count=lambda: active_exchange.active_connection_count,
        )
        return runner.run()
    finally:
        if owned_exchange:
            active_exchange.stop()

