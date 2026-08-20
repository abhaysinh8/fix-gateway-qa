"""Stream framing helpers for the FIX subset used by the test infrastructure."""

from __future__ import annotations

import socket


class FixConnectionClosedError(ConnectionError):
    """Raised when a peer closes before another complete FIX message arrives."""


class FixStreamReader:
    """Extract complete FIX messages from a TCP byte stream.

    TCP does not preserve application-message boundaries. This reader retains extra
    bytes between calls and recognizes the final ``10=ddd<SOH>`` trailer. The project
    profile does not carry encoded data fields containing embedded SOH bytes.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def receive(self, connection: socket.socket) -> bytes:
        while True:
            start = self._buffer.find(b"8=")
            if start > 0:
                del self._buffer[:start]

            checksum_marker = self._buffer.find(b"\x0110=")
            if checksum_marker >= 0:
                trailer_end = self._buffer.find(b"\x01", checksum_marker + 1)
                if trailer_end >= 0:
                    message_end = trailer_end + 1
                    message = bytes(self._buffer[:message_end])
                    del self._buffer[:message_end]
                    return message

            chunk = connection.recv(4096)
            if not chunk:
                if self._buffer:
                    raise FixConnectionClosedError(
                        "Connection closed with a truncated FIX message buffered"
                    )
                raise FixConnectionClosedError("FIX peer closed the connection")
            self._buffer.extend(chunk)

