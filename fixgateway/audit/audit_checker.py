"""Simplified educational audit-trail completeness checks.

This module is a QA portfolio analog of audit and regulatory-reporting completeness
controls, including ideas relevant to MiFID II transaction-reporting workflows. It is
not a real regulatory reporting, record-keeping, or compliance system.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fixgateway.exchange.state_machine import (
    TERMINAL_STATES,
    InvalidTransitionError,
    OrderEvent,
    OrderState,
    OrderStateMachine,
    Transition,
)


class AuditReconstructionError(ValueError):
    """Raised when logged events cannot form a valid order lifecycle."""


@dataclass
class AuditResult:
    is_complete: bool
    missing_entries: list[str] = field(default_factory=list)
    ordering_issues: list[str] = field(default_factory=list)
    lifecycle_issues: list[str] = field(default_factory=list)

    @property
    def issues(self) -> list[str]:
        return [*self.missing_entries, *self.ordering_issues, *self.lifecycle_issues]


@dataclass(frozen=True)
class _AuditRecord:
    timestamp: float
    direction: str
    delivery_status: str
    msg_type: str | None
    order_id: str | None
    exec_type: str | None
    exec_id: str | None
    ord_status: str | None


@dataclass(frozen=True)
class _ExpectedHistory:
    events: list[OrderEvent]
    final_state: OrderState


def _timestamp_value(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError as exc:
                raise ValueError(f"Invalid audit timestamp: {value!r}") from exc
    raise TypeError(f"Unsupported audit timestamp: {value!r}")


def _field(fields: Mapping[Any, Any], tag: int) -> str | None:
    value = fields.get(tag, fields.get(str(tag)))
    return None if value is None else str(value)


def _normalize_record(entry: Any) -> _AuditRecord:
    if isinstance(entry, Mapping):
        data = dict(entry)
    elif hasattr(entry, "timestamp") and hasattr(entry, "direction"):
        data = {
            "timestamp": entry.timestamp,
            "direction": entry.direction,
            "delivery_status": getattr(entry, "delivery_status", entry.direction),
            "msg_type": getattr(entry, "msg_type", None),
            "order_id": getattr(entry, "order_id", None),
            "exec_type": getattr(entry, "exec_type", None),
            "exec_id": getattr(entry, "exec_id", None),
            "ord_status": getattr(entry, "ord_status", None),
            "fields": getattr(entry, "fields", {}),
        }
        if data["msg_type"] is None and hasattr(entry, "message"):
            data["fields"] = entry.message.fields if entry.message is not None else {}
    else:
        raise TypeError("Audit entries must be mappings or structured log entries")

    if "timestamp" not in data:
        raise ValueError("Audit entry is missing timestamp")
    fields = data.get("fields") or {}
    msg_type = data.get("msg_type") or _field(fields, 35)
    cl_ord_id = data.get("cl_ord_id") or _field(fields, 11)
    orig_cl_ord_id = data.get("orig_cl_ord_id") or _field(fields, 41)
    order_id = data.get("order_id") or orig_cl_ord_id or cl_ord_id
    raw_direction = str(data.get("direction", "")).lower()
    direction = {
        "received": "inbound",
        "sent": "outbound",
        "dropped": "outbound",
    }.get(raw_direction, raw_direction)
    if direction not in {"inbound", "outbound"}:
        raise ValueError(f"Invalid audit direction: {data.get('direction')!r}")

    return _AuditRecord(
        timestamp=_timestamp_value(data["timestamp"]),
        direction=direction,
        delivery_status=str(data.get("delivery_status", "unknown")).lower(),
        msg_type=None if msg_type is None else str(msg_type),
        order_id=None if order_id is None else str(order_id),
        exec_type=(
            str(data.get("exec_type") or _field(fields, 150))
            if data.get("exec_type") is not None or _field(fields, 150) is not None
            else None
        ),
        exec_id=(
            str(data.get("exec_id") or _field(fields, 17))
            if data.get("exec_id") is not None or _field(fields, 17) is not None
            else None
        ),
        ord_status=(
            str(data.get("ord_status") or _field(fields, 39))
            if data.get("ord_status") is not None or _field(fields, 39) is not None
            else None
        ),
    )


def _normalize_log(audit_log: Iterable[Any]) -> list[_AuditRecord]:
    return [_normalize_record(entry) for entry in audit_log]


def _execution_event(record: _AuditRecord) -> OrderEvent | None:
    if record.direction != "outbound" or record.msg_type != "8":
        return None
    value = record.exec_type
    if value == "0" or (value is None and record.ord_status == "0"):
        return OrderEvent.ACK
    if value == "1" or (value is None and record.ord_status == "1"):
        return OrderEvent.PARTIAL_FILL
    if value == "2" or (value is None and record.ord_status == "2"):
        return OrderEvent.FULL_FILL
    if value == "4" or (value is None and record.ord_status == "4"):
        return OrderEvent.CANCEL_CONFIRMED
    if value == "8" or (value is None and record.ord_status == "8"):
        return OrderEvent.REJECT
    return None


def _reconstruct(records: list[_AuditRecord], order_id: str) -> list[Transition]:
    relevant = sorted(
        (
            (index, record)
            for index, record in enumerate(records)
            if record.order_id == order_id
        ),
        key=lambda item: (item[1].timestamp, item[0]),
    )
    machine = OrderStateMachine()
    pending_cancel_requests = 0
    seen_exec_ids: set[str] = set()
    try:
        for _, record in relevant:
            if record.direction == "inbound" and record.msg_type == "F":
                pending_cancel_requests += 1
                continue
            if record.direction == "outbound" and record.msg_type == "9":
                pending_cancel_requests = max(0, pending_cancel_requests - 1)
                continue

            if record.exec_id is not None:
                if record.exec_id in seen_exec_ids:
                    continue
                seen_exec_ids.add(record.exec_id)

            event = _execution_event(record)
            if event is OrderEvent.CANCEL_CONFIRMED and pending_cancel_requests:
                machine.apply(OrderEvent.CANCEL_REQUEST)
                pending_cancel_requests -= 1
            if event is not None:
                machine.apply(event)
    except InvalidTransitionError as exc:
        raise AuditReconstructionError(
            f"Order {order_id} has an invalid logged lifecycle: {exc}"
        ) from exc
    return list(machine.history)


def reconstruct_order_history(audit_log: Iterable[Any], order_id: str) -> list[Transition]:
    """Reconstruct one order solely from chronological audit records."""

    return _reconstruct(_normalize_log(audit_log), str(order_id))


def cross_check_against_state_machine(
    audit_log: Iterable[Any],
    order_id: str,
    actual_state_machine: OrderStateMachine,
) -> bool:
    """Return whether audit-only reconstruction exactly matches the live machine."""

    if not isinstance(actual_state_machine, OrderStateMachine):
        raise TypeError("actual_state_machine must be an OrderStateMachine")
    try:
        reconstructed = reconstruct_order_history(audit_log, order_id)
    except AuditReconstructionError:
        return False
    final_state = reconstructed[-1].to_state if reconstructed else OrderState.NEW
    return final_state is actual_state_machine.state and reconstructed == actual_state_machine.history


def _event_value(value: Any) -> OrderEvent:
    if isinstance(value, Transition):
        return value.event
    if isinstance(value, OrderEvent):
        return value
    if isinstance(value, Mapping):
        value = value.get("event", value.get("action"))
    return OrderEvent(value)


def _expected_histories(expected_order_events: Any) -> dict[str, _ExpectedHistory]:
    if expected_order_events is None:
        return {}
    if isinstance(expected_order_events, Mapping):
        items = expected_order_events.items()
    else:
        grouped: dict[str, list[Any]] = defaultdict(list)
        for item in expected_order_events:
            if isinstance(item, Mapping):
                grouped[str(item["order_id"])].append(item)
            else:
                grouped[str(item.order_id)].append(item)
        items = grouped.items()

    expected: dict[str, _ExpectedHistory] = {}
    for order_id, value in items:
        machine = value if isinstance(value, OrderStateMachine) else None
        if machine is None and hasattr(value, "state_machine"):
            machine = value.state_machine
        if machine is not None:
            expected[str(order_id)] = _ExpectedHistory(
                [transition.event for transition in machine.history], machine.state
            )
            continue

        events = [_event_value(item) for item in value]
        simulated = OrderStateMachine()
        for event in events:
            simulated.apply(event)
        expected[str(order_id)] = _ExpectedHistory(events, simulated.state)
    return expected


def check_completeness(
    audit_log: Iterable[Any], expected_order_events: Any
) -> AuditResult:
    """Check ordering, state-event coverage, and terminal/open traceability."""

    records = _normalize_log(audit_log)
    expected = _expected_histories(expected_order_events)
    missing_entries: list[str] = []
    ordering_issues: list[str] = []
    lifecycle_issues: list[str] = []

    for index in range(1, len(records)):
        if records[index].timestamp < records[index - 1].timestamp:
            ordering_issues.append(
                f"Non-monotonic overall timestamp at audit entry {index}: "
                f"{records[index].timestamp} < {records[index - 1].timestamp}"
            )

    per_order_timestamp: dict[str, float] = {}
    for index, record in enumerate(records):
        if record.order_id is None:
            continue
        previous = per_order_timestamp.get(record.order_id)
        if previous is not None and record.timestamp <= previous:
            ordering_issues.append(
                f"Non-increasing timestamp for order {record.order_id} at audit "
                f"entry {index}: {record.timestamp} <= {previous}"
            )
        per_order_timestamp[record.order_id] = record.timestamp

    reconstructed_by_order: dict[str, list[Transition]] = {}
    for order_id, expected_history in expected.items():
        try:
            reconstructed = _reconstruct(records, order_id)
        except AuditReconstructionError as exc:
            lifecycle_issues.append(str(exc))
            reconstructed = []
        reconstructed_by_order[order_id] = reconstructed
        actual_events = [transition.event for transition in reconstructed]

        expected_counts = Counter(expected_history.events)
        actual_counts = Counter(actual_events)
        for event, expected_count in expected_counts.items():
            actual_count = actual_counts[event]
            if actual_count < expected_count:
                missing_entries.append(
                    f"Missing audit entry for order {order_id}: {event.value} "
                    f"(expected {expected_count}, found {actual_count})"
                )
        if actual_events != expected_history.events and not any(
            order_id in issue for issue in missing_entries
        ):
            lifecycle_issues.append(
                f"Audit transition order mismatch for order {order_id}: "
                f"expected {[event.value for event in expected_history.events]}, "
                f"found {[event.value for event in actual_events]}"
            )

        final_state = reconstructed[-1].to_state if reconstructed else OrderState.NEW
        if final_state is not expected_history.final_state:
            lifecycle_issues.append(
                f"Final state mismatch for order {order_id}: expected "
                f"{expected_history.final_state.value}, reconstructed {final_state.value}"
            )

    started_order_ids = {
        record.order_id
        for record in records
        if record.direction == "inbound" and record.msg_type == "D" and record.order_id
    }
    for order_id in sorted(started_order_ids):
        try:
            history = reconstructed_by_order.get(order_id) or _reconstruct(records, order_id)
        except AuditReconstructionError as exc:
            if str(exc) not in lifecycle_issues:
                lifecycle_issues.append(str(exc))
            continue
        if not history:
            missing_entries.append(
                f"Order {order_id} has a NewOrderSingle but no auditable state entry"
            )
            continue
        final_state = history[-1].to_state
        explicitly_open = (
            order_id in expected
            and expected[order_id].final_state not in TERMINAL_STATES
            and expected[order_id].final_state is final_state
        )
        if final_state not in TERMINAL_STATES and not explicitly_open:
            lifecycle_issues.append(
                f"Order {order_id} has no terminal state and is not explicitly still open"
            )

    result = AuditResult(
        is_complete=not (missing_entries or ordering_issues or lifecycle_issues),
        missing_entries=missing_entries,
        ordering_issues=ordering_issues,
        lifecycle_issues=lifecycle_issues,
    )
    return result
