from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from fixgateway.exchange.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    InvalidTransitionError,
    OrderEvent,
    OrderState,
    OrderStateMachine,
)


@given(st.lists(st.sampled_from(list(OrderEvent)), min_size=0, max_size=100))
def test_random_event_sequences_follow_transition_table_exactly(
    events: list[OrderEvent],
) -> None:
    machine = OrderStateMachine()
    expected_state = OrderState.NEW
    successful_transitions = 0

    for event in events:
        transition = (expected_state, event)
        state_before = machine.state
        history_before = list(machine.history)

        if transition in TRANSITIONS:
            expected_state = TRANSITIONS[transition]
            assert machine.apply(event) is expected_state
            successful_transitions += 1
            assert machine.history[-1].from_state is state_before
            assert machine.history[-1].event is event
            assert machine.history[-1].to_state is expected_state
        else:
            with pytest.raises(InvalidTransitionError):
                machine.apply(event)
            assert machine.state is state_before
            assert machine.history == history_before

        assert machine.state is expected_state
        assert machine.state in OrderState
        assert len(machine.history) == successful_transitions


@given(
    terminal_state=st.sampled_from(list(TERMINAL_STATES)),
    event=st.sampled_from(list(OrderEvent)),
)
def test_terminal_states_have_no_outgoing_transitions(
    terminal_state: OrderState, event: OrderEvent
) -> None:
    machine = OrderStateMachine(terminal_state)
    with pytest.raises(InvalidTransitionError):
        machine.apply(event)
    assert machine.state is terminal_state
    assert machine.history == []


def test_multiple_partial_fills_then_full_fill() -> None:
    machine = OrderStateMachine()
    assert machine.apply(OrderEvent.PARTIAL_FILL) is OrderState.PARTIALLY_FILLED
    assert machine.apply(OrderEvent.PARTIAL_FILL) is OrderState.PARTIALLY_FILLED
    assert machine.apply(OrderEvent.FULL_FILL) is OrderState.FILLED
    assert len(machine.history) == 3


def test_new_order_can_be_canceled() -> None:
    machine = OrderStateMachine()
    assert machine.apply(OrderEvent.CANCEL_CONFIRMED) is OrderState.CANCELED


def test_canceling_filled_order_raises() -> None:
    machine = OrderStateMachine()
    machine.apply(OrderEvent.FULL_FILL)
    with pytest.raises(InvalidTransitionError, match="FILLED"):
        machine.apply(OrderEvent.CANCEL_REQUEST)


def test_filling_canceled_order_raises() -> None:
    machine = OrderStateMachine()
    machine.apply(OrderEvent.CANCEL_CONFIRMED)
    with pytest.raises(InvalidTransitionError, match="CANCELED"):
        machine.apply(OrderEvent.FULL_FILL)

