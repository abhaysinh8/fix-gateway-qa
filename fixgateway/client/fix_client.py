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


class FixRequestTimeoutError(TimeoutError):
    """Raised when a FIX request does not receive a response before its deadline."""


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
    ) -> None:
        self.host = host
        self.port = port
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        self.timeout = timeout
        self.latency_samples: list[LatencySample] = []
        self._socket: socket.socket | None = None
        self._reader = FixStreamReader()
        self._sequence = itertools.count(1)

    @property
    def is_connected(self) -> bool:
        return self._socket is not None

    @property
    def last_latency_ms(self) -> float | None:
        if not self.latency_samples:
            return None
        return self.latency_samples[-1].latency_ms

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

    def __enter__(self) -> "FixClient":
        self.connect()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def receive_message(self) -> FixMessage:
        if self._socket is None:
            raise RuntimeError("FIX client is not connected")
        try:
            return decode(self._reader.receive(self._socket))
        except TimeoutError as exc:
            raise FixRequestTimeoutError(
                f"Timed out after {self.timeout:.3f}s waiting for a FIX response"
            ) from exc

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
        return self.send_message(FixMessage(fields), cl_ord_id=cl_ord_id)

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
    ) -> FixMessage:
        if self._socket is None:
            raise RuntimeError("FIX client is not connected")
        started_ns = time.perf_counter_ns()
        self._socket.sendall(raw)
        try:
            response = self.receive_message()
        except FixRequestTimeoutError as exc:
            raise FixRequestTimeoutError(
                f"Timed out after {self.timeout:.3f}s waiting for response to "
                f"MsgType {request_type} (ClOrdID {cl_ord_id})"
            ) from exc
        finished_ns = time.perf_counter_ns()
        self.latency_samples.append(
            LatencySample(
                request_type=request_type,
                cl_ord_id=cl_ord_id,
                latency_ms=(finished_ns - started_ns) / 1_000_000,
            )
        )
        return response

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
