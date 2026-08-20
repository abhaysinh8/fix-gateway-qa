from __future__ import annotations

from decimal import Decimal

import pytest

from fixgateway.exchange.order_book import FillMode, OrderBook
from fixgateway.exchange.state_machine import InvalidTransitionError, OrderEvent, OrderState


def test_order_can_rest_after_acknowledgement() -> None:
    order = OrderBook().accept_order("A", "AAPL", "1", 100, "225.00")
    assert order.state is OrderState.NEW
    assert order.leaves_quantity == Decimal("100")
    assert [entry.event for entry in order.state_machine.history] == [OrderEvent.ACK]


def test_order_can_auto_fill_fully() -> None:
    order = OrderBook().accept_order(
        "A", "AAPL", "1", 100, fill_mode=FillMode.FULL
    )
    assert order.state is OrderState.FILLED
    assert order.cumulative_quantity == Decimal("100")
    assert order.leaves_quantity == 0


def test_order_can_partially_fill_then_cancel_remaining_quantity() -> None:
    book = OrderBook()
    order = book.accept_order(
        "A",
        "AAPL",
        "2",
        100,
        fill_mode=FillMode.PARTIAL,
        partial_fill_quantity=25,
    )
    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.cumulative_quantity == Decimal("25")
    assert order.leaves_quantity == Decimal("75")

    book.cancel_order("A")
    assert order.state is OrderState.CANCELED
    assert order.leaves_quantity == Decimal("75")


def test_overfill_is_rejected_without_changing_order() -> None:
    book = OrderBook()
    order = book.accept_order("A", "AAPL", "1", 10)
    with pytest.raises(ValueError, match="exceeds leaves quantity"):
        book.fill_order("A", 11)
    assert order.state is OrderState.NEW
    assert order.cumulative_quantity == 0


def test_filled_order_cannot_be_canceled() -> None:
    book = OrderBook()
    order = book.accept_order("A", "AAPL", "1", 10, fill_mode="full")
    with pytest.raises(InvalidTransitionError):
        book.cancel_order("A")
    assert order.state is OrderState.FILLED

