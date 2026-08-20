"""Replay FIX-like market data and report delivery/book conformance."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from fixgateway.fix.message import FixDecodeError
from fixgateway.fix.validator import validate_wire_integrity

from .book_reconstructor import BookState, BookUpdateError, OrderBook
from .md_message import (
    MarketDataIncrementalRefresh,
    MarketDataMessage,
    MarketDataSnapshotFullRefresh,
    decode_market_data,
)


@dataclass(frozen=True)
class SequenceGap:
    symbol: str
    previous_seq_num: int
    first_missing: int
    last_missing: int
    received_seq_num: int


@dataclass(frozen=True)
class DuplicateMessage:
    symbol: str
    seq_num: int


@dataclass(frozen=True)
class OutOfOrderMessage:
    symbol: str
    seq_num: int
    highest_seen_seq_num: int
    buffered: bool = False
    recovery: str = "unrecoverable_requires_snapshot"


@dataclass(frozen=True)
class ChecksumValidation:
    symbol: str | None
    seq_num: int | None
    is_valid: bool
    errors: tuple[str, ...]


@dataclass
class ConformanceResult:
    sequence_gaps: list[SequenceGap] = field(default_factory=list)
    duplicate_messages: list[DuplicateMessage] = field(default_factory=list)
    out_of_order_messages: list[OutOfOrderMessage] = field(default_factory=list)
    checksum_results: list[ChecksumValidation] = field(default_factory=list)
    book_errors: list[str] = field(default_factory=list)
    request_resync: bool = False
    stale_symbols: set[str] = field(default_factory=set)
    final_books: dict[str, BookState] = field(default_factory=dict)

    @property
    def all_checksums_valid(self) -> bool:
        return all(check.is_valid for check in self.checksum_results)

    @property
    def duplicates(self) -> list[DuplicateMessage]:
        return self.duplicate_messages

    @property
    def out_of_order(self) -> list[OutOfOrderMessage]:
        return self.out_of_order_messages


def feed_replay(
    messages: Iterable[MarketDataMessage | bytes | str],
) -> ConformanceResult:
    """Replay messages in delivery order using snapshot-only gap recovery.

    No reorder buffer is used. Once a gap or out-of-order packet is observed, the
    symbol is stale and incrementals are ignored until a newer full snapshot arrives.
    """

    result = ConformanceResult()
    reconstructor = OrderBook()
    seen: dict[str, set[int]] = {}
    highest_seen: dict[str, int] = {}

    for supplied_message in messages:
        raw = (
            supplied_message.encode()
            if isinstance(supplied_message, MarketDataMessage)
            else supplied_message
        )
        integrity = validate_wire_integrity(raw)
        try:
            message = decode_market_data(raw)
        except FixDecodeError as exc:
            result.checksum_results.append(
                ChecksumValidation(None, None, integrity.is_valid, tuple(integrity.errors))
            )
            result.book_errors.append(f"Undecodable market data message: {exc}")
            result.request_resync = True
            continue

        result.checksum_results.append(
            ChecksumValidation(
                message.symbol,
                message.seq_num,
                integrity.is_valid,
                tuple(integrity.errors),
            )
        )
        if not integrity.is_valid:
            reconstructor.mark_stale(message.symbol)
            result.request_resync = True
            continue

        symbol_seen = seen.setdefault(message.symbol, set())
        if message.seq_num in symbol_seen:
            result.duplicate_messages.append(
                DuplicateMessage(message.symbol, message.seq_num)
            )
            continue

        previous_highest = highest_seen.get(message.symbol)
        if previous_highest is not None and message.seq_num < previous_highest:
            symbol_seen.add(message.seq_num)
            result.out_of_order_messages.append(
                OutOfOrderMessage(
                    message.symbol, message.seq_num, previous_highest
                )
            )
            reconstructor.mark_stale(message.symbol)
            result.request_resync = True
            continue

        symbol_seen.add(message.seq_num)
        highest_seen[message.symbol] = max(
            message.seq_num, previous_highest or message.seq_num
        )

        if isinstance(message, MarketDataSnapshotFullRefresh):
            if previous_highest is not None and message.seq_num > previous_highest + 1:
                result.sequence_gaps.append(
                    SequenceGap(
                        message.symbol,
                        previous_highest,
                        previous_highest + 1,
                        message.seq_num - 1,
                        message.seq_num,
                    )
                )
            reconstructor.apply_snapshot(message)
            continue

        assert isinstance(message, MarketDataIncrementalRefresh)
        if previous_highest is None:
            result.book_errors.append(
                f"Incremental MsgSeqNum {message.seq_num} for {message.symbol} "
                "arrived before a snapshot"
            )
            reconstructor.mark_stale(message.symbol)
            result.request_resync = True
            continue
        if message.seq_num > previous_highest + 1:
            result.sequence_gaps.append(
                SequenceGap(
                    message.symbol,
                    previous_highest,
                    previous_highest + 1,
                    message.seq_num - 1,
                    message.seq_num,
                )
            )
            reconstructor.mark_stale(message.symbol)
            result.request_resync = True
            continue

        current = reconstructor.get_book(message.symbol)
        if current is None or current.stale:
            result.request_resync = True
            continue
        try:
            reconstructor.apply_incremental(message)
        except BookUpdateError as exc:
            result.book_errors.append(str(exc))
            reconstructor.mark_stale(message.symbol)
            result.request_resync = True

    result.final_books = reconstructor.books
    result.stale_symbols = {
        symbol for symbol, state in result.final_books.items() if state.stale
    }
    result.request_resync = bool(result.stale_symbols) or result.request_resync
    return result

