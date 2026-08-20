"""Small, dependency-free FIX 4.4 wire encoder and decoder."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .constants import SOH, Tag


class FixMessageError(ValueError):
    """Base error for malformed FIX wire data."""


class FixDecodeError(FixMessageError):
    """Raised when bytes cannot be framed or parsed as a FIX message."""


def _as_bytes(raw: bytes | str) -> bytes:
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        try:
            return raw.encode("ascii")
        except UnicodeEncodeError as exc:
            raise FixMessageError("FIX messages must contain ASCII data") from exc
    raise TypeError("FIX data must be bytes or str")


def calculate_checksum(raw_before_checksum: bytes | str) -> int:
    """Return the FIX checksum integer for bytes preceding tag 10.

    A complete message may also be supplied; in that case tag 10 and everything
    after its start are excluded automatically.
    """

    data = _as_bytes(raw_before_checksum)
    marker = data.find(b"\x0110=")
    if marker >= 0:
        data = data[: marker + 1]
    return sum(data) % 256


def calculate_body_length(raw_message: bytes | str) -> int:
    """Count bytes after tag 9's delimiter through the delimiter before tag 10."""

    data = _as_bytes(raw_message)
    body_length_start = data.find(b"\x019=")
    if body_length_start < 0:
        raise FixMessageError("Cannot calculate BodyLength: tag 9 is missing")
    body_start = data.find(b"\x01", body_length_start + 1)
    if body_start < 0:
        raise FixMessageError("Cannot calculate BodyLength: tag 9 is truncated")
    body_start += 1
    checksum_marker = data.rfind(b"\x0110=")
    if checksum_marker < body_start:
        raise FixMessageError("Cannot calculate BodyLength: final tag 10 is missing")
    checksum_start = checksum_marker + 1
    return checksum_start - body_start


@dataclass
class FixMessage:
    """A FIX message represented by integer tags and string values."""

    _fields: dict[int, str] = field(default_factory=dict)
    _raw: bytes | None = field(default=None, repr=False, compare=False)

    _HEADER_ORDER = (
        Tag.MSG_TYPE,
        Tag.SENDER_COMP_ID,
        Tag.TARGET_COMP_ID,
        Tag.MSG_SEQ_NUM,
        Tag.SENDING_TIME,
    )

    def __init__(self, fields: Mapping[int | str, Any], *, _raw: bytes | None = None):
        normalized: dict[int, str] = {}
        for tag, value in fields.items():
            try:
                numeric_tag = int(tag)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"FIX tag {tag!r} is not an integer") from exc
            if numeric_tag <= 0:
                raise ValueError(f"FIX tag {numeric_tag} must be positive")
            normalized[numeric_tag] = str(value)
        self._fields = normalized
        self._raw = _raw

    @property
    def fields(self) -> dict[int, str]:
        """Return a copy of the parsed tag map."""

        return dict(self._fields)

    @property
    def raw(self) -> bytes | None:
        """Original wire bytes when this message was produced by :func:`decode`."""

        return self._raw

    def __getitem__(self, tag: int) -> str:
        return self._fields[int(tag)]

    def get(self, tag: int, default: Any = None) -> str | Any:
        return self._fields.get(int(tag), default)

    def __contains__(self, tag: object) -> bool:
        try:
            return int(tag) in self._fields  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    def encode(self) -> bytes:
        """Encode with canonical header order and freshly computed tags 9 and 10."""

        begin_string = self._fields.get(Tag.BEGIN_STRING, "FIX.4.4")
        excluded = {int(Tag.BEGIN_STRING), int(Tag.BODY_LENGTH), int(Tag.CHECK_SUM)}

        ordered_tags: list[int] = []
        for tag in self._HEADER_ORDER:
            if tag in self._fields:
                ordered_tags.append(int(tag))
        ordered_tags.extend(
            tag
            for tag in self._fields
            if tag not in excluded and tag not in ordered_tags
        )

        try:
            body = b"".join(
                f"{tag}={self._fields[tag]}{SOH}".encode("ascii")
                for tag in ordered_tags
            )
            begin = f"8={begin_string}{SOH}".encode("ascii")
        except UnicodeEncodeError as exc:
            raise FixMessageError("FIX messages must contain ASCII data") from exc

        prefix = begin + f"9={len(body)}{SOH}".encode("ascii") + body
        checksum = calculate_checksum(prefix)
        return prefix + f"10={checksum:03d}{SOH}".encode("ascii")


def decode(raw_bytes_or_str: bytes | str) -> FixMessage:
    """Parse one complete SOH-delimited FIX message.

    Framing and syntax errors raise :class:`FixDecodeError`. Incorrect declared
    lengths/checksums are intentionally retained for the validator to report.
    """

    try:
        raw = _as_bytes(raw_bytes_or_str)
    except (FixMessageError, TypeError) as exc:
        raise FixDecodeError(str(exc)) from exc

    if not raw:
        raise FixDecodeError("Cannot decode an empty FIX message")
    if not raw.endswith(b"\x01"):
        raise FixDecodeError("Truncated FIX message: expected a trailing SOH delimiter")

    chunks = raw[:-1].split(b"\x01")
    if len(chunks) < 3:
        raise FixDecodeError("Malformed FIX message: expected header, body, and checksum")

    fields: dict[int, str] = {}
    for index, chunk in enumerate(chunks, start=1):
        if not chunk:
            raise FixDecodeError(f"Malformed FIX message: empty field at position {index}")
        if b"=" not in chunk:
            raise FixDecodeError(
                f"Malformed FIX field at position {index}: missing '=' separator"
            )
        raw_tag, raw_value = chunk.split(b"=", 1)
        try:
            tag_text = raw_tag.decode("ascii")
            value = raw_value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise FixDecodeError(f"Non-ASCII FIX field at position {index}") from exc
        if not tag_text.isdigit() or int(tag_text) <= 0:
            raise FixDecodeError(f"Invalid FIX tag {tag_text!r} at position {index}")
        tag = int(tag_text)
        if tag in fields:
            raise FixDecodeError(f"Duplicate FIX tag {tag} is not supported")
        fields[tag] = value

    if chunks[0].split(b"=", 1)[0] != b"8":
        raise FixDecodeError("Malformed FIX message: tag 8 must be first")
    if len(chunks) < 2 or chunks[1].split(b"=", 1)[0] != b"9":
        raise FixDecodeError("Malformed FIX message: tag 9 must be second")
    if chunks[-1].split(b"=", 1)[0] != b"10":
        raise FixDecodeError("Truncated FIX message: tag 10 must be the final field")

    return FixMessage(fields, _raw=raw)

