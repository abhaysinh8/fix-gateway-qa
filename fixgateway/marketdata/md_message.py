"""FIX-like snapshot and incremental market-data messages with repeating groups."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, ClassVar

from fixgateway.fix.constants import SOH
from fixgateway.fix.message import FixDecodeError, FixMessageError, calculate_checksum


class MDEntryType(str, Enum):
    BID = "0"
    OFFER = "1"
    TRADE = "2"


class MDUpdateAction(str, Enum):
    NEW = "0"
    CHANGE = "1"
    DELETE = "2"


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be numeric; got {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be finite and non-negative; got {value!r}")
    return result


def _enum_value(value: Any, enum_type: type[Enum], name: str):
    try:
        return value if isinstance(value, enum_type) else enum_type(str(value))
    except ValueError as exc:
        raise ValueError(f"Invalid {name}: {value!r}") from exc


@dataclass(frozen=True)
class MarketDataEntry:
    entry_type: MDEntryType | str
    price: Decimal | int | float | str
    size: Decimal | int | float | str
    update_action: MDUpdateAction | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entry_type",
            _enum_value(self.entry_type, MDEntryType, "MDEntryType"),
        )
        object.__setattr__(self, "price", _decimal(self.price, "MDEntryPx"))
        object.__setattr__(self, "size", _decimal(self.size, "MDEntrySize"))
        if self.update_action is not None:
            object.__setattr__(
                self,
                "update_action",
                _enum_value(self.update_action, MDUpdateAction, "MDUpdateAction"),
            )


def _sending_time() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


@dataclass
class MarketDataMessage:
    symbol: str
    seq_num: int
    entries: list[MarketDataEntry]
    sender_comp_id: str = "MARKET_DATA"
    target_comp_id: str = "MD_CONSUMER"
    sending_time: str = field(default_factory=_sending_time)
    _raw: bytes | None = field(default=None, repr=False, compare=False)

    MSG_TYPE: ClassVar[str]

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        self.seq_num = int(self.seq_num)
        if self.seq_num <= 0:
            raise ValueError("seq_num must be greater than zero")
        self.entries = [
            entry if isinstance(entry, MarketDataEntry) else MarketDataEntry(**entry)
            for entry in self.entries
        ]
        if not self.entries:
            raise ValueError("A market data message must contain at least one entry")
        if self.MSG_TYPE == "X" and any(
            entry.update_action is None for entry in self.entries
        ):
            raise ValueError("Incremental entries require MDUpdateAction (279)")

    @property
    def msg_type(self) -> str:
        return self.MSG_TYPE

    @property
    def raw(self) -> bytes | None:
        return self._raw

    def encode(self) -> bytes:
        body_pairs: list[tuple[int, str]] = [
            (35, self.MSG_TYPE),
            (49, self.sender_comp_id),
            (56, self.target_comp_id),
            (34, str(self.seq_num)),
            (52, self.sending_time),
            (55, self.symbol),
            (268, str(len(self.entries))),
        ]
        for entry in self.entries:
            if self.MSG_TYPE == "X":
                assert entry.update_action is not None
                body_pairs.append((279, entry.update_action.value))
            body_pairs.extend(
                [
                    (269, entry.entry_type.value),
                    (270, str(entry.price)),
                    (271, str(entry.size)),
                ]
            )
        try:
            body = b"".join(
                f"{tag}={value}{SOH}".encode("ascii") for tag, value in body_pairs
            )
            begin = f"8=FIX.4.4{SOH}".encode("ascii")
        except UnicodeEncodeError as exc:
            raise FixMessageError("Market data FIX messages must be ASCII") from exc
        prefix = begin + f"9={len(body)}{SOH}".encode("ascii") + body
        return prefix + f"10={calculate_checksum(prefix):03d}{SOH}".encode("ascii")


@dataclass
class MarketDataSnapshotFullRefresh(MarketDataMessage):
    MSG_TYPE: ClassVar[str] = "W"


@dataclass
class MarketDataIncrementalRefresh(MarketDataMessage):
    MSG_TYPE: ClassVar[str] = "X"


def _parse_pairs(raw: bytes | str) -> tuple[bytes, list[tuple[int, str]]]:
    try:
        data = raw if isinstance(raw, bytes) else raw.encode("ascii")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise FixDecodeError("Market data FIX input must be ASCII bytes or str") from exc
    if not data.endswith(b"\x01"):
        raise FixDecodeError("Truncated market data message: missing trailing SOH")
    pairs: list[tuple[int, str]] = []
    for index, chunk in enumerate(data[:-1].split(b"\x01"), start=1):
        if b"=" not in chunk:
            raise FixDecodeError(f"Malformed field at position {index}: missing '='")
        raw_tag, raw_value = chunk.split(b"=", 1)
        try:
            tag = int(raw_tag)
            value = raw_value.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise FixDecodeError(f"Malformed field at position {index}") from exc
        pairs.append((tag, value))
    if len(pairs) < 4 or pairs[0][0] != 8 or pairs[1][0] != 9 or pairs[-1][0] != 10:
        raise FixDecodeError("Malformed market data FIX header or trailer")
    return data, pairs


def decode_market_data(raw: bytes | str) -> MarketDataMessage:
    """Decode W/X messages while preserving their repeated entry tags."""

    data, pairs = _parse_pairs(raw)
    try:
        group_index = next(index for index, pair in enumerate(pairs) if pair[0] == 268)
    except StopIteration as exc:
        raise FixDecodeError("Market data message is missing NoMDEntries (268)") from exc
    header = dict(pairs[: group_index + 1])
    try:
        msg_type = header[35]
        entry_count = int(header[268])
        symbol = header[55]
        seq_num = int(header[34])
    except (KeyError, ValueError) as exc:
        raise FixDecodeError("Market data message has an invalid required header field") from exc
    if msg_type not in {"W", "X"}:
        raise FixDecodeError(f"Unsupported market data MsgType: {msg_type!r}")

    group_pairs = pairs[group_index + 1 : -1]
    group_width = 3 if msg_type == "W" else 4
    if len(group_pairs) != entry_count * group_width:
        raise FixDecodeError(
            f"NoMDEntries (268) declares {entry_count}, but repeating group size is invalid"
        )
    entries: list[MarketDataEntry] = []
    for offset in range(0, len(group_pairs), group_width):
        group = group_pairs[offset : offset + group_width]
        expected_tags = [269, 270, 271] if msg_type == "W" else [279, 269, 270, 271]
        if [tag for tag, _ in group] != expected_tags:
            raise FixDecodeError(
                f"Invalid market data repeating-group tag order at entry "
                f"{offset // group_width + 1}"
            )
        values = dict(group)
        entries.append(
            MarketDataEntry(
                entry_type=values[269],
                price=values[270],
                size=values[271],
                update_action=values.get(279),
            )
        )

    message_class = (
        MarketDataSnapshotFullRefresh if msg_type == "W" else MarketDataIncrementalRefresh
    )
    return message_class(
        symbol=symbol,
        seq_num=seq_num,
        entries=entries,
        sender_comp_id=header.get(49, "MARKET_DATA"),
        target_comp_id=header.get(56, "MD_CONSUMER"),
        sending_time=header.get(52, ""),
        _raw=data,
    )


decode_md_message = decode_market_data

