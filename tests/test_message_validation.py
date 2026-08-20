from __future__ import annotations

from fixgateway.fix.message import FixMessage, decode
from fixgateway.fix.validator import validate


def base_header(msg_type: str) -> dict[int, str]:
    return {
        8: "FIX.4.4",
        35: msg_type,
        49: "CLIENT",
        56: "VENUE",
        34: "7",
        52: "20260820-13:30:00.000",
    }


def wire_message(fields: dict[int, str]):
    return decode(FixMessage(fields).encode())


def valid_order() -> dict[int, str]:
    return base_header("D") | {
        11: "C-100",
        55: "MSFT",
        54: "1",
        38: "25",
        40: "2",
        44: "500.00",
        59: "0",
    }


def valid_execution_report() -> dict[int, str]:
    return base_header("8") | {
        37: "V-900",
        11: "C-100",
        150: "1",
        39: "1",
        55: "MSFT",
        54: "1",
        38: "25",
        14: "10",
        151: "15",
        6: "499.75",
    }


def test_valid_new_order_single_passes() -> None:
    result = validate(wire_message(valid_order()))
    assert result.is_valid
    assert result.errors == []


def test_missing_required_tag_fails_clearly() -> None:
    fields = valid_order()
    del fields[55]
    result = validate(wire_message(fields))
    assert not result.is_valid
    assert any("Missing required tag 55" in error for error in result.errors)


def test_invalid_enum_value_fails() -> None:
    fields = valid_order()
    fields[54] = "9"
    result = validate(wire_message(fields))
    assert not result.is_valid
    assert any("Invalid Side (54)" in error for error in result.errors)


def test_bad_checksum_fails() -> None:
    raw = FixMessage(valid_order()).encode()
    corrupted = raw[:-7] + b"10=999\x01"
    result = validate(decode(corrupted))
    assert not result.is_valid
    assert any("CheckSum mismatch" in error for error in result.errors)


def test_execution_report_quantity_invariant_fails() -> None:
    fields = valid_execution_report()
    fields[151] = "14"
    result = validate(wire_message(fields))
    assert not result.is_valid
    assert any("CumQty (10) + LeavesQty (14)" in error for error in result.errors)


def test_valid_execution_report_passes() -> None:
    assert validate(wire_message(valid_execution_report())).is_valid

