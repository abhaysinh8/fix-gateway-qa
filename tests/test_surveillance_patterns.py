from __future__ import annotations

from fixgateway.surveillance.pattern_detector import (
    SurveillanceEvent,
    detect_high_cancel_ratio,
    detect_quote_stuffing_or_spoofing,
)


def event(
    order_id: str,
    action: str,
    timestamp: float,
    *,
    symbol: str = "AAPL",
    trader_id: str = "TRADER-1",
    quantity: int = 100,
    price: int = 200,
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "action": action,
        "timestamp": timestamp,
        "quantity": quantity,
        "price": price,
        "symbol": symbol,
        "trader_id": trader_id,
    }


def test_benign_order_activity_is_not_flagged() -> None:
    events = [
        event("A", "place", 0),
        event("A", "fill", 40, quantity=100),
        event("B", "place", 200),
        event("B", "partial_fill", 250, quantity=25),
        event("B", "cancel", 300, quantity=75),
        event("C", "place", 500),
        event("C", "cancel", 2_000),
    ]

    assert detect_quote_stuffing_or_spoofing(
        events, window_ms=1_000, cancel_threshold_ms=100
    ) == []
    assert detect_high_cancel_ratio(
        events, window_ms=1_000, ratio_threshold=0.70
    ) == []


def test_rapid_unfilled_cancel_is_spoofing_like() -> None:
    events = [
        SurveillanceEvent("FAST-1", "place", 1_000, 50, "101.25", "MSFT", "T-7"),
        SurveillanceEvent("FAST-1", "cancel", 1_007, 50, "101.25", "MSFT", "T-7"),
    ]

    matches = detect_quote_stuffing_or_spoofing(
        events, window_ms=500, cancel_threshold_ms=10
    )

    assert len(matches) == 1
    assert matches[0].pattern == "spoofing-like"
    assert matches[0].order_id == "FAST-1"
    assert matches[0].elapsed_ms == 7
    assert matches[0].trader_id == "T-7"


def test_many_rapid_cancels_are_layering_like() -> None:
    events = [event(f"OLD-{index}", "place", 0) for index in range(6)]
    events.extend(event(f"OLD-{index}", "cancel", 100 + index) for index in range(6))

    matches = detect_high_cancel_ratio(
        events, window_ms=10, ratio_threshold=0.80, min_actions=5
    )

    assert matches
    strongest = max(matches, key=lambda match: match.cancel_ratio)
    assert strongest.pattern == "layering-like"
    assert strongest.symbol == "AAPL"
    assert strongest.trader_id == "TRADER-1"
    assert strongest.cancel_count >= 5
    assert strongest.cancel_ratio == 1.0


def test_cancel_outside_rapid_cancel_threshold_is_not_flagged() -> None:
    events = [event("SLOW-1", "place", 10), event("SLOW-1", "cancel", 510)]

    assert detect_quote_stuffing_or_spoofing(
        events, window_ms=1_000, cancel_threshold_ms=100
    ) == []


def test_fill_before_quick_cancel_suppresses_spoofing_like_match() -> None:
    events = [
        event("TRADED-1", "place", 10),
        event("TRADED-1", "fill", 12, quantity=25),
        event("TRADED-1", "cancel", 15, quantity=75),
    ]

    assert detect_quote_stuffing_or_spoofing(
        events, window_ms=100, cancel_threshold_ms=10
    ) == []


def test_high_cancel_ratio_is_scoped_by_symbol_and_trader() -> None:
    events = [event(f"A-{index}", "cancel", index) for index in range(5)]
    events.extend(
        event(
            f"B-{index}",
            "place" if index < 4 else "cancel",
            index,
            trader_id="TRADER-2",
        )
        for index in range(5)
    )

    matches = detect_high_cancel_ratio(
        events, window_ms=10, ratio_threshold=0.70, min_actions=5
    )

    assert {(match.symbol, match.trader_id) for match in matches} == {
        ("AAPL", "TRADER-1")
    }

