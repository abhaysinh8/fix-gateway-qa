"""Business and wire-conformance checks for FIX messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .constants import (
    ExecType,
    MsgType,
    OrdStatus,
    OrdType,
    REQUIRED_TAGS,
    Side,
    Tag,
    TimeInForce,
)
from .message import FixMessage, FixMessageError, calculate_body_length, calculate_checksum, decode


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)


_ENUM_FIELDS = {
    Tag.SIDE: ("Side", Side),
    Tag.ORD_TYPE: ("OrdType", OrdType),
    Tag.ORD_STATUS: ("OrdStatus", OrdStatus),
    Tag.EXEC_TYPE: ("ExecType", ExecType),
    Tag.TIME_IN_FORCE: ("TimeInForce", TimeInForce),
}

_NUMERIC_FIELDS = {
    Tag.ORDER_QTY: "OrderQty",
    Tag.PRICE: "Price",
    Tag.CUM_QTY: "CumQty",
    Tag.LEAVES_QTY: "LeavesQty",
}


def _decimal_value(fields: dict[int, str], tag: Tag, name: str, errors: list[str]) -> Decimal | None:
    if tag not in fields:
        return None
    try:
        value = Decimal(fields[tag])
    except (InvalidOperation, ValueError):
        errors.append(f"{name} ({int(tag)}) must be numeric; got {fields[tag]!r}")
        return None
    if not value.is_finite():
        errors.append(f"{name} ({int(tag)}) must be a finite number; got {fields[tag]!r}")
        return None
    if value < 0:
        errors.append(f"{name} ({int(tag)}) must be non-negative; got {fields[tag]!r}")
        return None
    return value


def validate(message: FixMessage) -> ValidationResult:
    """Validate wire integrity and the project's FIX 4.4 order profile."""

    if not isinstance(message, FixMessage):
        raise TypeError("validate() expects a FixMessage")

    errors: list[str] = []
    supplied_fields = message.fields
    raw = message.raw
    if raw is None:
        raw = message.encode()
        wire_fields = decode(raw).fields
    else:
        wire_fields = supplied_fields

    msg_type_value = wire_fields.get(Tag.MSG_TYPE)
    try:
        msg_type = MsgType(msg_type_value) if msg_type_value is not None else None
    except ValueError:
        msg_type = None
        errors.append(f"Unsupported MsgType (35): {msg_type_value!r}")

    if msg_type is None and msg_type_value is None:
        errors.append("Missing required tag 35 (MsgType)")
    elif msg_type is not None:
        for tag in sorted(REQUIRED_TAGS[msg_type], key=int):
            if int(tag) not in wire_fields:
                errors.append(f"Missing required tag {int(tag)} ({tag.name})")

    try:
        expected_body_length = calculate_body_length(raw)
        declared_body_length = wire_fields.get(Tag.BODY_LENGTH)
        if declared_body_length is None:
            errors.append("Missing BodyLength (9)")
        else:
            try:
                actual_declared_length = int(declared_body_length)
            except ValueError:
                errors.append(f"BodyLength (9) must be an integer; got {declared_body_length!r}")
            else:
                if actual_declared_length != expected_body_length:
                    errors.append(
                        "BodyLength mismatch: "
                        f"declared {actual_declared_length}, calculated {expected_body_length}"
                    )
    except FixMessageError as exc:
        errors.append(str(exc))

    checksum_value = wire_fields.get(Tag.CHECK_SUM)
    expected_checksum = calculate_checksum(raw)
    if checksum_value is None:
        errors.append("Missing CheckSum (10)")
    elif len(checksum_value) != 3 or not checksum_value.isdigit():
        errors.append(f"CheckSum (10) must be exactly three digits; got {checksum_value!r}")
    elif int(checksum_value) != expected_checksum:
        errors.append(
            f"CheckSum mismatch: declared {checksum_value}, calculated {expected_checksum:03d}"
        )

    # If a caller supplied wire-derived fields without raw bytes, do not silently
    # overlook deliberately bad declared values just because encode() repaired them.
    if message.raw is None:
        for tag, label in ((Tag.BODY_LENGTH, "BodyLength"), (Tag.CHECK_SUM, "CheckSum")):
            if tag in supplied_fields and supplied_fields[tag] != wire_fields[tag]:
                errors.append(
                    f"{label} ({int(tag)}) does not match the automatically calculated value "
                    f"{wire_fields[tag]}"
                )

    for tag, (name, enum_type) in _ENUM_FIELDS.items():
        if tag in wire_fields:
            allowed = {member.value for member in enum_type}
            if wire_fields[tag] not in allowed:
                errors.append(
                    f"Invalid {name} ({int(tag)}) value {wire_fields[tag]!r}; "
                    f"expected one of {sorted(allowed)}"
                )

    numeric_values: dict[Tag, Decimal] = {}
    for tag, name in _NUMERIC_FIELDS.items():
        value = _decimal_value(wire_fields, tag, name, errors)
        if value is not None:
            numeric_values[tag] = value

    if msg_type == MsgType.EXECUTION_REPORT and all(
        tag in numeric_values for tag in (Tag.CUM_QTY, Tag.LEAVES_QTY, Tag.ORDER_QTY)
    ):
        cum_qty = numeric_values[Tag.CUM_QTY]
        leaves_qty = numeric_values[Tag.LEAVES_QTY]
        order_qty = numeric_values[Tag.ORDER_QTY]
        if cum_qty + leaves_qty != order_qty:
            errors.append(
                "ExecutionReport quantity invariant violated: "
                f"CumQty ({cum_qty}) + LeavesQty ({leaves_qty}) "
                f"must equal OrderQty ({order_qty})"
            )

    return ValidationResult(is_valid=not errors, errors=errors)

