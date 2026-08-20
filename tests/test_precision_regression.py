from __future__ import annotations

from decimal import Decimal, localcontext

import pytest
from hypothesis import given, strategies as st

from fixgateway.fix.precision import (
    average_fill_price,
    naive_average_fill_price,
    naive_notional,
    naive_pnl,
    naive_round_currency,
    naive_round_to_tick,
    notional,
    pnl,
    round_currency,
    round_to_tick,
)


TICK_SIZES = st.sampled_from(
    [
        Decimal("0.0001"),
        Decimal("0.001"),
        Decimal("0.01"),
        Decimal("0.05"),
        Decimal("0.25"),
        Decimal("1"),
    ]
)

PRICES = st.one_of(
    st.sampled_from(
        [
            Decimal("0.1"),
            Decimal("0.2"),
            Decimal("0.3"),
            Decimal(str(0.1 + 0.2)),
            Decimal("2.675"),
            Decimal("999999999.9999"),
        ]
    ),
    st.decimals(
        min_value=Decimal("0.000001"),
        max_value=Decimal("1000000000"),
        places=6,
        allow_nan=False,
        allow_infinity=False,
    ),
)


@given(price=PRICES, tick_size=TICK_SIZES)
def test_rounding_to_tick_is_idempotent(
    price: Decimal, tick_size: Decimal
) -> None:
    rounded_once = round_to_tick(price, tick_size)
    rounded_twice = round_to_tick(rounded_once, tick_size)

    assert rounded_twice == rounded_once
    assert rounded_once % tick_size == 0


@given(
    price=PRICES,
    quantity=st.integers(min_value=0, max_value=1_000_000_000),
)
def test_decimal_notional_matches_independent_string_math(
    price: Decimal, quantity: int
) -> None:
    expected = Decimal(str(price)) * Decimal(str(quantity))
    assert notional(price, quantity) == expected


FILL_LISTS = st.lists(
    st.tuples(
        st.decimals(
            min_value=Decimal("0.0001"),
            max_value=Decimal("100000000"),
            places=4,
            allow_nan=False,
            allow_infinity=False,
        ),
        st.integers(min_value=1, max_value=1_000_000),
    ),
    min_size=1,
    max_size=10,
)


@given(fills=FILL_LISTS)
def test_weighted_average_reconstructs_partial_fill_notional(
    fills: list[tuple[Decimal, int]],
) -> None:
    average = average_fill_price(fills)
    total_quantity = sum((Decimal(str(quantity)) for _, quantity in fills), Decimal("0"))
    expected_notional = sum(
        (
            Decimal(str(price)) * Decimal(str(quantity))
            for price, quantity in fills
        ),
        Decimal("0"),
    )

    with localcontext() as context:
        context.prec = 60
        reconstructed_notional = average * total_quantity
    assert abs(reconstructed_notional - expected_notional) <= Decimal("1e-25")


def test_known_binary_float_notional_drift_is_visible() -> None:
    exact = notional("0.1", "3")
    naive = naive_notional(0.1, 3)

    assert exact == Decimal("0.3")
    assert naive == 0.30000000000000004, (
        "The deliberately naive float path should expose the familiar binary "
        "representation drift for 0.1 * 3"
    )
    assert Decimal(str(naive)) != exact


def test_closed_position_pnl_respects_buy_and_sell_direction() -> None:
    assert pnl("100.10", "101.25", "20", "buy") == Decimal("23.00")
    assert pnl("100.10", "101.25", "20", "sell") == Decimal("-23.00")
    assert naive_pnl(0.1, 0.2, 3, "buy") != float(
        pnl("0.1", "0.2", "3", "buy")
    )


def test_currency_rounding_uses_exact_bankers_rounding() -> None:
    # Half-even avoids a systematic upward bias when many exact half-cent ties are
    # aggregated. float round() sees 2.675 as slightly below the tie and returns 2.67.
    assert round_currency("2.675") == Decimal("2.68")
    assert naive_round_currency(2.675) == 2.67


def test_naive_helpers_are_explicit_float_comparisons() -> None:
    assert isinstance(naive_round_to_tick(100.12, 0.25), float)
    assert isinstance(naive_average_fill_price([(0.1, 1), (0.2, 2)]), float)


@pytest.mark.parametrize("invalid_tick", [0, -1, "NaN", "Infinity"])
def test_invalid_tick_size_is_rejected(invalid_tick: object) -> None:
    with pytest.raises(ValueError):
        round_to_tick("100", invalid_tick)  # type: ignore[arg-type]
