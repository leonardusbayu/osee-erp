from datetime import datetime
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from finance.tests_concurrency import ConcurrentFinanceTests

from . import services
from .tests import OrderIntakeTests


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class OperationRaceTests(TransactionTestCase):
    setUp = OrderIntakeTests.setUp
    race = ConcurrentFinanceTests.race

    def test_last_session_seat_cannot_be_booked_by_two_participants(self):
        order = services.create_order(
            organization=self.org,
            actor=self.actor,
            reference="RACE",
            buyer=self.buyer,
            date="2026-01-05",
        )
        line = services.add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=2,
        )
        regs = [
            services.register_participant(
                organization=self.org,
                actor=self.actor,
                line=line,
                source="form",
                external_id=str(n),
                name="Synthetic",
            )
            for n in range(2)
        ]
        session = services.create_session(
            organization=self.org,
            actor=self.actor,
            reference="SEAT",
            product=self.product,
            starts_at=timezone.make_aware(datetime(2026, 1, 5, 10)),
            capacity=1,
        )
        calls = [
            lambda reg=reg: services.schedule_registration(
                organization=self.org,
                actor=self.actor,
                registration=reg,
                session=session,
                expected_version=1,
                reason="Confirmed participant",
            )
            for reg in regs
        ]
        self.assertCountEqual(self.race(*calls), ["committed", "rejected"])
        self.assertEqual(session.registrations.count(), 1)
