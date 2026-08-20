from __future__ import annotations

from collections.abc import Iterator

import pytest

from fixgateway.client.fix_client import FixClient
from fixgateway.exchange.mock_exchange import MockExchange
from fixgateway.fix.message import FixMessage
from fixgateway.fix.validator import validate


@pytest.fixture
def exchange_factory() -> Iterator:
    exchanges: list[MockExchange] = []

    def create(fill_strategy: str = "resting") -> MockExchange:
        exchange = MockExchange(
            processing_delay_ms=(0.1, 0.5), fill_strategy=fill_strategy
        ).start()
        exchanges.append(exchange)
        return exchange

    yield create
    for exchange in exchanges:
        exchange.stop()


def test_new_order_is_acknowledged_then_fully_filled(exchange_factory) -> None:
    exchange = exchange_factory("full")
    with FixClient(exchange.host, exchange.port) as client:
        acknowledgement = client.send_new_order("FULL-1", "AAPL", "1", 100, "225")
        fill = client.receive_message()

    assert (acknowledgement[35], acknowledgement[150], acknowledgement[39]) == (
        "8",
        "0",
        "0",
    )
    assert (fill[35], fill[150], fill[39]) == ("8", "2", "2")
    assert (fill[14], fill[151]) == ("100", "0")
    assert validate(acknowledgement).is_valid
    assert validate(fill).is_valid


def test_order_receives_two_partial_fills_then_full_fill(exchange_factory) -> None:
    exchange = exchange_factory("two_partial_then_full")
    with FixClient(exchange.host, exchange.port) as client:
        messages = [client.send_new_order("STAGED-1", "MSFT", "2", 100, "500")]
        messages.extend(client.receive_message() for _ in range(3))

    assert [message[150] for message in messages] == ["0", "1", "1", "2"]
    assert [message[39] for message in messages] == ["0", "1", "1", "2"]
    assert [(message[14], message[151]) for message in messages] == [
        ("0", "100"),
        ("25", "75"),
        ("50", "50"),
        ("100", "0"),
    ]
    assert all(validate(message).is_valid for message in messages)


def test_resting_order_can_be_canceled(exchange_factory) -> None:
    exchange = exchange_factory()
    with FixClient(exchange.host, exchange.port) as client:
        acknowledgement = client.send_new_order("REST-1", "NVDA", "1", 20, "180")
        canceled = client.send_cancel("CANCEL-1", "REST-1", "NVDA", "1")

    assert acknowledgement[39] == "0"
    assert (canceled[35], canceled[150], canceled[39]) == ("8", "4", "4")
    assert canceled[11] == "CANCEL-1"
    assert validate(canceled).is_valid


def test_canceling_filled_order_returns_cancel_reject(exchange_factory) -> None:
    exchange = exchange_factory("full")
    with FixClient(exchange.host, exchange.port) as client:
        client.send_new_order("DONE-1", "IBM", "1", 10, "250")
        client.receive_message()
        rejection = client.send_cancel("CANCEL-2", "DONE-1", "IBM", "1")

    assert rejection[35] == "9"
    assert rejection[11] == "CANCEL-2"
    assert rejection[41] == "DONE-1"
    assert "FILLED" in rejection[58]
    assert validate(rejection).is_valid


def test_malformed_order_is_rejected_without_stopping_exchange(exchange_factory) -> None:
    exchange = exchange_factory()
    with FixClient(exchange.host, exchange.port) as client:
        malformed = FixMessage(
            {
                8: "FIX.4.4",
                35: "D",
                49: "TEST_CLIENT",
                56: "MOCK_EXCHANGE",
                34: "2",
                52: "20260820-13:30:00.000",
                11: "BAD-1",
                # Symbol (55) deliberately omitted.
                54: "1",
                38: "10",
                40: "2",
                44: "10",
            }
        )
        rejection = client.send_message(malformed, cl_ord_id="BAD-1")
        valid_after_reject = client.send_new_order("GOOD-1", "ORCL", "1", 5, "150")

    assert (rejection[35], rejection[150], rejection[39]) == ("8", "8", "8")
    assert "Missing required tag 55" in rejection[58]
    assert valid_after_reject[39] == "0"
    assert exchange.is_running
    assert len(exchange.message_log) >= 6  # logon pair plus two request/response pairs


def test_concurrent_client_api_sends_independent_orders(exchange_factory) -> None:
    import asyncio

    exchange = exchange_factory()
    client = FixClient(exchange.host, exchange.port, sender_comp_id="LOAD_CLIENT")
    orders = [
        {
            "cl_ord_id": f"CONCURRENT-{index}",
            "symbol": "AAPL",
            "side": "1",
            "quantity": 1,
            "price": 100,
        }
        for index in range(10)
    ]
    results = asyncio.run(client.send_orders_concurrently(orders, max_concurrency=4))

    assert len(results) == 10
    assert all(result.message[39] == "0" for result in results)
    assert all(result.latency_ms > 0 for result in results)

