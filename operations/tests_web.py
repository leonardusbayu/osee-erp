from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Membership, Organization
from finance.models import Party, Product
from operations import intake, services


class OperationsWebTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic UI")
        self.owner = get_user_model().objects.create_user(username="ops-ui-owner")
        self.marketing = get_user_model().objects.create_user(
            username="ops-ui-marketing"
        )
        self.operations = get_user_model().objects.create_user(
            username="ops-ui-operations"
        )
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        Membership.objects.create(
            organization=self.org, user=self.marketing, role="marketing"
        )
        Membership.objects.create(
            organization=self.org, user=self.operations, role="operations"
        )
        self.client.force_login(self.owner)
        self.party = Party.objects.create(
            organization=self.org, kind="customer", name="Synthetic buyer"
        )
        self.product = Product.objects.create(
            organization=self.org,
            kind="itp",
            code="ITP",
            name="ITP",
            default_price="510000",
        )
        self.order = services.create_order(
            organization=self.org,
            actor=self.owner,
            reference="UI-1",
            buyer=self.party,
            date="2026-01-05",
        )
        self.line = services.add_order_line(
            organization=self.org,
            actor=self.owner,
            order=self.order,
            product=self.product,
            quantity=2,
        )
        self.reg = services.register_participant(
            organization=self.org,
            actor=self.owner,
            line=self.line,
            source="UI",
            external_id="R1",
            name="Synthetic participant",
        )

    def test_all_owner_pages_render_and_intake_upload_reaches_mapping(self):
        urls = [
            reverse("operations:overview"),
            reverse("operations:order_create"),
            reverse("operations:sessions"),
            reverse("operations:order", args=[self.order.pk]),
            reverse("operations:line_create", args=[self.order.pk]),
            reverse("operations:line_invoice", args=[self.line.pk]),
            reverse("operations:registration_create", args=[self.line.pk]),
            reverse("operations:registration", args=[self.reg.pk]),
            reverse("operations:intake_upload"),
        ]
        urls += [
            reverse("operations:registration_action", args=[self.reg.pk, action])
            for action in ["schedule", "attendance", "complete", "cancel"]
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(
            reverse("operations:intake_upload"),
            {
                "source": "FORM",
                "file": SimpleUploadedFile("test.csv", b"id,name\nR-2,Synthetic two\n"),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response["Location"]), "Cocokkan kolom")

    def test_marketing_cannot_read_participant_contacts_or_uploads(self):
        self.client.force_login(self.marketing)
        for url in [
            reverse("operations:overview"),
            reverse("operations:registration", args=[self.reg.pk]),
            reverse("operations:intake_upload"),
        ]:
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_operations_role_can_manage_fulfillment_but_not_financial_controls(self):
        from finance.models import Invoice

        self.client.force_login(self.operations)
        allowed_urls = (
            reverse("operations:overview"),
            reverse("operations:order_create"),
            reverse("operations:order", args=[self.order.pk]),
            reverse("operations:line_create", args=[self.order.pk]),
            reverse("operations:registration_create", args=[self.line.pk]),
            reverse("operations:registration", args=[self.reg.pk]),
            reverse("operations:sessions"),
            reverse("operations:intake_upload"),
        )
        for url in allowed_urls:
            with self.subTest(allowed=url):
                self.assertEqual(self.client.get(url).status_code, 200)

        denied_urls = (
            reverse("operations:line_invoice", args=[self.line.pk]),
            reverse("operations:existing_invoice", args=[self.line.pk]),
            reverse("operations:order_lead", args=[self.order.pk]),
            reverse("operations:registration_action", args=[self.reg.pk, "complete"]),
            reverse("operations:registration_action", args=[self.reg.pk, "cancel"]),
        )
        for url in denied_urls:
            with self.subTest(denied=url):
                self.assertEqual(self.client.get(url).status_code, 403)

        issue_response = self.client.post(
            reverse("operations:line_invoice", args=[self.line.pk]),
            {
                "date": self.order.date.isoformat(),
                "due_date": self.order.date.isoformat(),
                "action": "preview",
            },
        )
        self.assertEqual(issue_response.status_code, 403)
        self.assertFalse(Invoice.objects.exists())

    def test_operations_pages_hide_invoice_number_and_exact_balance(self):
        invoice = services.invoice_line(
            organization=self.org,
            actor=self.owner,
            line=self.line,
            date=self.order.date,
        )
        self.client.force_login(self.operations)

        for url in (
            reverse("operations:order", args=[self.order.pk]),
            reverse("operations:registration", args=[self.reg.pk]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, invoice.number)
                self.assertNotContains(response, "1.020.000")
                self.assertNotContains(response, f"/sales/{invoice.pk}/")

    def test_operations_cannot_bypass_financial_or_attribution_guards_in_services(self):
        from finance.models import Invoice

        self.assertFalse(Invoice.objects.exists())
        with self.assertRaises(PermissionDenied):
            services.invoice_quote(
                organization=self.org,
                actor=self.operations,
                line=self.line,
                date=self.order.date,
            )
        with self.assertRaises(PermissionDenied):
            services.invoice_line(
                organization=self.org,
                actor=self.operations,
                line=self.line,
                date=self.order.date,
            )
        with self.assertRaises(PermissionDenied):
            services.link_order_lead(
                organization=self.org,
                actor=self.operations,
                order=self.order,
                lead=None,
                expected_version=self.order.version,
                reason="Synthetic test",
            )
        with self.assertRaises(PermissionDenied):
            services.complete_registration(
                organization=self.org,
                actor=self.operations,
                registration=self.reg,
                expected_version=self.reg.version,
                date=self.order.date,
                evidence="Synthetic test",
            )
        invoice = services.invoice_line(
            organization=self.org,
            actor=self.owner,
            line=self.line,
            date=self.order.date,
        )
        with self.assertRaises(PermissionDenied):
            services.link_order_invoice(
                organization=self.org,
                actor=self.operations,
                line=self.line,
                invoice=invoice,
                reason="Synthetic test",
            )
        self.assertEqual(Invoice.objects.count(), 1)

    def test_invoice_requires_matching_confirmed_price_preview(self):
        from finance.models import Invoice

        url = reverse("operations:line_invoice", args=[self.line.pk])
        data = {"date": "2026-01-05", "due_date": "2026-01-05", "action": "preview"}
        preview = self.client.post(url, data)
        self.assertEqual(preview.context["quote"]["total"], "1020000.00")
        self.assertFalse(Invoice.objects.exists())
        token = preview.context["form"]["quote_token"].value()
        self.client.post(url, {**data, "action": "issue", "quote_token": token})
        self.assertFalse(Invoice.objects.exists())
        self.product.default_price = "530000"
        self.product.save()
        self.client.post(
            url, {**data, "action": "issue", "quote_token": token, "confirm": "on"}
        )
        self.assertFalse(Invoice.objects.exists())
        fresh = self.client.post(url, data)
        response = self.client.post(
            url,
            {
                **data,
                "action": "issue",
                "quote_token": fresh.context["form"]["quote_token"].value(),
                "confirm": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Invoice.objects.get().total, 1060000)

    def test_staff_can_correct_participant_identity_with_confirmation_and_history(self):
        from operations.models import OperationEvent

        registration_url = reverse("operations:registration", args=[self.reg.pk])
        action_url = reverse(
            "operations:registration_action", args=[self.reg.pk, "correct-identity"]
        )
        self.assertContains(self.client.get(registration_url), "Koreksi data peserta")
        page = self.client.get(action_url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["form"]["name"].value(), "Synthetic participant")
        data = {
            "expected_version": self.reg.version,
            "name": "Synthetic participant corrected",
            "email": "corrected@example.test",
            "phone": "081200000099",
            "reason": "Bukti dari formulir asli diperiksa",
            "confirm": "on",
        }
        invalid = self.client.post(action_url, {**data, "confirm": ""})
        self.assertEqual(invalid.status_code, 200)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.participant.name, "Synthetic participant")
        response = self.client.post(action_url, data)
        self.assertRedirects(response, registration_url)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.version, 2)
        self.assertEqual(
            (
                self.reg.participant.name,
                self.reg.participant.email,
                self.reg.participant.phone,
            ),
            (
                "Synthetic participant corrected",
                "corrected@example.test",
                "081200000099",
            ),
        )
        self.assertTrue(
            OperationEvent.objects.filter(
                organization=self.org,
                object_type="registration",
                object_id=self.reg.pk,
                action="registration.identity_corrected",
                before__name="Synthetic participant",
                after__name="Synthetic participant corrected",
            ).exists()
        )
        self.client.force_login(self.marketing)
        self.assertEqual(self.client.get(action_url).status_code, 403)
        self.assertEqual(self.client.post(action_url, data).status_code, 403)

    def test_staff_can_correct_unbooked_attendance_and_must_confirm_the_reason(self):
        from datetime import timedelta

        from django.utils import timezone

        from operations.models import OperationEvent

        buyer = Party.objects.create(
            organization=self.org, kind="customer", name="Synthetic attendance buyer"
        )
        order = services.create_order(
            organization=self.org,
            actor=self.owner,
            reference="ATTENDANCE-UI",
            buyer=buyer,
            date=timezone.localdate() - timedelta(days=2),
        )
        line = services.add_order_line(
            organization=self.org,
            actor=self.owner,
            order=order,
            product=self.product,
            quantity=1,
        )
        services.invoice_line(
            organization=self.org,
            actor=self.owner,
            line=line,
            date=timezone.localdate() - timedelta(days=2),
        )
        reg = services.register_participant(
            organization=self.org,
            actor=self.owner,
            line=line,
            source="manual",
            external_id="ATTENDANCE-UI-1",
            name="Synthetic attendee",
        )
        session = services.create_session(
            organization=self.org,
            actor=self.owner,
            reference="ATTENDANCE-UI-SESSION",
            product=self.product,
            starts_at=timezone.now() - timedelta(days=1),
            capacity=1,
        )
        reg = services.schedule_registration(
            organization=self.org,
            actor=self.owner,
            registration=reg,
            session=session,
            expected_version=1,
            reason="Synthetic confirmation",
        )
        reg = services.record_attendance(
            organization=self.org,
            actor=self.owner,
            registration=reg,
            expected_version=2,
            attended=True,
            evidence="Synthetic attendance list",
        )

        detail_url = reverse("operations:registration", args=[reg.pk])
        self.assertContains(self.client.get(detail_url), "Koreksi kehadiran")
        action_url = reverse(
            "operations:registration_action", args=[reg.pk, "correct-attendance"]
        )
        page = self.client.get(action_url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["form"]["attended"].value(), "yes")
        data = {
            "expected_version": reg.version,
            "attended": "no",
            "reason": "Daftar tes diperiksa kembali oleh staf",
            "confirm": "on",
        }
        invalid = self.client.post(action_url, {**data, "confirm": ""})
        self.assertEqual(invalid.status_code, 200)
        reg.refresh_from_db()
        self.assertEqual(reg.status, "attended")
        response = self.client.post(action_url, data)
        self.assertRedirects(response, detail_url)
        reg.refresh_from_db()
        self.assertEqual((reg.status, reg.version), ("absent", 4))
        self.assertTrue(
            OperationEvent.objects.filter(
                organization=self.org,
                object_type="registration",
                object_id=reg.pk,
                action="registration.attendance_corrected",
            ).exists()
        )
        self.client.force_login(self.marketing)
        self.assertEqual(self.client.get(action_url).status_code, 403)
        self.assertEqual(self.client.post(action_url, data).status_code, 403)

    def test_csrf_and_foreign_organization_are_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(
            client.post(reverse("operations:order_create"), {}).status_code, 403
        )
        other = Organization.objects.create(name="Other UI")
        other_user = get_user_model().objects.create_user(username="other-ui")
        Membership.objects.create(organization=other, user=other_user, role="owner")
        self.client.force_login(other_user)
        self.assertEqual(
            self.client.get(
                reverse("operations:order", args=[self.order.pk])
            ).status_code,
            404,
        )

    def test_intake_review_page_renders_checks_and_accepts_selected_rows(self):
        batch = intake.stage_upload(
            organization=self.org,
            actor=self.owner,
            content=b"id,name\nR-2,Synthetic two\n",
            filename="test.csv",
            source="FORM",
        )
        self.assertEqual(
            self.client.post(
                reverse("operations:intake_map", args=[batch.pk]),
                {"expected_version": 1, "external_id": "0", "name": "1"},
            ).status_code,
            302,
        )
        url = reverse("operations:intake_review", args=[batch.pk])
        self.assertContains(self.client.get(url), "Synthetic two")
        response = self.client.post(
            url,
            {
                "expected_version": 2,
                "row_ids": [batch.rows.first().pk],
                "order": self.order.pk,
                "product": self.product.pk,
                "reason": "Buyer checked",
                "confirm_identity": "on",
                "operation": "accept",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            services.order_summary(organization=self.org, order=self.order)[
                "registration_count"
            ],
            2,
        )
