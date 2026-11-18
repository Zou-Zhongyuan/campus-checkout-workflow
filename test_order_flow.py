"""Unit tests for the campus food checkout order flow."""

import unittest
from dataclasses import FrozenInstanceError

from order_flow import (
    Event,
    EventConflictError,
    EventType,
    InvalidTransitionError,
    Order,
    OrderSnapshot,
    OrderState,
    PaymentState,
)


class TestOrderFlow(unittest.TestCase):
    """Test suite covering order flow state transitions, idempotency, and conflicts."""

    def test_event_payload_stays_stable_after_construction(self) -> None:
        payload = {"amount_cents": 1200}
        event = Event("evt-pay", EventType.PAYMENT_SUCCESS, payload)
        payload["amount_cents"] = 400
        self.assertEqual(event.payload_dict, {"amount_cents": 1200})
        with self.assertRaises(TypeError):
            Event("evt-invalid", EventType.PAYMENT_SUCCESS, {"amount_cents": [1200]})

    def test_happy_path_acceptance(self) -> None:
        """Verify normal customer payment followed by vendor acceptance."""
        order = Order("ORD-TEST-001")
        self.assertEqual(order.order_state, OrderState.PENDING_PAYMENT)
        self.assertEqual(order.payment_state, PaymentState.UNPAID)
        self.assertEqual(order.version, 0)

        snap_pay = order.record_payment_success("evt-pay-1", amount_cents=1500)
        self.assertEqual(snap_pay.order_state, OrderState.AWAITING_VENDOR)
        self.assertEqual(snap_pay.payment_state, PaymentState.PAID)
        self.assertEqual(snap_pay.version, 1)
        self.assertEqual(snap_pay.last_event_id, "evt-pay-1")

        snap_accept = order.vendor_accept("evt-acc-1", estimated_prep_min=20)
        self.assertEqual(snap_accept.order_state, OrderState.ACCEPTED)
        self.assertEqual(snap_accept.payment_state, PaymentState.PAID)
        self.assertEqual(snap_accept.version, 2)
        self.assertEqual(snap_accept.last_event_id, "evt-acc-1")
        self.assertEqual(len(order.history), 3)

    def test_happy_path_rejection_and_refund(self) -> None:
        """Verify vendor rejection requests simulated refund, completed by refund confirmation."""
        order = Order("ORD-TEST-002")

        order.record_payment_success("evt-pay-2", amount_cents=2000)
        snap_reject = order.vendor_reject("evt-rej-2", reason="Out of rice")
        self.assertEqual(snap_reject.order_state, OrderState.REJECTED)
        self.assertEqual(snap_reject.payment_state, PaymentState.REFUND_PENDING)

        snap_refund = order.record_refund_success("evt-ref-2")
        self.assertEqual(snap_refund.order_state, OrderState.REJECTED)
        self.assertEqual(snap_refund.payment_state, PaymentState.REFUNDED)
        self.assertEqual(snap_refund.version, 3)

    def test_cancellation_before_payment(self) -> None:
        """Verify cancellation is supported before payment."""
        order = Order("ORD-TEST-003")
        snap_cancel = order.cancel_before_payment("evt-can-3", reason="Customer changed mind")
        self.assertEqual(snap_cancel.order_state, OrderState.CANCELLED)
        self.assertEqual(snap_cancel.payment_state, PaymentState.CANCELLED)
        self.assertEqual(snap_cancel.version, 1)

    def test_out_of_order_vendor_accept_and_retry(self) -> None:
        """Ensure vendor accept rejected before payment can be retried successfully after payment."""
        order = Order("ORD-TEST-004")
        accept_event = Event("evt-acc-4", EventType.VENDOR_ACCEPT, {"estimated_prep_min": 15})

        # Step 1: Out-of-order attempt before payment
        with self.assertRaises(InvalidTransitionError):
            order.apply(accept_event)

        # Verify states preserved and ID not consumed
        self.assertEqual(order.order_state, OrderState.PENDING_PAYMENT)
        self.assertEqual(order.payment_state, PaymentState.UNPAID)
        self.assertEqual(order.version, 0)
        self.assertNotIn("evt-acc-4", order.consumed_events)

        # Step 2: Payment succeeds
        order.record_payment_success("evt-pay-4", amount_cents=1000)
        self.assertEqual(order.order_state, OrderState.AWAITING_VENDOR)

        # Step 3: Retry the exact same event that previously failed
        snap_retry = order.apply(accept_event)
        self.assertEqual(snap_retry.order_state, OrderState.ACCEPTED)
        self.assertEqual(snap_retry.payment_state, PaymentState.PAID)
        self.assertEqual(snap_retry.last_event_id, "evt-acc-4")
        self.assertIn("evt-acc-4", order.consumed_events)

    def test_out_of_order_vendor_reject_and_retry(self) -> None:
        """Ensure vendor reject rejected before payment can be retried successfully after payment."""
        order = Order("ORD-TEST-005")
        reject_event = Event("evt-rej-5", EventType.VENDOR_REJECT, {"reason": "Sold out"})

        # Out-of-order rejection before payment
        with self.assertRaises(InvalidTransitionError):
            order.apply(reject_event)

        self.assertEqual(order.order_state, OrderState.PENDING_PAYMENT)
        self.assertEqual(order.payment_state, PaymentState.UNPAID)
        self.assertNotIn("evt-rej-5", order.consumed_events)

        # Payment arrives
        order.record_payment_success("evt-pay-5", amount_cents=1200)

        # Retry rejection
        snap_retry = order.apply(reject_event)
        self.assertEqual(snap_retry.order_state, OrderState.REJECTED)
        self.assertEqual(snap_retry.payment_state, PaymentState.REFUND_PENDING)

    def test_out_of_order_refund_confirmation_and_retry(self) -> None:
        """Ensure refund confirmation fails before rejection and succeeds when retried after rejection."""
        order = Order("ORD-TEST-006")
        order.record_payment_success("evt-pay-6", amount_cents=1500)

        refund_event = Event("evt-ref-6", EventType.PAYMENT_REFUND_SUCCESS)

        # Attempt refund before vendor rejection
        with self.assertRaises(InvalidTransitionError):
            order.apply(refund_event)

        self.assertEqual(order.order_state, OrderState.AWAITING_VENDOR)
        self.assertEqual(order.payment_state, PaymentState.PAID)
        self.assertNotIn("evt-ref-6", order.consumed_events)

        # Vendor rejects
        order.vendor_reject("evt-rej-6", reason="Kitchen equipment breakdown")
        self.assertEqual(order.payment_state, PaymentState.REFUND_PENDING)

        # Retry refund confirmation
        snap_refund = order.apply(refund_event)
        self.assertEqual(snap_refund.payment_state, PaymentState.REFUNDED)
        self.assertIn("evt-ref-6", order.consumed_events)

    def test_exact_replay_idempotency(self) -> None:
        """Verify an exact replay returns current state unchanged without side effects."""
        order = Order("ORD-TEST-007")
        order.record_payment_success("evt-pay-7", amount_cents=1100)
        snap1 = order.vendor_accept("evt-acc-7", estimated_prep_min=25)

        # Replay the same accept event with identical ID and payload
        replay_event = Event("evt-acc-7", EventType.VENDOR_ACCEPT, {"estimated_prep_min": 25})
        snap2 = order.apply(replay_event)

        self.assertIs(snap1, snap2)
        self.assertEqual(snap2.version, 2)
        self.assertEqual(len(order.history), 3)

    def test_event_conflict_different_type(self) -> None:
        """Verify reusing an event ID with a different event type raises EventConflictError."""
        order = Order("ORD-TEST-008")
        order.record_payment_success("evt-shared-1", amount_cents=1000)

        conflicting_event = Event("evt-shared-1", EventType.VENDOR_ACCEPT)
        with self.assertRaises(EventConflictError):
            order.apply(conflicting_event)

    def test_event_conflict_different_payload(self) -> None:
        """Verify reusing an event ID with different payload raises EventConflictError."""
        order = Order("ORD-TEST-009")
        order.record_payment_success("evt-pay-9", amount_cents=1000)

        conflicting_event = Event("evt-pay-9", EventType.PAYMENT_SUCCESS, {"amount_cents": 5000})
        with self.assertRaises(EventConflictError):
            order.apply(conflicting_event)

    def test_invalid_transition_cancellation_after_payment(self) -> None:
        """Verify cancellation after payment is rejected and preserves state."""
        order = Order("ORD-TEST-010")
        order.record_payment_success("evt-pay-10", amount_cents=1000)

        with self.assertRaises(InvalidTransitionError):
            order.cancel_before_payment("evt-can-10")

        self.assertEqual(order.order_state, OrderState.AWAITING_VENDOR)
        self.assertEqual(order.payment_state, PaymentState.PAID)
        self.assertNotIn("evt-can-10", order.consumed_events)

    def test_invalid_transition_payment_after_cancellation(self) -> None:
        """Verify payment after cancellation is rejected."""
        order = Order("ORD-TEST-011")
        order.cancel_before_payment("evt-can-11")

        with self.assertRaises(InvalidTransitionError):
            order.record_payment_success("evt-pay-11", amount_cents=1000)

        self.assertEqual(order.order_state, OrderState.CANCELLED)
        self.assertEqual(order.payment_state, PaymentState.CANCELLED)

    def test_invalid_transition_payment_after_paid(self) -> None:
        """Verify paying twice with a different event ID is rejected."""
        order = Order("ORD-TEST-012")
        order.record_payment_success("evt-pay-12a", amount_cents=1000)

        with self.assertRaises(InvalidTransitionError):
            order.record_payment_success("evt-pay-12b", amount_cents=1000)

        self.assertEqual(order.payment_state, PaymentState.PAID)
        self.assertNotIn("evt-pay-12b", order.consumed_events)

    def test_invalid_transition_vendor_decision_after_acceptance(self) -> None:
        """Verify vendor cannot reject or accept again with new event ID after acceptance."""
        order = Order("ORD-TEST-013")
        order.record_payment_success("evt-pay-13", amount_cents=1000)
        order.vendor_accept("evt-acc-13")

        with self.assertRaises(InvalidTransitionError):
            order.vendor_reject("evt-rej-13")

        with self.assertRaises(InvalidTransitionError):
            order.vendor_accept("evt-acc-13-new")

        self.assertEqual(order.order_state, OrderState.ACCEPTED)

    def test_invalid_transition_refund_twice(self) -> None:
        """Verify refund cannot be confirmed twice with different event IDs."""
        order = Order("ORD-TEST-014")
        order.record_payment_success("evt-pay-14", amount_cents=1000)
        order.vendor_reject("evt-rej-14")
        order.record_refund_success("evt-ref-14a")

        with self.assertRaises(InvalidTransitionError):
            order.record_refund_success("evt-ref-14b")

        self.assertEqual(order.payment_state, PaymentState.REFUNDED)
        self.assertNotIn("evt-ref-14b", order.consumed_events)

    def test_immutability_of_snapshots(self) -> None:
        """Verify OrderSnapshot instances cannot be mutated in place."""
        order = Order("ORD-TEST-015")
        snap0 = order.current_snapshot

        with self.assertRaises((FrozenInstanceError, AttributeError)):
            snap0.order_state = OrderState.ACCEPTED  # type: ignore[misc]

        # Verify advancing state creates a new snapshot without mutating previous one
        snap1 = order.record_payment_success("evt-pay-15", amount_cents=1000)
        self.assertEqual(snap0.version, 0)
        self.assertEqual(snap0.order_state, OrderState.PENDING_PAYMENT)
        self.assertEqual(snap1.version, 1)
        self.assertEqual(snap1.order_state, OrderState.AWAITING_VENDOR)


if __name__ == "__main__":
    unittest.main()
