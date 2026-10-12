from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Membership, Organization
from finance.models import Invoice, Journal, Party, Product
from operations.services import (
    add_order_line,
    create_order,
    order_summary,
    register_participant,
)


class OrderIntakeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic operations")
        self.actor = get_user_model().objects.create_user(username="ops-owner")
        Membership.objects.create(organization=self.org, user=self.actor, role="owner")
        self.buyer = Party.objects.create(
            organization=self.org, name="Synthetic partner", kind="reseller"
        )
        self.product = Product.objects.create(
            organization=self.org,
            code="ITP",
            name="ITP",
            kind="itp",
            default_price="510000",
        )

    def test_registration_is_separate_from_buyer_and_does_not_create_money(self):
        order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="ORDER-1",
            buyer=self.buyer,
            date="2026-01-05",
        )
        line = add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=2,
        )
        first = register_participant(
            organization=self.org,
            actor=self.actor,
            line=line,
            name="Participant one",
            source="form",
            external_id="response-1",
        )
        repeat = register_participant(
            organization=self.org,
            actor=self.actor,
            line=line,
            name="Participant one",
            source="form",
            external_id="response-1",
        )
        self.assertEqual(first.pk, repeat.pk)
        self.assertEqual(
            order_summary(organization=self.org, order=order)["registration_count"], 1
        )
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(Journal.objects.exists())

    def test_participant_identity_correction_preserves_source_and_commercial_links(
        self,
    ):
        from datetime import datetime

        from django.utils import timezone

        from core.models import AuditEvent
        from marketing.services import create_lead
        from operations.models import OperationEvent, Participant
        from operations.services import (
            correct_registration_identity,
            invoice_line,
            link_order_lead,
        )

        order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="IDENTITY-CORRECTION",
            buyer=self.buyer,
            date="2026-01-05",
        )
        lead = create_lead(
            organization=self.org,
            actor=self.actor,
            reference="IDENTITY-LEAD",
            name="Identity correction source",
            channel="mitra",
            party=self.buyer,
            segment="reseller",
            received_at=timezone.make_aware(datetime(2026, 1, 4, 10)),
        )
        order = link_order_lead(
            organization=self.org,
            actor=self.actor,
            order=order,
            lead=lead,
            expected_version=1,
            reason="Synthetic source attribution confirmed",
            confirm_identity=True,
        )
        line = add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=1,
        )
        invoice_line(
            organization=self.org,
            actor=self.actor,
            line=line,
            date="2026-01-05",
        )
        registration = register_participant(
            organization=self.org,
            actor=self.actor,
            line=line,
            name="Synthetic original participant",
            email="original@example.test",
            phone="081200000001",
            source="website-form",
            external_id="FORM-ORIGINAL-42",
        )
        original = registration.participant
        commercial_before = (
            registration.line_id,
            registration.line.order_id,
            registration.line.order.buyer_id,
            registration.line.invoice_id,
            registration.line.order.lead_id,
            registration.source,
            registration.external_id,
        )
        journal_count = Journal.objects.count()
        invoice_count = Invoice.objects.count()

        with self.assertRaises(ValidationError):
            correct_registration_identity(
                organization=self.org,
                actor=self.actor,
                registration=registration,
                expected_version=0,
                name="Corrected participant",
                email="corrected@example.test",
                phone="081200000002",
                reason="Stale copy",
            )
        with self.assertRaises(ValidationError):
            correct_registration_identity(
                organization=self.org,
                actor=self.actor,
                registration=registration,
                expected_version=1,
                name="Corrected participant",
                email="not-an-email",
                phone="081200000002",
                reason="Evidence reviewed by finance",
            )
        corrected = correct_registration_identity(
            organization=self.org,
            actor=self.actor,
            registration=registration,
            expected_version=1,
            name="Corrected participant",
            email="corrected@example.test",
            phone="081200000002",
            reason="Evidence reviewed by finance",
        )

        reimported = register_participant(
            organization=self.org,
            actor=self.actor,
            line=line,
            name="Synthetic original participant",
            email="original@example.test",
            phone="081200000001",
            source="website-form",
            external_id="FORM-ORIGINAL-42",
        )
        self.assertEqual(reimported.pk, registration.pk)
        reimported.refresh_from_db()
        self.assertEqual(
            (
                reimported.participant.name,
                reimported.participant.email,
                reimported.participant.phone,
            ),
            (
                "Corrected participant",
                "corrected@example.test",
                "081200000002",
            ),
        )

        self.assertEqual(corrected.version, 2)
        self.assertNotEqual(corrected.participant_id, original.pk)
        self.assertEqual(
            (original.name, original.email, original.phone),
            (
                "Synthetic original participant",
                "original@example.test",
                "081200000001",
            ),
        )
        self.assertEqual(
            (
                corrected.participant.name,
                corrected.participant.email,
                corrected.participant.phone,
            ),
            (
                "Corrected participant",
                "corrected@example.test",
                "081200000002",
            ),
        )
        event = OperationEvent.objects.get(
            object_type="registration",
            object_id=registration.pk,
            action="registration.identity_corrected",
        )
        self.assertEqual(
            event.before,
            {
                "participant_id": original.pk,
                "name": "Synthetic original participant",
                "email": "original@example.test",
                "phone": "081200000001",
            },
        )
        self.assertEqual(
            event.after,
            {
                "participant_id": corrected.participant_id,
                "name": "Corrected participant",
                "email": "corrected@example.test",
                "phone": "081200000002",
            },
        )
        self.assertEqual(event.reason, "Evidence reviewed by finance")
        general_audit = AuditEvent.objects.get(
            action="operations.registration.identity_corrected",
            object_id=str(registration.pk),
        )
        for private_value in (
            "Synthetic original participant",
            "original@example.test",
            "Corrected participant",
            "corrected@example.test",
            "081200000001",
            "081200000002",
        ):
            self.assertNotIn(private_value, str(general_audit.detail))
        self.assertEqual(
            (
                corrected.line_id,
                corrected.line.order_id,
                corrected.line.order.buyer_id,
                corrected.line.invoice_id,
                corrected.line.order.lead_id,
                corrected.source,
                corrected.external_id,
            ),
            commercial_before,
        )
        self.assertEqual(Participant.objects.filter(organization=self.org).count(), 2)
        self.assertEqual(Journal.objects.count(), journal_count)
        self.assertEqual(Invoice.objects.count(), invoice_count)

    def test_order_links_all_invoice_components_to_one_lead_without_double_attribution(
        self,
    ):
        from datetime import datetime

        from django.utils import timezone

        from marketing.services import create_lead
        from operations.attribution import invoice_attribution
        from operations.services import invoice_line, link_order_lead

        lead = create_lead(
            organization=self.org,
            actor=self.actor,
            reference="LEAD-ORDER",
            name="Buying opportunity",
            channel="mitra",
            party=self.buyer,
            segment="reseller",
            received_at=timezone.make_aware(datetime(2026, 1, 4, 10)),
        )
        order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="LINKED-ORDER",
            buyer=self.buyer,
            date="2026-01-05",
        )
        link_order_lead(
            organization=self.org,
            actor=self.actor,
            order=order,
            lead=lead,
            expected_version=1,
            reason="Buyer verified",
        )
        first = add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=1,
            reference="A",
        )
        second = add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=1,
            reference="B",
        )
        a = invoice_line(
            organization=self.org, actor=self.actor, line=first, date="2026-01-05"
        )
        b = invoice_line(
            organization=self.org, actor=self.actor, line=second, date="2026-01-05"
        )
        mapping = invoice_attribution(organization=self.org)
        self.assertEqual(mapping["by_lead"][lead.pk], [a.pk, b.pk])

    def test_group_order_requires_payment_and_completes_only_attending_unit(self):
        from datetime import datetime

        from django.utils import timezone

        from finance.models import BankAccount, BankTransaction
        from finance.services import reconcile_receipt, report_balances
        from operations.services import (
            complete_registration,
            create_session,
            invoice_line,
            record_attendance,
            schedule_registration,
        )

        order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="GROUP-1",
            buyer=self.buyer,
            date="2026-01-05",
        )
        line = add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=2,
        )
        reg = register_participant(
            organization=self.org,
            actor=self.actor,
            line=line,
            name="Participant",
            source="form",
            external_id="R1",
        )
        invoice = invoice_line(
            organization=self.org, actor=self.actor, line=line, date="2026-01-05"
        )
        session = create_session(
            organization=self.org,
            actor=self.actor,
            reference="SESSION-1",
            product=self.product,
            starts_at=timezone.make_aware(datetime(2026, 1, 5, 10)),
            capacity=2,
        )
        reg = schedule_registration(
            organization=self.org,
            actor=self.actor,
            registration=reg,
            session=session,
            expected_version=1,
            reason="Customer confirmed session",
        )
        with self.assertRaises(ValidationError):
            record_attendance(
                organization=self.org,
                actor=self.actor,
                registration=reg,
                expected_version=2,
                attended=True,
                evidence="Attendance",
            )
        account = BankAccount.objects.create(
            organization=self.org, name="Synthetic bank", account_number="TEST"
        )
        bank = BankTransaction.objects.create(
            organization=self.org,
            account=account,
            date="2026-01-05",
            reference="R",
            amount="1020000",
        )
        reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=bank,
            amount="1020000",
        )
        reg = record_attendance(
            organization=self.org,
            actor=self.actor,
            registration=reg,
            expected_version=2,
            attended=True,
            evidence="Signed roster",
        )
        complete_registration(
            organization=self.org,
            actor=self.actor,
            registration=reg,
            expected_version=3,
            date="2026-01-05",
            evidence="Test completed",
        )
        self.assertEqual(
            order_summary(organization=self.org, order=order)["completed_count"], 1
        )
        self.assertEqual(report_balances(organization=self.org)["REVENUE"], -510000)

    def test_intake_preview_and_confirmation_preserve_source_without_inventing_payment(
        self,
    ):
        from operations.intake import accept_rows, map_batch, stage_upload

        upload = b"id,name,date\nresponse-1,Synthetic participant,2026-01-05\n"
        batch = stage_upload(
            organization=self.org,
            actor=self.actor,
            content=upload,
            filename="registration.csv",
            source="form",
        )
        batch = map_batch(
            organization=self.org,
            actor=self.actor,
            batch=batch,
            mapping={"external_id": "0", "name": "1", "planned_date": "2"},
            expected_version=1,
        )
        order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="INTAKE-ORDER",
            buyer=self.buyer,
            date="2026-01-05",
        )
        result = accept_rows(
            organization=self.org,
            actor=self.actor,
            batch=batch,
            row_ids=list(batch.rows.values_list("pk", flat=True)),
            order=order,
            product=self.product,
            expected_version=2,
            reason="Buyer and source identity checked",
            confirm_identity=True,
        )
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(
            order_summary(organization=self.org, order=order)["registration_count"], 1
        )
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(Journal.objects.exists())

    def test_month_close_respects_explicit_future_sessions_without_releasing_revenue(
        self,
    ):
        from datetime import datetime

        from django.utils import timezone

        from finance.services import monthly_summary
        from operations.services import (
            create_session,
            invoice_line,
            schedule_registration,
        )

        buyer = Party.objects.create(
            organization=self.org, name="Synthetic direct", kind="customer"
        )
        order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="FUTURE",
            buyer=buyer,
            date="2026-01-05",
        )
        line = add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=1,
        )
        reg = register_participant(
            organization=self.org,
            actor=self.actor,
            line=line,
            name="Later test",
            source="form",
            external_id="LATER",
        )
        invoice_line(
            organization=self.org, actor=self.actor, line=line, date="2026-01-05"
        )
        self.assertTrue(
            any(
                "Layanan jatuh tempo" in value
                for value in monthly_summary(organization=self.org, year=2026, month=1)[
                    "blockers"
                ]
            )
        )
        session = create_session(
            organization=self.org,
            actor=self.actor,
            reference="FEB",
            product=self.product,
            starts_at=timezone.make_aware(datetime(2026, 2, 5, 10)),
            capacity=1,
        )
        schedule_registration(
            organization=self.org,
            actor=self.actor,
            registration=reg,
            session=session,
            expected_version=1,
            reason="Customer confirmed February",
        )
        summary = monthly_summary(organization=self.org, year=2026, month=1)
        self.assertEqual(summary["blockers"], [])
        self.assertEqual(summary["balances"]["DEFERRED_REVENUE"], -510000)
        self.assertEqual(summary["balances"]["REVENUE"], 0)

    def test_attendance_can_be_corrected_with_history_before_delivery_only(self):
        from datetime import datetime, timedelta

        from django.utils import timezone

        from finance.fulfillment_models import InvoiceCredit, InvoiceDelivery
        from finance.models import Journal
        from operations.models import OperationEvent
        from operations.services import (
            complete_registration,
            correct_attendance,
            create_session,
            invoice_line,
            record_attendance,
            schedule_registration,
        )

        buyer = Party.objects.create(
            organization=self.org, name="Synthetic direct buyer", kind="customer"
        )
        day = timezone.localdate() - timedelta(days=2)
        order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="ATTENDANCE-CORRECTION",
            buyer=buyer,
            date=day,
        )
        line = add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=1,
        )
        invoice_line(organization=self.org, actor=self.actor, line=line, date=day)
        registration = register_participant(
            organization=self.org,
            actor=self.actor,
            line=line,
            name="Synthetic attendee",
            source="manual",
            external_id="ATTENDANCE-CORRECTION-1",
        )
        session = create_session(
            organization=self.org,
            actor=self.actor,
            reference="ATTENDANCE-CORRECTION-SESSION",
            product=self.product,
            starts_at=timezone.make_aware(datetime.combine(day, datetime.min.time())),
            capacity=1,
        )
        registration = schedule_registration(
            organization=self.org,
            actor=self.actor,
            registration=registration,
            session=session,
            expected_version=1,
            reason="Synthetic schedule",
        )
        registration = record_attendance(
            organization=self.org,
            actor=self.actor,
            registration=registration,
            expected_version=2,
            attended=True,
            evidence="Synthetic roster",
        )
        before = (
            Journal.objects.count(),
            InvoiceDelivery.objects.count(),
            InvoiceCredit.objects.count(),
            OperationEvent.objects.filter(
                object_type="registration", object_id=registration.pk
            ).count(),
        )

        with self.assertRaises(ValidationError):
            correct_attendance(
                organization=self.org,
                actor=self.actor,
                registration=registration,
                expected_version=2,
                attended=False,
                reason="Stale synthetic correction",
            )
        corrected = correct_attendance(
            organization=self.org,
            actor=self.actor,
            registration=registration,
            expected_version=3,
            attended=False,
            reason="Synthetic roster correction confirmed by reviewer",
        )
        self.assertEqual(
            (corrected.status, corrected.version, corrected.delivery_id),
            ("absent", 4, None),
        )
        event = OperationEvent.objects.filter(
            object_type="registration",
            object_id=registration.pk,
            action="registration.attendance_corrected",
        ).get()
        self.assertEqual(
            (event.before, event.after), ({"status": "attended"}, {"status": "absent"})
        )
        self.assertIn("reviewer", event.reason)
        self.assertEqual(
            (
                Journal.objects.count(),
                InvoiceDelivery.objects.count(),
                InvoiceCredit.objects.count(),
            ),
            before[:3],
        )

        corrected = correct_attendance(
            organization=self.org,
            actor=self.actor,
            registration=corrected,
            expected_version=4,
            attended=True,
            reason="Synthetic roster rechecked and participant confirmed present",
        )
        self.assertEqual(corrected.status, "attended")
        completed = complete_registration(
            organization=self.org,
            actor=self.actor,
            registration=corrected,
            expected_version=5,
            date=day,
            evidence="Synthetic completion after corrected attendance",
        )
        self.assertEqual(completed.status, "completed")
        with self.assertRaises(ValidationError):
            correct_attendance(
                organization=self.org,
                actor=self.actor,
                registration=completed,
                expected_version=completed.version,
                attended=False,
                reason="Too late",
            )
        self.assertEqual(InvoiceDelivery.objects.count(), 1)
        self.assertEqual(InvoiceCredit.objects.count(), 0)
