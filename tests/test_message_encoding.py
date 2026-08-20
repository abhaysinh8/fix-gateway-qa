from __future__ import annotations

import pytest

from fixgateway.fix.constants import SOH
from fixgateway.fix.message import (
    FixDecodeError,
    FixMessage,
    calculate_body_length,
    calculate_checksum,
    decode,
)


def order_fields() -> dict[int, str]:
    return {
        8: "FIX.4.4",
        35: "D",
        49: "BUY_SIDE",
        56: "EXCHANGE",
        34: "1",
        52: "20260820-13:30:00.000",
        11: "ORDER-1",
        55: "AAPL",
        54: "1",
        38: "100",
        40: "2",
        44: "225.50",
        59: "0",
    }


def test_round_trip_preserves_all_application_fields() -> None:
    fields = order_fields()
    encoded = FixMessage(fields).encode()
    parsed = decode(encoded)

    for tag, value in fields.items():
        assert parsed[tag] == value
    assert parsed[9] == str(calculate_body_length(encoded))
    assert parsed[10] == f"{calculate_checksum(encoded):03d}"
    assert encoded.endswith(f"10={parsed[10]}{SOH}".encode())


def test_checksum_for_known_fix_example() -> None:
    # This compact fixture was calculated independently as sum(prefix) % 256.
    prefix = b"8=FIX.4.4\x019=5\x0135=0\x01"
    assert calculate_checksum(prefix) == 163
    assert decode(prefix + b"10=163\x01")[10] == "163"


def test_body_length_counts_from_after_tag_9_to_before_tag_10() -> None:
    raw = b"8=FIX.4.4\x019=12\x0135=D\x0111=A\x0155=X\x0110=000\x01"
    assert calculate_body_length(raw) == len(b"35=D\x0111=A\x0155=X\x01")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"8=FIX.4.4\x019=5\x0135=D", "trailing SOH"),
        (b"8=FIX.4.4\x019=5\x0135=D\x01", "tag 10"),
        (b"8=FIX.4.4\x019=5\x0135\x0110=000\x01", "missing '='"),
        (b"8=FIX.4.4\x0135=D\x019=5\x0110=000\x01", "tag 9 must be second"),
    ],
)
def test_decode_rejects_malformed_or_truncated_input(raw: bytes, message: str) -> None:
    with pytest.raises(FixDecodeError, match=message):
        decode(raw)

