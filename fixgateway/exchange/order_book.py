"""Minimal in-memory order book used by the future mock exchange."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from fixgateway.fix.constants import Side

from .state_machine import OrderEvent, OrderState, OrderStateMachine


class FillMode(str, Enum):
    RESTING = "resting"
    PARTIAL = "partial"
    FULL = "full"


class DuplicateOrderError(ValueError):
    """Raised when an order ID is already present in the book."""


class OrderNotFoundError(KeyError):
    """Raised when an operation references an unknown order ID."""


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric; got {value!r}") from exc
    if not converted.is_finite():
        raise ValueError(f"{field_name} must be finite; got {value!r}")
    return converted


@dataclass
class Order:
    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal | None = None
    state_machine: OrderStateMachine = field(default_factory=OrderStateMachine)
    cumulative_quantity: Decimal = field(default_factory=lambda: Decimal("0"))

    @property
    def state(self) -> OrderState:
        return self.state_machine.state

    @property
    def leaves_quantity(self) -> Decimal:
        return self.quantity - self.cumulative_quantity


class OrderBook:
    """Store orders and simulate acknowledgement, fills, and cancel confirmation."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    @property
    def orders(self) -> dict[str, Order]:
        """Return a shallow copy so callers cannot replace book entries directly."""

        return dict(self._orders)

    def get_order(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise OrderNotFoundError(f"Unknown order ID: {order_id}") from exc

    def accept_order(
        self,
        order_id: str,
        symbol: str,
        side: Side | str,
        quantity: Decimal | int | str,
        price: Decimal | int | str | None = None,
        *,
        fill_mode: FillMode | str = FillMode.RESTING,
        partial_fill_quantity: Decimal | int | str | None = None,
    ) -> Order:
        """Acknowledge an order, then optionally fill it partially or fully."""

        if not order_id:
            raise ValueError("order_id must not be empty")
        if order_id in self._orders:
            raise DuplicateOrderError(f"Order ID already exists: {order_id}")
        if not symbol:
            raise ValueError("symbol must not be empty")

        try:
            normalized_side = side if isinstance(side, Side) else Side(str(side))
        except ValueError as exc:
            raise ValueError(f"Unsupported side: {side!r}") from exc

        normalized_quantity = _decimal(quantity, "quantity")
        if normalized_quantity <= 0:
            raise ValueError("quantity must be greater than zero")

        normalized_price = None if price is None else _decimal(price, "price")
        if normalized_price is not None and normalized_price < 0:
            raise ValueError("price must be non-negative")

        try:
            normalized_fill_mode = (
                fill_mode if isinstance(fill_mode, FillMode) else FillMode(fill_mode)
            )
        except ValueError as exc:
            raise ValueError(f"Unsupported fill mode: {fill_mode!r}") from exc

        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=normalized_side,
            quantity=normalized_quantity,
            price=normalized_price,
        )
        order.state_machine.apply(OrderEvent.ACK)
        self._orders[order_id] = order

        if normalized_fill_mode is FillMode.FULL:
            self.fill_order(order_id, order.quantity)
        elif normalized_fill_mode is FillMode.PARTIAL:
            if partial_fill_quantity is None:
                raise ValueError("partial_fill_quantity is required for partial fill mode")
            try:
                self.fill_order(order_id, partial_fill_quantity, require_partial=True)
            except Exception:
                # Acceptance is atomic when its requested automatic fill is invalid.
                del self._orders[order_id]
                raise
        elif partial_fill_quantity is not None:
            del self._orders[order_id]
            raise ValueError(
                "partial_fill_quantity can only be used with partial fill mode"
            )

        return order

    # These names make the stub natural to call from different Phase 3 components.
    add_order = accept_order
    submit_order = accept_order

    def fill_order(
        self,
        order_id: str,
        quantity: Decimal | int | str,
        *,
        require_partial: bool = False,
    ) -> Order:
        """Apply a fill, selecting PARTIAL_FILL or FULL_FILL from its quantity."""

        order = self.get_order(order_id)
        fill_quantity = _decimal(quantity, "fill quantity")
        if fill_quantity <= 0:
            raise ValueError("fill quantity must be greater than zero")
        if fill_quantity > order.leaves_quantity:
            raise ValueError(
                f"fill quantity {fill_quantity} exceeds leaves quantity "
                f"{order.leaves_quantity} for order {order_id}"
            )
        if require_partial and fill_quantity == order.leaves_quantity:
            raise ValueError("partial fill quantity must be less than leaves quantity")

        event = (
            OrderEvent.FULL_FILL
            if fill_quantity == order.leaves_quantity
            else OrderEvent.PARTIAL_FILL
        )
        order.state_machine.apply(event)
        order.cumulative_quantity += fill_quantity
        return order

    def auto_fill(self, order_id: str) -> Order:
        order = self.get_order(order_id)
        return self.fill_order(order_id, order.leaves_quantity)

    def cancel_order(self, order_id: str) -> Order:
        """Simulate a cancel request immediately confirmed by the exchange."""

        order = self.get_order(order_id)
        order.state_machine.apply(OrderEvent.CANCEL_REQUEST)
        order.state_machine.apply(OrderEvent.CANCEL_CONFIRMED)
        return order

    process_cancel = cancel_order

