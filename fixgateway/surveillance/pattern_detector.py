"""Simplified educational market-abuse pattern heuristics.

This module exists for a QA/testing portfolio project. It is not a real trade
surveillance or regulatory-compliance system, and its ``spoofing-like`` and
``layering-like`` labels are heuristic test signals rather than allegations or
compliance determinations.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class SurveillanceEvent:
    """One normalized action in an order timeline.

    Numeric timestamps are interpreted as milliseconds. Naive datetimes are treated
    as UTC so test results do not depend on the machine's local timezone.
    """

    order_id: str
    action: str
    timestamp: datetime | int | float | str
    quantity: Decimal | int | float | str
    price: Decimal | int | float | str
    symbol: str
    trader_id: str = "UNKNOWN"


@dataclass(frozen=True)
class SpoofingLikeMatch:
    pattern: str
    order_id: str
    symbol: str
    trader_id: str
    placed_at_ms: float
    canceled_at_ms: float
    elapsed_ms: float
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class LayeringLikeMatch:
    pattern: str
    symbol: str
    trader_id: str
    window_start_ms: float
    window_end_ms: float
    window_ms: float
    cancel_count: int
    total_actions: int
    cancel_ratio: float


@dataclass(frozen=True)
class _NormalizedEvent:
    order_id: str
    action: str
    timestamp_ms: float
    quantity: Decimal
    price: Decimal
    symbol: str
    trader_id: str


_ACTION_ALIASES = {
    "place": "place",
    "new": "place",
    "cancel": "cancel",
    "fill": "fill",
    "partial_fill": "fill",
    "full_fill": "fill",
}


def _timestamp_ms(value: datetime | int | float | str) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        result = value.timestamp() * 1_000
    elif isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"Invalid event timestamp: {value!r}") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            result = parsed.timestamp() * 1_000
    else:
        raise TypeError(f"Unsupported event timestamp: {value!r}")
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"Event timestamp must be finite: {value!r}")
    return result


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Event {name} must be numeric: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"Event {name} must be finite and non-negative: {value!r}")
    return result


def _normalize_event(
    event: SurveillanceEvent | Mapping[str, Any],
) -> _NormalizedEvent:
    if isinstance(event, SurveillanceEvent):
        values = {
            "order_id": event.order_id,
            "action": event.action,
            "timestamp": event.timestamp,
            "quantity": event.quantity,
            "price": event.price,
            "symbol": event.symbol,
            "trader_id": event.trader_id,
        }
    elif isinstance(event, Mapping):
        required = {"order_id", "action", "timestamp", "quantity", "price", "symbol"}
        missing = sorted(required - event.keys())
        if missing:
            raise ValueError(f"Surveillance event is missing fields: {', '.join(missing)}")
        values = dict(event)
    else:
        raise TypeError("Events must be SurveillanceEvent instances or mappings")

    order_id = str(values["order_id"])
    symbol = str(values["symbol"])
    trader_id = str(values.get("trader_id", values.get("trader", "UNKNOWN")))
    if not order_id or not symbol or not trader_id:
        raise ValueError("Event order_id, symbol, and trader_id must not be empty")
    raw_action = str(values["action"]).strip().lower()
    try:
        action = _ACTION_ALIASES[raw_action]
    except KeyError as exc:
        raise ValueError(f"Unsupported surveillance action: {values['action']!r}") from exc

    return _NormalizedEvent(
        order_id=order_id,
        action=action,
        timestamp_ms=_timestamp_ms(values["timestamp"]),
        quantity=_decimal(values["quantity"], "quantity"),
        price=_decimal(values["price"], "price"),
        symbol=symbol,
        trader_id=trader_id,
    )


def _normalized_events(
    events: Iterable[SurveillanceEvent | Mapping[str, Any]],
) -> list[_NormalizedEvent]:
    return sorted(
        (_normalize_event(event) for event in events),
        key=lambda item: item.timestamp_ms,
    )


def detect_quote_stuffing_or_spoofing(
    events: Iterable[SurveillanceEvent | Mapping[str, Any]],
    window_ms: float = 1_000,
    cancel_threshold_ms: float = 100,
) -> list[SpoofingLikeMatch]:
    """Find rapid, unfilled place→cancel lifecycles.

    A match requires the cancel to occur within both ``window_ms`` and
    ``cancel_threshold_ms`` of the latest placement. Any intervening partial or full
    fill suppresses the match.
    """

    if window_ms <= 0:
        raise ValueError("window_ms must be greater than zero")
    if cancel_threshold_ms < 0:
        raise ValueError("cancel_threshold_ms must be non-negative")

    active_orders: dict[tuple[str, str], tuple[_NormalizedEvent, bool]] = {}
    matches: list[SpoofingLikeMatch] = []
    for event in _normalized_events(events):
        key = (event.trader_id, event.order_id)
        if event.action == "place":
            active_orders[key] = (event, False)
        elif event.action == "fill":
            if key in active_orders:
                placement, _ = active_orders[key]
                active_orders[key] = (placement, True)
        elif event.action == "cancel" and key in active_orders:
            placement, had_fill = active_orders.pop(key)
            elapsed_ms = event.timestamp_ms - placement.timestamp_ms
            if (
                not had_fill
                and elapsed_ms <= window_ms
                and elapsed_ms <= cancel_threshold_ms
            ):
                matches.append(
                    SpoofingLikeMatch(
                        pattern="spoofing-like",
                        order_id=event.order_id,
                        symbol=placement.symbol,
                        trader_id=event.trader_id,
                        placed_at_ms=placement.timestamp_ms,
                        canceled_at_ms=event.timestamp_ms,
                        elapsed_ms=elapsed_ms,
                        quantity=placement.quantity,
                        price=placement.price,
                    )
                )
    return matches


def detect_high_cancel_ratio(
    events: Iterable[SurveillanceEvent | Mapping[str, Any]],
    window_ms: float = 1_000,
    ratio_threshold: float = 0.80,
    *,
    min_actions: int = 5,
) -> list[LayeringLikeMatch]:
    """Find high cancel ratios in trailing windows grouped by symbol and trader.

    The ratio is ``cancel actions / all actions`` inside each sliding window. The
    ``min_actions`` guard reflects that a high ratio on tiny samples is not useful.
    """

    if window_ms <= 0:
        raise ValueError("window_ms must be greater than zero")
    if not 0 <= ratio_threshold <= 1:
        raise ValueError("ratio_threshold must be between zero and one")
    if min_actions <= 0:
        raise ValueError("min_actions must be greater than zero")

    grouped: dict[tuple[str, str], list[_NormalizedEvent]] = defaultdict(list)
    for event in _normalized_events(events):
        grouped[(event.symbol, event.trader_id)].append(event)

    matches: list[LayeringLikeMatch] = []
    for (symbol, trader_id), group in grouped.items():
        left = 0
        cancel_count = 0
        for right, event in enumerate(group):
            if event.action == "cancel":
                cancel_count += 1
            while event.timestamp_ms - group[left].timestamp_ms > window_ms:
                if group[left].action == "cancel":
                    cancel_count -= 1
                left += 1

            total_actions = right - left + 1
            cancel_ratio = cancel_count / total_actions
            if total_actions >= min_actions and cancel_ratio > ratio_threshold:
                matches.append(
                    LayeringLikeMatch(
                        pattern="layering-like",
                        symbol=symbol,
                        trader_id=trader_id,
                        window_start_ms=group[left].timestamp_ms,
                        window_end_ms=event.timestamp_ms,
                        window_ms=window_ms,
                        cancel_count=cancel_count,
                        total_actions=total_actions,
                        cancel_ratio=cancel_ratio,
                    )
                )
    return matches
