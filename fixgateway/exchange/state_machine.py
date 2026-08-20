"""Deterministic lifecycle state machine for an exchange order."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OrderState(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class OrderEvent(str, Enum):
    ACK = "ACK"
    PARTIAL_FILL = "PARTIAL_FILL"
    FULL_FILL = "FULL_FILL"
    CANCEL_REQUEST = "CANCEL_REQUEST"
    CANCEL_CONFIRMED = "CANCEL_CONFIRMED"
    REJECT = "REJECT"


TRANSITIONS: dict[tuple[OrderState, OrderEvent], OrderState] = {
    (OrderState.NEW, OrderEvent.ACK): OrderState.NEW,
    (OrderState.NEW, OrderEvent.PARTIAL_FILL): OrderState.PARTIALLY_FILLED,
    (OrderState.NEW, OrderEvent.FULL_FILL): OrderState.FILLED,
    (OrderState.NEW, OrderEvent.CANCEL_REQUEST): OrderState.NEW,
    (OrderState.NEW, OrderEvent.CANCEL_CONFIRMED): OrderState.CANCELED,
    (OrderState.NEW, OrderEvent.REJECT): OrderState.REJECTED,
    (
        OrderState.PARTIALLY_FILLED,
        OrderEvent.PARTIAL_FILL,
    ): OrderState.PARTIALLY_FILLED,
    (OrderState.PARTIALLY_FILLED, OrderEvent.FULL_FILL): OrderState.FILLED,
    (
        OrderState.PARTIALLY_FILLED,
        OrderEvent.CANCEL_REQUEST,
    ): OrderState.PARTIALLY_FILLED,
    (
        OrderState.PARTIALLY_FILLED,
        OrderEvent.CANCEL_CONFIRMED,
    ): OrderState.CANCELED,
}


TERMINAL_STATES = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED}
)


class InvalidTransitionError(ValueError):
    """Raised when an event is not permitted from an order's current state."""


@dataclass(frozen=True)
class Transition:
    """One successfully applied state transition."""

    from_state: OrderState
    event: OrderEvent
    to_state: OrderState


@dataclass
class OrderStateMachine:
    """Track an order state and the complete history of successful transitions."""

    state: OrderState = OrderState.NEW
    history: list[Transition] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.state, OrderState):
            try:
                self.state = OrderState(self.state)
            except ValueError as exc:
                raise ValueError(f"Unknown initial order state: {self.state!r}") from exc

    def apply(self, event: OrderEvent) -> OrderState:
        """Apply an event, returning the resulting state.

        Invalid attempts are side-effect free: neither state nor history changes.
        """

        if not isinstance(event, OrderEvent):
            try:
                event = OrderEvent(event)
            except ValueError as exc:
                raise InvalidTransitionError(
                    f"Unknown order event {event!r} from state {self.state.value}"
                ) from exc

        previous_state = self.state
        try:
            new_state = TRANSITIONS[(previous_state, event)]
        except KeyError as exc:
            raise InvalidTransitionError(
                f"Invalid order transition: event {event.value} cannot be applied "
                f"from state {previous_state.value}"
            ) from exc

        self.state = new_state
        self.history.append(Transition(previous_state, event, new_state))
        return new_state

