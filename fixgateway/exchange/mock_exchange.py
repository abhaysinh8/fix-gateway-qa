"""Threaded TCP mock exchange for end-to-end FIX gateway tests."""

from __future__ import annotations

import itertools
import logging
import random
import socket
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from fixgateway.fix.constants import MsgType, OrdStatus, Side, Tag
from fixgateway.fix.framing import FixConnectionClosedError, FixStreamReader
from fixgateway.fix.message import FixDecodeError, FixMessage, decode
from fixgateway.fix.validator import validate

from .order_book import Order, OrderBook, OrderNotFoundError
from .state_machine import InvalidTransitionError, OrderEvent


LOGGER = logging.getLogger(__name__)

FillStrategy = str | Callable[[Order], Sequence[Decimal | int | str]]


@dataclass(frozen=True)
class MessageLogEntry:
    direction: str
    timestamp: datetime
    peer: tuple[str, int]
    raw: bytes
    message: FixMessage | None


@dataclass(frozen=True)
class AuditLogEntry:
    """Normalized, queryable view of one exchange wire-log entry."""

    timestamp: datetime
    direction: str
    delivery_status: str
    msg_type: str | None
    order_id: str | None
    cl_ord_id: str | None
    orig_cl_ord_id: str | None
    exchange_order_id: str | None
    ord_status: str | None
    exec_type: str | None
    cum_qty: str | None
    leaves_qty: str | None
    fields: dict[int, str]
    raw: bytes


def _audit_entry(entry: MessageLogEntry) -> AuditLogEntry:
    fields = entry.message.fields if entry.message is not None else {}
    msg_type = fields.get(Tag.MSG_TYPE)
    cl_ord_id = fields.get(Tag.CL_ORD_ID)
    orig_cl_ord_id = fields.get(Tag.ORIG_CL_ORD_ID)
    order_id = orig_cl_ord_id if orig_cl_ord_id else cl_ord_id
    return AuditLogEntry(
        timestamp=entry.timestamp,
        direction="inbound" if entry.direction == "RECEIVED" else "outbound",
        delivery_status=entry.direction.lower(),
        msg_type=msg_type,
        order_id=order_id,
        cl_ord_id=cl_ord_id,
        orig_cl_ord_id=orig_cl_ord_id,
        exchange_order_id=fields.get(Tag.ORDER_ID),
        ord_status=fields.get(Tag.ORD_STATUS),
        exec_type=fields.get(Tag.EXEC_TYPE),
        cum_qty=fields.get(Tag.CUM_QTY),
        leaves_qty=fields.get(Tag.LEAVES_QTY),
        fields=fields,
        raw=entry.raw,
    )


def _fix_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


