from __future__ import annotations

import time

import pytest

from fixgateway.client.fix_client import FixClient, FixRequestTimeoutError
from fixgateway.exchange.mock_exchange import MockExchange


def test_dropped_execution_report_times_out_clearly_without_hanging() -> None:
    with MockExchange(
        processing_delay_ms=(0, 0),
        fill_strategy="resting",
        chaos_mode=True,
        chaos_rate=1.0,
        chaos_drop_probability=1.0,
        random_seed=7,
    ) as exchange:
        with FixClient(exchange.host, exchange.port, timeout=0.10) as client:
            started = time.perf_counter()
            with pytest.raises(
                FixRequestTimeoutError,
                match=r"MsgType D \(ClOrdID CHAOS-DROP-1\)",
            ):
                client.send_new_order("CHAOS-DROP-1", "AAPL", "1", 10, "200")
            elapsed = time.perf_counter() - started

        assert elapsed < 1.0
        assert any(entry.direction == "DROPPED" for entry in exchange.message_log)


def test_delayed_execution_report_arrives_when_within_client_timeout() -> None:
    with MockExchange(
        processing_delay_ms=(0, 0),
        fill_strategy="resting",
        chaos_mode=True,
        chaos_rate=1.0,
        chaos_drop_probability=0.0,
        chaos_extra_delay_ms=(30, 30),
        random_seed=11,
    ) as exchange:
        with FixClient(exchange.host, exchange.port, timeout=0.50) as client:
            response = client.send_new_order("CHAOS-DELAY-1", "MSFT", "1", 5, "400")

        assert response[39] == "0"
        assert client.last_latency_ms is not None
        assert client.last_latency_ms >= 25

