"""Aggregate visible bid/offer book reconstructed from market-data messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .md_message import (
    MDEntryType,
    MDUpdateAction,
    MarketDataIncrementalRefresh,
    MarketDataSnapshotFullRefresh,
)


class BookUpdateError(ValueError):
    """Raised when an incremental cannot be applied to the current visible book."""


@dataclass
class BookState:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    last_seq_num: int | None = None
    stale: bool = False
    last_trade_price: Decimal | None = None
    last_trade_size: Decimal | None = None

    def copy(self) -> "BookState":
        return BookState(
            bids=dict(self.bids),
            asks=dict(self.asks),
            last_seq_num=self.last_seq_num,
            stale=self.stale,
            last_trade_price=self.last_trade_price,
            last_trade_size=self.last_trade_size,
        )


class OrderBook:
    """Per-symbol aggregate market book, distinct from the order-entry book."""

    def __init__(self) -> None:
        self._books: dict[str, BookState] = {}

    @property
    def books(self) -> dict[str, BookState]:
        return {symbol: state.copy() for symbol, state in self._books.items()}

    def get_book(self, symbol: str) -> BookState | None:
        state = self._books.get(symbol)
        return None if state is None else state.copy()

    def mark_stale(self, symbol: str) -> None:
        self._books.setdefault(symbol, BookState()).stale = True

    def apply_snapshot(self, message: MarketDataSnapshotFullRefresh) -> BookState:
        if not isinstance(message, MarketDataSnapshotFullRefresh):
            raise TypeError("apply_snapshot expects MarketDataSnapshotFullRefresh")
        state = BookState(last_seq_num=message.seq_num, stale=False)
        for entry in message.entries:
            if entry.entry_type is MDEntryType.BID:
                if entry.size > 0:
                    state.bids[entry.price] = entry.size
            elif entry.entry_type is MDEntryType.OFFER:
                if entry.size > 0:
                    state.asks[entry.price] = entry.size
            elif entry.entry_type is MDEntryType.TRADE:
                state.last_trade_price = entry.price
                state.last_trade_size = entry.size
        self._books[message.symbol] = state
        return state.copy()

    def apply_incremental(self, message: MarketDataIncrementalRefresh) -> BookState:
        if not isinstance(message, MarketDataIncrementalRefresh):
            raise TypeError("apply_incremental expects MarketDataIncrementalRefresh")
        if message.symbol not in self._books:
            raise BookUpdateError(
                f"No snapshot exists for symbol {message.symbol}; resync is required"
            )
        current = self._books[message.symbol]
        if current.stale:
            raise BookUpdateError(
                f"Book for symbol {message.symbol} is stale; a snapshot is required"
            )
        if current.last_seq_num is not None and message.seq_num != current.last_seq_num + 1:
            raise BookUpdateError(
                f"Expected MsgSeqNum {current.last_seq_num + 1} for {message.symbol}, "
                f"received {message.seq_num}"
            )

        updated = current.copy()
        for entry in message.entries:
            assert entry.update_action is not None
            if entry.entry_type is MDEntryType.TRADE:
                if entry.update_action is not MDUpdateAction.DELETE:
                    updated.last_trade_price = entry.price
                    updated.last_trade_size = entry.size
                continue

            levels = (
                updated.bids if entry.entry_type is MDEntryType.BID else updated.asks
            )
            if entry.update_action is MDUpdateAction.NEW:
                levels[entry.price] = entry.size
            elif entry.update_action is MDUpdateAction.CHANGE:
                if entry.price not in levels:
                    raise BookUpdateError(
                        f"Cannot change missing {entry.entry_type.name.lower()} level "
                        f"{entry.price} for {message.symbol}"
                    )
                levels[entry.price] = entry.size
            elif entry.update_action is MDUpdateAction.DELETE:
                if entry.price not in levels:
                    raise BookUpdateError(
                        f"Cannot delete missing {entry.entry_type.name.lower()} level "
                        f"{entry.price} for {message.symbol}"
                    )
                del levels[entry.price]

        updated.last_seq_num = message.seq_num
        self._books[message.symbol] = updated
        return updated.copy()