class MockExchange:
    """Accept FIX sessions and route order flow through the Phase 2 order book."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        processing_delay_ms: tuple[float, float] = (2.0, 5.0),
        fill_strategy: FillStrategy = "resting",
        sender_comp_id: str = "MOCK_EXCHANGE",
        chaos_mode: bool = False,
        chaos_rate: float = 0.10,
        chaos_drop_probability: float = 0.50,
        chaos_extra_delay_ms: tuple[float, float] = (200.0, 1_000.0),
        random_seed: int | None = None,
    ) -> None:
        low_delay, high_delay = processing_delay_ms
        if low_delay < 0 or high_delay < low_delay:
            raise ValueError("processing_delay_ms must be a non-negative (min, max) range")
        if not callable(fill_strategy) and fill_strategy not in {
            "resting",
            "full",
            "partial_then_full",
            "two_partial_then_full",
        }:
            raise ValueError(f"Unsupported fill strategy: {fill_strategy!r}")
        if not 0 <= chaos_rate <= 1:
            raise ValueError("chaos_rate must be between zero and one")
        if not 0 <= chaos_drop_probability <= 1:
            raise ValueError("chaos_drop_probability must be between zero and one")
        chaos_low, chaos_high = chaos_extra_delay_ms
        if chaos_low < 0 or chaos_high < chaos_low:
            raise ValueError(
                "chaos_extra_delay_ms must be a non-negative (min, max) range"
            )

        self.host = host
        self.port = port
        self.processing_delay_ms = (float(low_delay), float(high_delay))
        self.fill_strategy = fill_strategy
        self.sender_comp_id = sender_comp_id
        self.chaos_mode = chaos_mode
        self.chaos_rate = chaos_rate
        self.chaos_drop_probability = chaos_drop_probability
        self.chaos_extra_delay_ms = (float(chaos_low), float(chaos_high))
        self.order_book = OrderBook()
        self.message_log: list[MessageLogEntry] = []

        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._client_threads: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._order_ids = itertools.count(1)
        self._random = random.Random(random_seed)

    @property
    def is_running(self) -> bool:
        return self._accept_thread is not None and self._accept_thread.is_alive()

    @property
    def audit_log(self) -> list[AuditLogEntry]:
        """Return a snapshot suitable for independent audit reconstruction."""

        with self._lock:
            entries = list(self.message_log)
        return [_audit_entry(entry) for entry in entries]

    def start(self) -> "MockExchange":
        if self.is_running:
            return self

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen()
        listener.settimeout(0.2)
        self.port = listener.getsockname()[1]
        self._listener = listener
        self._stop_event.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name=f"mock-exchange-{self.port}",
            daemon=True,
        )
        self._accept_thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)
            self._accept_thread = None
        with self._lock:
            threads = list(self._client_threads)
        for thread in threads:
            thread.join(timeout=2)

    def __enter__(self) -> "MockExchange":
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        listener = self._listener
        while not self._stop_event.is_set():
            try:
                connection, peer = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            connection.settimeout(0.2)
            thread = threading.Thread(
                target=self._handle_connection,
                args=(connection, peer),
                name=f"fix-session-{peer[0]}-{peer[1]}",
                daemon=True,
            )
            with self._lock:
                self._connections.add(connection)
                self._client_threads.add(thread)
            thread.start()

    def _handle_connection(
        self, connection: socket.socket, peer: tuple[str, int]
    ) -> None:
        reader = FixStreamReader()
        outgoing_sequence = itertools.count(1)
        client_comp_id = "FIX_CLIENT"
        try:
            logon = self._receive(connection, reader, peer)
            if logon.get(Tag.MSG_TYPE) != "A":
                self._send_session_reject(
                    connection,
                    peer,
                    client_comp_id,
                    outgoing_sequence,
                    "First message must be Logon (35=A)",
                )
                return
            client_comp_id = logon.get(Tag.SENDER_COMP_ID, client_comp_id)
            self._send(
                connection,
                peer,
                FixMessage(
                    self._header("A", client_comp_id, outgoing_sequence)
                    | {98: "0", 108: logon.get(108, "30")}
                ),
            )

            while not self._stop_event.is_set():
                try:
                    message = self._receive(connection, reader, peer)
                except TimeoutError:
                    continue
                msg_type = message.get(Tag.MSG_TYPE)
                if msg_type == MsgType.NEW_ORDER_SINGLE.value:
                    self._handle_new_order(
                        connection, peer, client_comp_id, outgoing_sequence, message
                    )
                elif msg_type == MsgType.ORDER_CANCEL_REQUEST.value:
                    self._handle_cancel(
                        connection, peer, client_comp_id, outgoing_sequence, message
                    )
                else:
                    self._send_session_reject(
                        connection,
                        peer,
                        client_comp_id,
                        outgoing_sequence,
                        f"Unsupported MsgType: {msg_type!r}",
                    )
        except (FixConnectionClosedError, ConnectionError, OSError):
            pass
        except FixDecodeError as exc:
            LOGGER.warning("Closing malformed FIX session from %s: %s", peer, exc)
        finally:
            with self._lock:
                self._connections.discard(connection)
                self._client_threads.discard(threading.current_thread())
            connection.close()

    def _receive(
        self,
        connection: socket.socket,
        reader: FixStreamReader,
        peer: tuple[str, int],
    ) -> FixMessage:
        raw = reader.receive(connection)
        try:
            message = decode(raw)
        except FixDecodeError:
            self._record("RECEIVED", peer, raw, None)
            raise
        self._record("RECEIVED", peer, raw, message)
        return message

    def _send(
        self,
        connection: socket.socket,
        peer: tuple[str, int],
        message: FixMessage,
    ) -> None:
        raw = message.encode()
        is_execution_report = message.get(Tag.MSG_TYPE) == MsgType.EXECUTION_REPORT.value
        apply_chaos = (
            self.chaos_mode
            and is_execution_report
            and self._random.random() < self.chaos_rate
        )

        delay = self._random.uniform(*self.processing_delay_ms) / 1_000
        if delay:
            time.sleep(delay)
        if apply_chaos and self._random.random() < self.chaos_drop_probability:
            self._record("DROPPED", peer, raw, decode(raw))
            return
        if apply_chaos:
            extra_delay = self._random.uniform(*self.chaos_extra_delay_ms) / 1_000
            if extra_delay:
                time.sleep(extra_delay)
        connection.sendall(raw)
        self._record("SENT", peer, raw, decode(raw))

    def _record(
        self,
        direction: str,
        peer: tuple[str, int],
        raw: bytes,
        message: FixMessage | None,
    ) -> None:
        entry = MessageLogEntry(direction, datetime.now(UTC), peer, raw, message)
        with self._lock:
            self.message_log.append(entry)
        LOGGER.debug("%s %s %r", direction, peer, raw)

    def _header(
        self,
        msg_type: str,
        target_comp_id: str,
        sequence: itertools.count,
    ) -> dict[int, str]:
        return {
            8: "FIX.4.4",
            35: msg_type,
            49: self.sender_comp_id,
            56: target_comp_id,
            34: str(next(sequence)),
            52: _fix_timestamp(),
        }

    def _handle_new_order(
        self,
        connection: socket.socket,
        peer: tuple[str, int],
        target: str,
        sequence: itertools.count,
        message: FixMessage,
    ) -> None:
        result = validate(message)
        if not result.is_valid:
            self._send(
                connection,
                peer,
                self._rejected_execution_report(message, target, sequence, result.errors),
            )
            return

        cl_ord_id = message[Tag.CL_ORD_ID]
        try:
            with self._lock:
                order = self.order_book.accept_order(
                    cl_ord_id,
                    message[Tag.SYMBOL],
                    message[Tag.SIDE],
                    message[Tag.ORDER_QTY],
                    message.get(Tag.PRICE),
                )
                exchange_order_id = f"MOCK-{next(self._order_ids)}"
                setattr(order, "exchange_order_id", exchange_order_id)
        except ValueError as exc:
            self._send(
                connection,
                peer,
                self._rejected_execution_report(message, target, sequence, [str(exc)]),
            )
            return

        self._send(
            connection,
            peer,
            self._execution_report(order, target, sequence, OrderEvent.ACK),
        )
        for fill_quantity in self._fill_quantities(order):
            with self._lock:
                self.order_book.fill_order(order.order_id, fill_quantity)
                event = order.state_machine.history[-1].event
                report = self._execution_report(order, target, sequence, event)
            self._send(connection, peer, report)

    def _handle_cancel(
        self,
        connection: socket.socket,
        peer: tuple[str, int],
        target: str,
        sequence: itertools.count,
        message: FixMessage,
    ) -> None:
        result = validate(message)
        original_cl_ord_id = message.get(Tag.ORIG_CL_ORD_ID, "UNKNOWN")
        if not result.is_valid:
            self._send(
                connection,
                peer,
                self._cancel_reject(
                    message, target, sequence, "; ".join(result.errors), "NONE"
                ),
            )
            return

        try:
            with self._lock:
                order = self.order_book.cancel_order(original_cl_ord_id)
                report = self._execution_report(
                    order,
                    target,
                    sequence,
                    OrderEvent.CANCEL_CONFIRMED,
                    cl_ord_id=message[Tag.CL_ORD_ID],
                    orig_cl_ord_id=original_cl_ord_id,
                )
        except (InvalidTransitionError, OrderNotFoundError) as exc:
            try:
                order_id = getattr(
                    self.order_book.get_order(original_cl_ord_id),
                    "exchange_order_id",
                    "NONE",
                )
            except OrderNotFoundError:
                order_id = "NONE"
            self._send(
                connection,
                peer,
                self._cancel_reject(message, target, sequence, str(exc), order_id),
            )
            return
        self._send(connection, peer, report)

    def _fill_quantities(self, order: Order) -> list[Decimal]:
        if callable(self.fill_strategy):
            return [Decimal(str(value)) for value in self.fill_strategy(order)]
        if self.fill_strategy == "resting":
            return []
        if self.fill_strategy == "full":
            return [order.quantity]
        if self.fill_strategy == "partial_then_full":
            first = order.quantity / 2
            return [first, order.quantity - first]
        first = order.quantity / 4
        second = order.quantity / 4
        return [first, second, order.quantity - first - second]

    def _execution_report(
        self,
        order: Order,
        target: str,
        sequence: itertools.count,
        event: OrderEvent,
        *,
        cl_ord_id: str | None = None,
        orig_cl_ord_id: str | None = None,
    ) -> FixMessage:
        exec_type, status = {
            OrderEvent.ACK: ("0", OrdStatus.NEW.value),
            OrderEvent.PARTIAL_FILL: ("1", OrdStatus.PARTIALLY_FILLED.value),
            OrderEvent.FULL_FILL: ("2", OrdStatus.FILLED.value),
            OrderEvent.CANCEL_CONFIRMED: ("4", OrdStatus.CANCELED.value),
        }[event]
        average_price = order.price if order.cumulative_quantity else Decimal("0")
        fields = self._header(MsgType.EXECUTION_REPORT.value, target, sequence) | {
            37: getattr(order, "exchange_order_id"),
            11: cl_ord_id or order.order_id,
            17: f"EXEC-{next(self._order_ids)}",
            150: exec_type,
            39: status,
            55: order.symbol,
            54: order.side.value,
            38: str(order.quantity),
            14: str(order.cumulative_quantity),
            151: str(order.leaves_quantity),
            6: str(average_price),
        }
        if orig_cl_ord_id is not None:
            fields[Tag.ORIG_CL_ORD_ID] = orig_cl_ord_id
        return FixMessage(fields)

    def _rejected_execution_report(
        self,
        request: FixMessage,
        target: str,
        sequence: itertools.count,
        errors: list[str],
    ) -> FixMessage:
        return FixMessage(
            self._header(MsgType.EXECUTION_REPORT.value, target, sequence)
            | {
                37: "NONE",
                11: request.get(Tag.CL_ORD_ID, "UNKNOWN"),
                17: f"EXEC-{next(self._order_ids)}",
                150: "8",
                39: OrdStatus.REJECTED.value,
                55: request.get(Tag.SYMBOL, "UNKNOWN"),
                54: request.get(Tag.SIDE, Side.BUY.value),
                38: request.get(Tag.ORDER_QTY, "0"),
                14: "0",
                151: "0",
                6: "0",
                58: "; ".join(errors),
            }
        )

    def _cancel_reject(
        self,
        request: FixMessage,
        target: str,
        sequence: itertools.count,
        reason: str,
        order_id: str,
    ) -> FixMessage:
        return FixMessage(
            self._header(MsgType.ORDER_CANCEL_REJECT.value, target, sequence)
            | {
                37: order_id,
                11: request.get(Tag.CL_ORD_ID, "UNKNOWN"),
                41: request.get(Tag.ORIG_CL_ORD_ID, "UNKNOWN"),
                39: request.get(Tag.ORD_STATUS, OrdStatus.REJECTED.value),
                58: reason,
            }
        )

    def _send_session_reject(
        self,
        connection: socket.socket,
        peer: tuple[str, int],
        target: str,
        sequence: itertools.count,
        reason: str,
    ) -> None:
        self._send(
            connection,
            peer,
            FixMessage(self._header("3", target, sequence) | {58: reason}),
        )
