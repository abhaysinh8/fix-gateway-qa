from __future__ import annotations

from fixgateway.audit.audit_checker import (
    check_completeness,
    cross_check_against_state_machine,
    reconstruct_order_history,
)
from fixgateway.client.fix_client import FixClient
from fixgateway.exchange.mock_exchange import AuditLogEntry, MockExchange
from fixgateway.exchange.state_machine import OrderEvent, OrderState


def run_mixed_order_flow() -> tuple[
    list[AuditLogEntry], dict[str, object]
]:
    def fill_strategy(order):
        if order.order_id == "TRACE-FILL":
            return [25, 75]
        return []

    exchange = MockExchange(
        processing_delay_ms=(0.2, 0.5),
        fill_strategy=fill_strategy,
        random_seed=17,
    )
    with exchange:
        with FixClient(exchange.host, exchange.port, sender_comp_id="AUDIT_CLIENT") as client:
            client.send_new_order("TRACE-FILL", "AAPL", "1", 100, "200")
            client.receive_message()
            client.receive_message()
            cancel_reject = client.send_cancel(
                "CANCEL-TRACE-FILL", "TRACE-FILL", "AAPL", "1"
            )
            assert cancel_reject[35] == "9"

            client.send_new_order("TRACE-CANCEL", "MSFT", "2", 20, "400")
            client.send_cancel(
                "CANCEL-TRACE-CANCEL", "TRACE-CANCEL", "MSFT", "2"
            )

            client.send_new_order("TRACE-OPEN", "NVDA", "1", 5, "180")

    return exchange.audit_log, exchange.order_book.orders


def test_normal_run_is_complete_and_reconstructable() -> None:
    audit_log, orders = run_mixed_order_flow()

    result = check_completeness(audit_log, orders)

    assert result.is_complete, result.issues
    assert result.issues == []
    assert all(entry.direction in {"inbound", "outbound"} for entry in audit_log)
    assert any(entry.cum_qty == "25" for entry in audit_log)


def test_reconstructs_known_partial_fill_lifecycle() -> None:
    audit_log, _ = run_mixed_order_flow()

    history = reconstruct_order_history(audit_log, "TRACE-FILL")

    assert [transition.event for transition in history] == [
        OrderEvent.ACK,
        OrderEvent.PARTIAL_FILL,
        OrderEvent.FULL_FILL,
    ]
    assert [transition.to_state for transition in history] == [
        OrderState.NEW,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    ]


def test_audit_history_matches_every_live_state_machine() -> None:
    audit_log, orders = run_mixed_order_flow()

    assert all(
        cross_check_against_state_machine(
            audit_log, order_id, order.state_machine
        )
        for order_id, order in orders.items()
    )


def test_deleted_state_entry_is_reported_as_missing() -> None:
    audit_log, orders = run_mixed_order_flow()
    corrupted = list(audit_log)
    partial_fill_index = next(
        index for index, entry in enumerate(corrupted) if entry.exec_type == "1"
    )
    del corrupted[partial_fill_index]

    result = check_completeness(corrupted, orders)

    assert not result.is_complete
    assert any(
        "Missing audit entry" in issue and "PARTIAL_FILL" in issue
        for issue in result.missing_entries
    )


def test_shuffled_log_entries_report_non_monotonic_timestamp() -> None:
    audit_log, orders = run_mixed_order_flow()
    corrupted = list(audit_log)
    corrupted[0], corrupted[-1] = corrupted[-1], corrupted[0]

    result = check_completeness(corrupted, orders)

    assert not result.is_complete
    assert any(
        "Non-monotonic overall timestamp" in issue
        for issue in result.ordering_issues
    )
