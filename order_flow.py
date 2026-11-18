"""Campus checkout workflow simulation.

Implements an in-memory, per-order, single-process state model with separate
order and payment states stored as immutable snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class OrderState(str, Enum):
    """Lifecycle states for a campus order."""
    PENDING_PAYMENT = "PENDING_PAYMENT"
    AWAITING_VENDOR = "AWAITING_VENDOR"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class PaymentState(str, Enum):
    """Lifecycle states for an order's payment."""
    UNPAID = "UNPAID"
    PAID = "PAID"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"


class EventType(str, Enum):
    """Supported domain events driving the workflow."""
    PAYMENT_SUCCESS = "PAYMENT_SUCCESS"
    VENDOR_ACCEPT = "VENDOR_ACCEPT"
    VENDOR_REJECT = "VENDOR_REJECT"
    PAYMENT_REFUND_SUCCESS = "PAYMENT_REFUND_SUCCESS"
    CANCEL_BEFORE_PAYMENT = "CANCEL_BEFORE_PAYMENT"


class OrderFlowError(Exception):
    """Base exception for all order flow errors."""


class InvalidTransitionError(OrderFlowError):
    """Raised when an event is invalid for current order and payment states.

    Invalid transitions preserve current states and do not consume the event ID.
    """


class EventConflictError(OrderFlowError):
    """Raised when an event ID is reused with a different event type or payload."""


@dataclass(frozen=True)
class Event:
    """Domain event with a stable ID, type, and payload."""
    event_id: str
    event_type: EventType
    payload: tuple[tuple[str, str | int], ...]

    def __init__(
        self,
        event_id: str,
        event_type: EventType,
        payload: Mapping[str, str | int] | None = None,
    ) -> None:
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("event_id must be a non-empty string")
        event_type = EventType(event_type)
        items = tuple(payload.items()) if payload is not None else ()
        for key, value in items:
            if type(key) is not str or type(value) not in (str, int):
                raise TypeError("payload keys must be strings and values must be strings or integers")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "payload", tuple(sorted(items)))

    @property
    def payload_dict(self) -> dict[str, str | int]:
        return dict(self.payload)

    def same_meaning_as(self, other: Event) -> bool:
        return self.event_type == other.event_type and self.payload == other.payload


@dataclass(frozen=True)
class OrderSnapshot:
    """Immutable point-in-time snapshot of an order."""
    order_id: str
    order_state: OrderState
    payment_state: PaymentState
    version: int
    last_event_id: str | None = None
    note: str = ""

    def summary(self) -> str:
        return (
            f"Order {self.order_id} [v{self.version}] | "
            f"order={self.order_state.value:<15} | "
            f"payment={self.payment_state.value:<15} | "
            f"{self.note}"
        )


