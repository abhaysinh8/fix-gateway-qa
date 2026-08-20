from __future__ import annotations

import time

import pytest

from fixgateway.audit.audit_checker import check_completeness
from fixgateway.client.fix_client import FixClient, FixRetriesExhaustedError
from fixgateway.exchange.mock_exchange import MockExchange
from fixgateway.exchange.state_machine import OrderEvent, OrderState


def run_staged_fill(*, duplicate_rate: float, chaos_mode: bool):
    with MockExchange(
        processing_delay_ms=(0, 0),
        fill_strategy="two_partial_then_full",
        chaos_mode=chaos_mode,
        chaos_rate=0,
        chaos_duplicate_rate=duplicate_rate,
        chaos_reorder_rate=0,
        random_seed=5,
    ) as exchange:
        with FixClient(exchange.host, exchange.port, timeout=0.5) as client:
            client.send_new_order("DUPLICATE-FLOW", "AAPL", "1", 100, "200")
            client.receive_message()
            client.receive_message()
            client.receive_message()
            state = client.order_states["DUPLICATE-FLOW"].state
            events = [
                transition.event
                for transition in client.order_states["DUPLICATE-FLOW"].history
            ]
            duplicate_count = len(client.detected_duplicates)
            processing_errors = list(client.processing_errors)
    return state, events, duplicate_count, processing_errors


def test_duplicate_execution_reports_never_double_apply_local_state() -> None:
    normal = run_staged_fill(duplicate_rate=0, chaos_mode=False)
    chaotic = run_staged_fill(duplicate_rate=1, chaos_mode=True)

    assert chaotic[0] is normal[0] is OrderState.FILLED
    assert chaotic[1] == normal[1] == [
        OrderEvent.ACK,
        OrderEvent.PARTIAL_FILL,
        OrderEvent.PARTIAL_FILL,
        OrderEvent.FULL_FILL,
    ]
    assert chaotic[2] >= 3
    assert chaotic[3] == []


def test_reports_reordered_across_orders_update_only_their_own_state() -> None:
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
            client.send_new_order_nowait("REORDER-1", "MSFT", "1", 10, "400")
            client.send_new_order_nowait("REORDER-2", "NVDA", "2", 20, "180")
            arrival_order = [client.receive_message()[11], client.receive_message()[11]]

            assert arrival_order == ["REORDER-2", "REORDER-1"]
            assert client.order_states["REORDER-1"].state is OrderState.NEW
            assert client.order_states["REORDER-2"].state is OrderState.NEW
            assert [
                transition.event
                for transition in client.order_states["REORDER-1"].history
            ] == [OrderEvent.ACK]
            assert [
                transition.event
                for transition in client.order_states["REORDER-2"].history
            ] == [OrderEvent.ACK]
            assert client.processing_errors == []


def test_timed_out_new_order_retry_is_replayed_without_double_fill() -> None:
    # Seed 31 makes the 50% chaos selector drop both original ACK/fill reports,
    # then deliver both cached reports when the identical ClOrdID is retried.
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
        with FixClient(
            exchange.host, exchange.port, timeout=0.05, max_retries=1
        ) as client:
            acknowledgement = client.send_new_order(
                "RETRY-ONCE", "AAPL", "1", 25, "200"
            )
            fill = client.receive_message()

            assert acknowledgement[150] == "0"
            assert fill[150] == "2"
            assert client.retry_attempts["RETRY-ONCE"] == 1
            assert client.order_states["RETRY-ONCE"].state is OrderState.FILLED

        orders = exchange.order_book.orders
        assert list(orders) == ["RETRY-ONCE"]
        assert orders["RETRY-ONCE"].cumulative_quantity == 25
        assert [
            transition.event
            for transition in orders["RETRY-ONCE"].state_machine.history
        ] == [OrderEvent.ACK, OrderEvent.FULL_FILL]
        audit_result = check_completeness(exchange.audit_log, orders)
        assert audit_result.is_complete, audit_result.issues


def test_excessive_drops_raise_typed_error_after_retries_are_exhausted() -> None:
    with MockExchange(
        processing_delay_ms=(0, 0),
        fill_strategy="full",
        chaos_mode=True,
        chaos_rate=1,
        chaos_drop_probability=1,
        chaos_duplicate_rate=0,
        chaos_reorder_rate=0,
        random_seed=13,
    ) as exchange:
        with FixClient(
            exchange.host, exchange.port, timeout=0.04, max_retries=2
        ) as client:
            started = time.perf_counter()
            with pytest.raises(FixRetriesExhaustedError) as raised:
                client.send_new_order("RETRY-FAIL", "IBM", "1", 10, "250")
            elapsed = time.perf_counter() - started

        assert raised.value.attempts == 3
        assert "retries exhausted" in str(raised.value)
        assert elapsed < 1
        assert list(exchange.order_book.orders) == ["RETRY-FAIL"]
        assert exchange.order_book.orders["RETRY-FAIL"].cumulative_quantity == 10
