"""Synchronous FIX test client with an asyncio concurrency convenience API."""

from __future__ import annotations

import asyncio
import itertools
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fixgateway.fix.constants import MsgType, OrdType, Side, Tag
from fixgateway.fix.framing import FixStreamReader
from fixgateway.fix.message import FixMessage, decode
from fixgateway.exchange.state_machine import (
    InvalidTransitionError,
    OrderEvent,
    OrderStateMachine,
)


def _fix_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


@dataclass(frozen=True)
class LatencySample:
    request_type: str
    cl_ord_id: str
    latency_ms: float


@dataclass(frozen=True)
class ConcurrentOrderResult:
    message: FixMessage
    latency_ms: float


@dataclass(frozen=True)
class DuplicateExecutionReport:
    exec_id: str
    cl_ord_id: str
    exec_type: str | None


class FixRequestTimeoutError(TimeoutError):
    """Raised when a FIX request does not receive a response before its deadline."""


class FixRetriesExhaustedError(FixRequestTimeoutError):
    """Raised after a request times out on its initial send and every retry."""

    def __init__(self, message: str, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


class FixClient:
    """A small persistent FIX client intended for tests and demonstrations."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        sender_comp_id: str = "TEST_CLIENT",
        target_comp_id: str = "MOCK_EXCHANGE",
        timeout: float = 5.0,
        max_retries: int = 0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.host = host
        self.port = port
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.latency_samples: list[LatencySample] = []
        self.detected_duplicates: list[DuplicateExecutionReport] = []
        self.processing_errors: list[str] = []
        self.retry_attempts: dict[str, int] = {}
        self._socket: socket.socket | None = None
        self._reader = FixStreamReader()
        self._sequence = itertools.count(1)
        self._processed_exec_ids: set[str] = set()
        self._order_states: dict[str, OrderStateMachine] = {}
        self._pending_messages: list[FixMessage] = []

    @property
    def is_connected(self) -> bool:
        return self._socket is not None

    @property
    def last_latency_ms(self) -> float | None:
        if not self.latency_samples:
            return None
        return self.latency_samples[-1].latency_ms

    @property
    def order_states(self) -> dict[str, OrderStateMachine]:
        """Return the client's per-order lifecycle trackers."""

        return dict(self._order_states)

    def connect(self) -> FixMessage:
        if self._socket is not None:
            raise RuntimeError("FIX client is already connected")
        connection = socket.create_connection((self.host, self.port), self.timeout)
        connection.settimeout(self.timeout)
        self._socket = connection
        logon = FixMessage(self._header("A") | {98: "0", 108: "30"})
        connection.sendall(logon.encode())
        response = self.receive_message()
        if response.get(Tag.MSG_TYPE) != "A":
            self.close()
            raise ConnectionError(
                f"Expected Logon response (35=A), received {response.get(Tag.MSG_TYPE)!r}"
            )
        return response

    def close(self) -> None:
        connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self._reader = FixStreamReader()
        self._pending_messages.clear()

    def __enter__(self) -> "FixClient":
        self.connect()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def receive_message(self) -> FixMessage:
        if self._pending_messages:
            return self._pending_messages.pop(0)
        return self._receive_unique_message()

    def _receive_unique_message(self) -> FixMessage:
        if self._socket is None:
            raise RuntimeError("FIX client is not connected")
        while True:
            try:
                message = decode(self._reader.receive(self._socket))
            except TimeoutError as exc:
                raise FixRequestTimeoutError(
                    f"Timed out after {self.timeout:.3f}s waiting for a FIX response"
                ) from exc
            if self._process_execution_report(message):
                continue
            return message

    def _process_execution_report(self, message: FixMessage) -> bool:
        """Apply one unique ExecutionReport; return True when it was a duplicate."""

        if message.get(Tag.MSG_TYPE) != MsgType.EXECUTION_REPORT.value:
            return False
        exec_id = message.get(Tag.EXEC_ID)
        cl_ord_id = message.get(Tag.CL_ORD_ID, "UNKNOWN")
        if exec_id is not None and exec_id in self._processed_exec_ids:
            self.detected_duplicates.append(
                DuplicateExecutionReport(
                    exec_id=exec_id,
                    cl_ord_id=cl_ord_id,
                    exec_type=message.get(Tag.EXEC_TYPE),
                )
            )
            return True
        if exec_id is not None:
            self._processed_exec_ids.add(exec_id)

        order_id = message.get(Tag.ORIG_CL_ORD_ID) or cl_ord_id
        event = {
            "0": OrderEvent.ACK,
            "1": OrderEvent.PARTIAL_FILL,
            "2": OrderEvent.FULL_FILL,
            "4": OrderEvent.CANCEL_CONFIRMED,
            "8": OrderEvent.REJECT,
        }.get(message.get(Tag.EXEC_TYPE))
        if event is None:
            return False
        machine = self._order_states.setdefault(order_id, OrderStateMachine())
        try:
            if event is OrderEvent.CANCEL_CONFIRMED:
                machine.apply(OrderEvent.CANCEL_REQUEST)
            machine.apply(event)
        except InvalidTransitionError as exc:
            self.processing_errors.append(
                f"ExecutionReport {exec_id or '<no ExecID>'} for {order_id}: {exc}"
            )
        return False

    def send_new_order(
        self,
        cl_ord_id: str,
        symbol: str,
        side: Side | str,
        quantity: int | float | str,
        price: int | float | str | None = None,
        *,
        time_in_force: str = "0",
    ) -> FixMessage:
        message = self._new_order_message(
            cl_ord_id,
            symbol,
            side,
            quantity,
            price,
            time_in_force=time_in_force,
        )
        return self.send_message(message, cl_ord_id=cl_ord_id)

    def send_new_order_nowait(
        self,
        cl_ord_id: str,
        symbol: str,
        side: Side | str,
        quantity: int | float | str,
        price: int | float | str | None = None,
        *,
        time_in_force: str = "0",
    ) -> FixMessage:
        """Submit an order without waiting, enabling controlled pipelined tests."""

        if self._socket is None:
            raise RuntimeError("FIX client is not connected")
        message = self._new_order_message(
            cl_ord_id,
            symbol,
            side,
            quantity,
            price,
            time_in_force=time_in_force,
        )
        self._socket.sendall(message.encode())
        return message

    def _new_order_message(
        self,
        cl_ord_id: str,
        symbol: str,
        side: Side | str,
        quantity: int | float | str,
        price: int | float | str | None,
        *,
        time_in_force: str,
    ) -> FixMessage:
        normalized_side = side.value if isinstance(side, Side) else str(side)
        ord_type = OrdType.MARKET.value if price is None else OrdType.LIMIT.value
        fields = self._header(MsgType.NEW_ORDER_SINGLE.value) | {
            11: cl_ord_id,
            55: symbol,
            54: normalized_side,
            38: str(quantity),
            40: ord_type,
            59: time_in_force,
        }
        if price is not None:
            fields[44] = str(price)
        return FixMessage(fields)

    def send_cancel(
        self,
        cl_ord_id: str,
        original_cl_ord_id: str,
        symbol: str,
        side: Side | str,
    ) -> FixMessage:
        normalized_side = side.value if isinstance(side, Side) else str(side)
        fields = self._header(MsgType.ORDER_CANCEL_REQUEST.value) | {
            11: cl_ord_id,
            41: original_cl_ord_id,
            55: symbol,
            54: normalized_side,
        }
        return self.send_message(FixMessage(fields), cl_ord_id=cl_ord_id)

    def send_message(self, message: FixMessage, *, cl_ord_id: str = "UNKNOWN") -> FixMessage:
        return self.send_raw(
            message.encode(),
            request_type=message.get(Tag.MSG_TYPE, "UNKNOWN"),
            cl_ord_id=cl_ord_id,
        )

    def send_raw(
        self,
        raw: bytes,
        *,
        request_type: str = "UNKNOWN",
        cl_ord_id: str = "UNKNOWN",
        max_retries: int | None = None,
    ) -> FixMessage:
        if self._socket is None:
            raise RuntimeError("FIX client is not connected")
        retry_limit = self.max_retries if max_retries is None else max_retries
        if retry_limit < 0:
            raise ValueError("max_retries must be non-negative")

        for attempt in range(retry_limit + 1):
            started_ns = time.perf_counter_ns()
            self._socket.sendall(raw)
            try:
                response = self._receive_matching(cl_ord_id)
            except FixRequestTimeoutError as exc:
                self.retry_attempts[cl_ord_id] = attempt
                if attempt < retry_limit:
                    continue
                attempts = attempt + 1
                raise FixRetriesExhaustedError(
                    f"Timed out after {attempts} attempt(s), {self.timeout:.3f}s each, "
                    f"waiting for response to MsgType {request_type} "
                    f"(ClOrdID {cl_ord_id}); retries exhausted",
                    attempts,
                ) from exc
            finished_ns = time.perf_counter_ns()
            self.retry_attempts[cl_ord_id] = attempt
            self.latency_samples.append(
                LatencySample(
                    request_type=request_type,
                    cl_ord_id=cl_ord_id,
                    latency_ms=(finished_ns - started_ns) / 1_000_000,
                )
            )
            return response
        raise AssertionError("retry loop terminated unexpectedly")

    def _receive_matching(self, cl_ord_id: str) -> FixMessage:
        for index, message in enumerate(self._pending_messages):
            if message.get(Tag.CL_ORD_ID) == cl_ord_id:
                return self._pending_messages.pop(index)
        while True:
            message = self._receive_unique_message()
            if (
                cl_ord_id == "UNKNOWN"
                or message.get(Tag.CL_ORD_ID) == cl_ord_id
                or message.get(Tag.MSG_TYPE) == "3"
            ):
                return message
            self._pending_messages.append(message)

    async def send_orders_concurrently(
        self,
        orders: list[dict[str, Any]],
        *,
        max_concurrency: int = 20,
    ) -> list[ConcurrentOrderResult]:
        """Send orders concurrently, one logged-on TCP session per in-flight order."""

        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be greater than zero")
        semaphore = asyncio.Semaphore(max_concurrency)

        async def send_one(index: int, order: dict[str, Any]) -> ConcurrentOrderResult:
            async with semaphore:
                def blocking_send() -> ConcurrentOrderResult:
                    with FixClient(
                        self.host,
                        self.port,
                        sender_comp_id=f"{self.sender_comp_id}_{index}",
                        target_comp_id=self.target_comp_id,
                        timeout=self.timeout,
                        max_retries=self.max_retries,
                    ) as worker:
                        message = worker.send_new_order(**order)
                        assert worker.last_latency_ms is not None
                        return ConcurrentOrderResult(message, worker.last_latency_ms)

                return await asyncio.to_thread(blocking_send)

        return await asyncio.gather(
            *(send_one(index, order) for index, order in enumerate(orders, start=1))
        )

    def _header(self, msg_type: str) -> dict[int, str]:
        return {
            8: "FIX.4.4",
            35: msg_type,
            49: self.sender_comp_id,
            56: self.target_comp_id,
            34: str(next(self._sequence)),
            52: _fix_timestamp(),
        }
