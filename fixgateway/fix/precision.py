"""Fixed-point financial arithmetic and deliberately naive float comparisons.

The Decimal helpers are the production-style API. Functions prefixed with ``naive_``
intentionally use binary floating point so the regression suite can demonstrate why
that representation is unsafe for exact price, quantity, and currency calculations.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import TypeAlias

from .constants import Side


DecimalInput: TypeAlias = Decimal | int | float | str
Fill: TypeAlias = tuple[DecimalInput, DecimalInput]


def _as_decimal(value: DecimalInput, name: str) -> Decimal:
    """Convert through text so a float's display value is not expanded into its bits."""

    if isinstance(value, bool):
        raise TypeError(f"{name} must be numeric, not bool")
    try:
        converted = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be numeric; got {value!r}") from exc
    if not converted.is_finite():
        raise ValueError(f"{name} must be finite; got {value!r}")
    return converted


def round_to_tick(price: DecimalInput, tick_size: DecimalInput) -> Decimal:
    """Round a price to the nearest tick using round-half-to-even tie breaking."""

    decimal_price = _as_decimal(price, "price")
    decimal_tick = _as_decimal(tick_size, "tick_size")
    if decimal_tick <= 0:
        raise ValueError("tick_size must be greater than zero")
    tick_count = (decimal_price / decimal_tick).quantize(
        Decimal("1"), rounding=ROUND_HALF_EVEN
    )
    return (tick_count * decimal_tick).quantize(
        decimal_tick, rounding=ROUND_HALF_EVEN
    )


def notional(price: DecimalInput, quantity: DecimalInput) -> Decimal:
    """Return exact price × quantity using decimal fixed-point operands."""

    return _as_decimal(price, "price") * _as_decimal(quantity, "quantity")


def average_fill_price(fills: Iterable[Fill]) -> Decimal:
    """Return the quantity-weighted average price across one or more fills."""

    total_notional = Decimal("0")
    total_quantity = Decimal("0")
    for index, (price, quantity) in enumerate(fills):
        decimal_price = _as_decimal(price, f"fills[{index}].price")
        decimal_quantity = _as_decimal(quantity, f"fills[{index}].quantity")
        if decimal_quantity <= 0:
            raise ValueError("Fill quantities must be greater than zero")
        total_notional += decimal_price * decimal_quantity
        total_quantity += decimal_quantity
    if total_quantity == 0:
        raise ValueError("At least one fill is required")

    # Extra working precision keeps repeating weighted averages accurate enough to
    # reconstruct the source notional even for large quantities.
    with localcontext() as context:
        context.prec = 50
        return total_notional / total_quantity


def pnl(
    entry_price: DecimalInput,
    exit_price: DecimalInput,
    quantity: DecimalInput,
    side: Side | str,
) -> Decimal:
    """Compute closed-position PnL: rising prices profit buys and lose for sells."""

    entry = _as_decimal(entry_price, "entry_price")
    exit_ = _as_decimal(exit_price, "exit_price")
    decimal_quantity = _as_decimal(quantity, "quantity")
    if decimal_quantity < 0:
        raise ValueError("quantity must be non-negative")

    normalized_side = side.value if isinstance(side, Side) else str(side).strip().lower()
    if normalized_side in {Side.BUY.value, "buy", "b"}:
        direction = Decimal("1")
    elif normalized_side in {Side.SELL.value, "sell", "s"}:
        direction = Decimal("-1")
    else:
        raise ValueError(f"Unsupported side: {side!r}")
    return (exit_ - entry) * decimal_quantity * direction


def round_currency(amount: DecimalInput) -> Decimal:
    """Round money to cents with banker's (round-half-to-even) rounding."""

    return _as_decimal(amount, "amount").quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )


# Deliberately naive comparison functions. Do not use these for financial accounting.
def naive_round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        raise ValueError("tick_size must be greater than zero")
    return round(float(price) / float(tick_size)) * float(tick_size)


def naive_notional(price: float, quantity: float) -> float:
    return float(price) * float(quantity)


def naive_average_fill_price(fills: Iterable[tuple[float, float]]) -> float:
    materialized = [(float(price), float(quantity)) for price, quantity in fills]
    if not materialized:
        raise ValueError("At least one fill is required")
    if any(quantity <= 0 for _, quantity in materialized):
        raise ValueError("Fill quantities must be greater than zero")
    return sum(price * quantity for price, quantity in materialized) / sum(
        quantity for _, quantity in materialized
    )


def naive_pnl(
    entry_price: float,
    exit_price: float,
    quantity: float,
    side: Side | str,
) -> float:
    normalized_side = side.value if isinstance(side, Side) else str(side).strip().lower()
    if normalized_side in {Side.BUY.value, "buy", "b"}:
        direction = 1.0
    elif normalized_side in {Side.SELL.value, "sell", "s"}:
        direction = -1.0
    else:
        raise ValueError(f"Unsupported side: {side!r}")
    return (float(exit_price) - float(entry_price)) * float(quantity) * direction


def naive_round_currency(amount: float) -> float:
    return round(float(amount), 2)


# Descriptive aliases keep the API easy to discover from either naming convention.
round_price_to_tick = round_to_tick
compute_notional = notional
compute_average_fill_price = average_fill_price
compute_pnl = pnl


__all__ = [
    "average_fill_price",
    "compute_average_fill_price",
    "compute_notional",
    "compute_pnl",
    "naive_average_fill_price",
    "naive_notional",
    "naive_pnl",
    "naive_round_currency",
    "naive_round_to_tick",
    "notional",
    "pnl",
    "round_currency",
    "round_price_to_tick",
    "round_to_tick",
]

