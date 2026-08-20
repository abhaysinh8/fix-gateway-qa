from __future__ import annotations

from decimal import Decimal

from fixgateway.marketdata.book_reconstructor import BookState
from fixgateway.marketdata.conformance_checker import feed_replay
from fixgateway.marketdata.md_message import (
    MDEntryType,
    MDUpdateAction,
    MarketDataEntry,
    MarketDataIncrementalRefresh,
    MarketDataSnapshotFullRefresh,
    decode_market_data,
)


def entry(
    entry_type: MDEntryType,
    price: str,
    size: str,
    action: MDUpdateAction | None = None,
) -> MarketDataEntry:
    return MarketDataEntry(entry_type, price, size, action)


def valid_book_evolution():
    return [
        MarketDataSnapshotFullRefresh(
            "AAPL",
            1,
            [
                entry(MDEntryType.BID, "100.00", "10"),
                entry(MDEntryType.BID, "99.00", "20"),
                entry(MDEntryType.OFFER, "101.00", "12"),
                entry(MDEntryType.OFFER, "102.00", "15"),
            ],
        ),
        MarketDataIncrementalRefresh(
            "AAPL",
            2,
            [entry(MDEntryType.BID, "100.00", "15", MDUpdateAction.CHANGE)],
        ),
        MarketDataIncrementalRefresh(
            "AAPL",
            3,
            [entry(MDEntryType.OFFER, "102.00", "0", MDUpdateAction.DELETE)],
        ),
        MarketDataIncrementalRefresh(
            "AAPL",
            4,
            [entry(MDEntryType.BID, "98.00", "30", MDUpdateAction.NEW)],
        ),
        MarketDataIncrementalRefresh(
            "AAPL",
            5,
            [entry(MDEntryType.OFFER, "101.00", "8", MDUpdateAction.CHANGE)],
        ),
    ]


def expected_final_book(*, stale: bool = False, last_seq_num: int = 5) -> BookState:
    return BookState(
        bids={
            Decimal("100.00"): Decimal("15"),
            Decimal("99.00"): Decimal("20"),
            Decimal("98.00"): Decimal("30"),
        },
        asks={Decimal("101.00"): Decimal("8")},
        last_seq_num=last_seq_num,
        stale=stale,
    )


def test_correct_sequence_reconstructs_exact_book() -> None:
    messages = valid_book_evolution()
    # Exercise repeating-group wire encoding/decoding, not only Python objects.
    wire_messages = [message.encode() for message in messages]
    assert len(decode_market_data(wire_messages[0]).entries) == 4

    result = feed_replay(wire_messages)

    assert result.sequence_gaps == []
    assert result.duplicate_messages == []
    assert result.out_of_order_messages == []
    assert result.all_checksums_valid
    assert not result.request_resync
    assert result.final_books["AAPL"] == expected_final_book()


def test_dropped_incremental_detects_gap_and_marks_book_stale() -> None:
    messages = valid_book_evolution()
    del messages[2]  # Drop MsgSeqNum 3; sequence now jumps from 2 to 4.

    result = feed_replay(messages)

    assert len(result.sequence_gaps) == 1
    assert result.sequence_gaps[0].first_missing == 3
    assert result.sequence_gaps[0].last_missing == 3
    assert result.request_resync
    assert result.stale_symbols == {"AAPL"}
    assert result.final_books["AAPL"].stale
    assert result.final_books["AAPL"].last_seq_num == 2


def test_duplicate_message_is_idempotently_ignored() -> None:
    messages = valid_book_evolution()
    messages.insert(2, messages[1])

    result = feed_replay(messages)

    assert [(item.symbol, item.seq_num) for item in result.duplicate_messages] == [
        ("AAPL", 2)
    ]
    assert not result.request_resync
    assert result.final_books["AAPL"] == expected_final_book()


def test_adjacent_reordering_is_flagged_and_requires_snapshot_resync() -> None:
    # This implementation intentionally does not buffer. Receiving 3 before 2 first
    # creates a gap, then identifies 2 as too-late/out-of-order; both are ignored.
    messages = valid_book_evolution()
    messages[1], messages[2] = messages[2], messages[1]

    result = feed_replay(messages)

    assert result.sequence_gaps[0].first_missing == 2
    assert [(item.seq_num, item.buffered) for item in result.out_of_order_messages] == [
        (2, False)
    ]
    assert result.out_of_order_messages[0].recovery == "unrecoverable_requires_snapshot"
    assert result.request_resync
    assert result.final_books["AAPL"].stale


def test_bad_checksum_is_reported_and_message_is_not_applied() -> None:
    raw = valid_book_evolution()[0].encode()
    corrupted = raw[:-7] + b"10=999\x01"

    result = feed_replay([corrupted])

    assert not result.all_checksums_valid
    assert "CheckSum mismatch" in result.checksum_results[0].errors[0]
    assert result.request_resync
    assert result.final_books["AAPL"].stale
    assert result.final_books["AAPL"].last_seq_num is None