class Order:
    """In-memory, per-order aggregate managing state transitions via immutable snapshots."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        self._current_snapshot = OrderSnapshot(
            order_id=order_id,
            order_state=OrderState.PENDING_PAYMENT,
            payment_state=PaymentState.UNPAID,
            version=0,
            last_event_id=None,
            note="Order created, awaiting payment",
        )
        self._consumed_events: dict[str, Event] = {}
        self._history: list[OrderSnapshot] = [self._current_snapshot]

    @property
    def current_snapshot(self) -> OrderSnapshot:
        return self._current_snapshot

    @property
    def order_state(self) -> OrderState:
        return self._current_snapshot.order_state

    @property
    def payment_state(self) -> PaymentState:
        return self._current_snapshot.payment_state

    @property
    def version(self) -> int:
        return self._current_snapshot.version

    @property
    def history(self) -> list[OrderSnapshot]:
        return list(self._history)

    @property
    def consumed_events(self) -> dict[str, Event]:
        return dict(self._consumed_events)

    def apply(self, event: Event) -> OrderSnapshot:
        """Apply an event to the order, advancing state if valid.

        - If event_id was already processed with identical type and payload,
          the call is an exact replay: returns current snapshot unchanged.
        - If event_id was already processed with different type or payload,
          raises EventConflictError.
        - If the transition is invalid for current states, raises
          InvalidTransitionError: states are preserved and event_id is not consumed.
        """
        # 1. Idempotency & Conflict checks
        if event.event_id in self._consumed_events:
            existing = self._consumed_events[event.event_id]
            if existing.same_meaning_as(event):
                return self._current_snapshot
            raise EventConflictError(
                f"Conflict on event ID '{event.event_id}': already processed as "
                f"type={existing.event_type.value} with payload={existing.payload_dict}, "
                f"conflicts with new type={event.event_type.value} with payload={event.payload_dict}."
            )

        # 2. State transition validation
        cur_order = self._current_snapshot.order_state
        cur_payment = self._current_snapshot.payment_state

        if event.event_type == EventType.CANCEL_BEFORE_PAYMENT:
            if cur_order == OrderState.PENDING_PAYMENT and cur_payment == PaymentState.UNPAID:
                next_order = OrderState.CANCELLED
                next_payment = PaymentState.CANCELLED
                reason = event.payload_dict.get("reason", "Cancelled by customer before payment")
                note = f"Order cancelled before payment ({reason})"
            else:
                raise InvalidTransitionError(
                    f"Cannot cancel order {self.order_id} in state ({cur_order.value}, {cur_payment.value}); "
                    f"cancellation is only permitted before payment."
                )

        elif event.event_type == EventType.PAYMENT_SUCCESS:
            if cur_order == OrderState.PENDING_PAYMENT and cur_payment == PaymentState.UNPAID:
                next_order = OrderState.AWAITING_VENDOR
                next_payment = PaymentState.PAID
                amount = event.payload_dict.get("amount_cents", "unknown")
                note = f"Payment captured ({amount} cents), awaiting vendor confirmation"
            else:
                raise InvalidTransitionError(
                    f"Payment success cannot be applied to order {self.order_id} in state "
                    f"({cur_order.value}, {cur_payment.value})."
                )

        elif event.event_type == EventType.VENDOR_ACCEPT:
            if cur_order == OrderState.AWAITING_VENDOR and cur_payment == PaymentState.PAID:
                next_order = OrderState.ACCEPTED
                next_payment = PaymentState.PAID
                prep_time = event.payload_dict.get("estimated_prep_min", 15)
                note = f"Vendor accepted order (est. prep: {prep_time} min)"
            else:
                raise InvalidTransitionError(
                    f"Vendor acceptance for order {self.order_id} is only allowed while "
                    f"awaiting vendor after payment. Current states: ({cur_order.value}, {cur_payment.value})."
                )

        elif event.event_type == EventType.VENDOR_REJECT:
            if cur_order == OrderState.AWAITING_VENDOR and cur_payment == PaymentState.PAID:
                next_order = OrderState.REJECTED
                next_payment = PaymentState.REFUND_PENDING
                reason = event.payload_dict.get("reason", "Vendor unavailable")
                note = f"Vendor rejected order ({reason}); simulated refund initiated"
            else:
                raise InvalidTransitionError(
                    f"Vendor rejection for order {self.order_id} is only allowed while "
                    f"awaiting vendor after payment. Current states: ({cur_order.value}, {cur_payment.value})."
                )

        elif event.event_type == EventType.PAYMENT_REFUND_SUCCESS:
            if cur_payment == PaymentState.REFUND_PENDING and cur_order == OrderState.REJECTED:
                next_order = OrderState.REJECTED
                next_payment = PaymentState.REFUNDED
                note = "Refund confirmed by payment simulator"
            else:
                raise InvalidTransitionError(
                    f"Refund confirmation only completes a pending refund. "
                    f"Current states: ({cur_order.value}, {cur_payment.value})."
                )

        else:
            raise InvalidTransitionError(f"Unsupported event type: {event.event_type}")

        # 3. Transition committed: emit next immutable snapshot and consume event ID
        new_snapshot = OrderSnapshot(
            order_id=self.order_id,
            order_state=next_order,
            payment_state=next_payment,
            version=self._current_snapshot.version + 1,
            last_event_id=event.event_id,
            note=note,
        )
        self._consumed_events[event.event_id] = event
        self._current_snapshot = new_snapshot
        self._history.append(new_snapshot)
        return new_snapshot

    # Ergonomic convenience methods
    def cancel_before_payment(self, event_id: str, reason: str = "") -> OrderSnapshot:
        payload = {"reason": reason} if reason else {}
        return self.apply(Event(event_id, EventType.CANCEL_BEFORE_PAYMENT, payload))

    def record_payment_success(self, event_id: str, amount_cents: int = 0) -> OrderSnapshot:
        payload = {"amount_cents": amount_cents} if amount_cents else {}
        return self.apply(Event(event_id, EventType.PAYMENT_SUCCESS, payload))

    def vendor_accept(self, event_id: str, estimated_prep_min: int = 15) -> OrderSnapshot:
        return self.apply(Event(event_id, EventType.VENDOR_ACCEPT, {"estimated_prep_min": estimated_prep_min}))

    def vendor_reject(self, event_id: str, reason: str = "") -> OrderSnapshot:
        payload = {"reason": reason} if reason else {}
        return self.apply(Event(event_id, EventType.VENDOR_REJECT, payload))

    def record_refund_success(self, event_id: str) -> OrderSnapshot:
        return self.apply(Event(event_id, EventType.PAYMENT_REFUND_SUCCESS))


def run_demo() -> None:
    """Demonstrate acceptance and rejection/refund timelines."""
    print("=" * 78)
    print("Campus Food Ordering Checkout Workflow Simulation")
    print("Architecture: In-memory, per-order, single-process state snapshot model")
    print("=" * 78)

    # ---------------------------------------------------------
    # Timeline 1: Successful Acceptance Flow
    # ---------------------------------------------------------
    print("\n[Timeline 1] Happy Path: Customer Pays -> Vendor Accepts")
    print("-" * 78)
    order1 = Order("CAMPUS-ORD-101")
    print(f"  T0: {order1.current_snapshot.summary()}")

    snap_pay1 = order1.record_payment_success("evt-pay-101", amount_cents=1250)
    print(f"  T1: {snap_pay1.summary()}")

    snap_acc1 = order1.vendor_accept("evt-acc-101", estimated_prep_min=20)
    print(f"  T2: {snap_acc1.summary()}")

    # Exact replay demonstration
    snap_replay = order1.vendor_accept("evt-acc-101", estimated_prep_min=20)
    print(f"  T3: [Exact Replay of evt-acc-101] -> Unchanged: v{snap_replay.version}, "
          f"order={snap_replay.order_state.value}, payment={snap_replay.payment_state.value}")

    # ---------------------------------------------------------
    # Timeline 2: Vendor Rejection, Simulated Refund & Out-of-Order Retry
    # ---------------------------------------------------------
    print("\n[Timeline 2] Exception Path: Out-of-Order Reject -> Payment -> Reject Retry -> Refund")
    print("-" * 78)
    order2 = Order("CAMPUS-ORD-102")
    print(f"  T0: {order2.current_snapshot.summary()}")

    # Out-of-order rejection: vendor tries to reject before payment succeeds
    reject_event = Event("evt-rej-102", EventType.VENDOR_REJECT, {"reason": "Stall closed for restock"})
    print(f"  T1: [Attempt out-of-order reject evt-rej-102 before payment]")
    try:
        order2.apply(reject_event)
    except InvalidTransitionError as err:
        print(f"      -> Rejected as expected: {err}")
        print(f"      -> Snapshot preserved: v{order2.version} "
              f"order={order2.order_state.value}, payment={order2.payment_state.value} (ID not consumed)")

    # Customer completes payment
    snap_pay2 = order2.record_payment_success("evt-pay-102", amount_cents=1800)
    print(f"  T2: {snap_pay2.summary()}")

    # Retry the exact same rejection event that was previously rejected
    snap_rej2 = order2.apply(reject_event)
    print(f"  T3: [Retry evt-rej-102 after payment] -> {snap_rej2.summary()}")

    # Refund confirmation arrives from simulated gateway
    snap_ref2 = order2.record_refund_success("evt-ref-102")
    print(f"  T4: {snap_ref2.summary()}")

    # Exact replay of refund confirmation
    snap_ref_replay = order2.record_refund_success("evt-ref-102")
    print(f"  T5: [Exact Replay of evt-ref-102] -> Unchanged: v{snap_ref_replay.version}, "
          f"order={snap_ref_replay.order_state.value}, payment={snap_ref_replay.payment_state.value}")

    # Conflict detection demonstration
    print(f"  T6: [Conflict Demo: Reuse evt-ref-102 with different payload]")
    try:
        order2.apply(Event("evt-ref-102", EventType.PAYMENT_REFUND_SUCCESS, {"amount_cents": 9999}))
    except EventConflictError as err:
        print(f"      -> Caught EventConflictError as expected: {err}")

    # ---------------------------------------------------------
    # Timeline 3: Cancellation Before Payment
    # ---------------------------------------------------------
    print("\n[Timeline 3] Pre-Payment Cancellation Flow")
    print("-" * 78)
    order3 = Order("CAMPUS-ORD-103")
    print(f"  T0: {order3.current_snapshot.summary()}")
    snap_cancel = order3.cancel_before_payment("evt-can-103", reason="Ordered wrong items")
    print(f"  T1: {snap_cancel.summary()}")

    print("\n" + "=" * 78)
    print("Simulation completed successfully.")
    print("=" * 78)


if __name__ == "__main__":
    run_demo()
