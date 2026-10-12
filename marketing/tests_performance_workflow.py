"""Public workflows for complete-source KPI review and accountable execution."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from . import kpi, services
from . import tests_kpi as fixtures


class CompleteSourceWorkflowTests(TestCase):
    setUp = fixtures.MarketingKpiTests.setUp
    lead = fixtures.MarketingKpiTests.lead
    target = fixtures.MarketingKpiTests.target
    activate = fixtures.MarketingKpiTests.activate
    invoice = fixtures.MarketingKpiTests.invoice
    receipt = fixtures.MarketingKpiTests.receipt

    def source_row(self, **changes):
        return {
            "kind": "leads",
            "name": "Complete form export",
            "reference": "SOURCE-1",
            "expected_count": 1,
            "imported_count": 1,
            "duplicate_count": 0,
            "excluded_count": 0,
            "unresolved_count": 0,
            "evidence": "Compared all source rows",
            **changes,
        }

    def test_inventory_approval_rejects_role_mismatch_counts_and_stale_data(self):
        from .kpi_completeness import approve_source_period, create_source_period

        self.lead("SOURCES")
        target = self.activate(self.target())
        period = create_source_period(
            organization=self.org,
            actor=self.marketing,
            target=target,
            reference="WRONG-COUNT",
            sources=[self.source_row(expected_count=2)],
            inventory_note="All sources listed",
            confirm_all_sources=True,
        )
        command = dict(
            organization=self.org,
            period=period,
            expected_version=period.version,
            expected_fingerprint=period.data_fingerprint,
            reason="Review counts",
        )
        with self.assertRaises(PermissionDenied):
            approve_source_period(actor=self.marketing, **command)
        with self.assertRaises(ValidationError):
            approve_source_period(actor=self.actor, **command)
        period.refresh_from_db()
        self.assertEqual(period.status, "draft")
        period = create_source_period(
            organization=self.org,
            actor=self.marketing,
            target=target,
            reference="STALE",
            sources=[self.source_row()],
            inventory_note="All sources listed",
            confirm_all_sources=True,
        )
        self.lead("LATE-SOURCE")
        with self.assertRaisesMessage(ValidationError, "Data berubah"):
            approve_source_period(
                organization=self.org,
                actor=self.actor,
                period=period,
                expected_version=period.version,
                expected_fingerprint=period.data_fingerprint,
                reason="Old inventory cannot authorize new rows",
            )

    def test_cash_kpi_requires_actual_finance_period_review_before_source_approval(
        self,
    ):
        from .kpi_completeness import approve_source_period, create_source_period

        invoice = self.invoice("CASH-SOURCE")
        self.lead("CASH-SOURCE", invoice)
        self.receipt(invoice, "25", "CASH-SOURCE")
        target = self.activate(self.target(metric_key="cash_in", target="50"))
        period = create_source_period(
            organization=self.org,
            actor=self.marketing,
            target=target,
            reference="BANK-NOT-REVIEWED",
            sources=[self.source_row(), self.source_row(kind="bank", reference="BANK")],
            inventory_note="All accounts listed",
            confirm_all_sources=True,
        )
        with self.assertRaisesMessage(ValidationError, "Finance"):
            approve_source_period(
                organization=self.org,
                actor=self.actor,
                period=period,
                expected_version=period.version,
                expected_fingerprint=period.data_fingerprint,
                reason="Rows alone are not statement completeness",
            )
        self.assertFalse(kpi.evaluate_target(target)["official"])

    @override_settings(MFA_REQUIRED=False)
    def test_finance_source_replacement_invalidates_cash_kpi_even_when_amount_unchanged(
        self,
    ):
        from tempfile import TemporaryDirectory

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse

        from finance.models import BankTransaction
        from finance.services import reconcile_receipt
        from finance.statement_services import (
            preview_statement_period,
            review_statement_period,
        )
        from webapp.models import StatementImport

        from .kpi_completeness import approve_source_period, create_source_period

        invoice = self.invoice("VERIFIED-CASH")
        self.lead("VERIFIED-CASH", invoice)
        self.client.force_login(self.actor)
        with TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            uploaded = self.client.post(
                reverse("bank_import"),
                {
                    "account": self.account.pk,
                    "file": SimpleUploadedFile(
                        "full-january.csv",
                        b"date,reference,description,amount\n2026-01-10,BANK-PROOF,Receipt,25\n",
                    ),
                },
            )
            self.assertEqual(uploaded.status_code, 302)
            statement = StatementImport.objects.get(organization=self.org)
            url = reverse("statement_preview", args=[statement.pk])
            preview = self.client.post(
                url,
                {
                    "date": "0",
                    "reference": "1",
                    "description": "2",
                    "amount": "3",
                    "number_format": "en",
                    "reference_mode": "bank_reference",
                    "first_row": 1,
                    "last_row": 1,
                },
            )
            self.assertEqual(
                self.client.post(
                    url,
                    {
                        "action": "confirm",
                        "preview_token": preview.context["token"],
                        "confirmed": "yes",
                    },
                ).status_code,
                302,
            )
            statement.refresh_from_db()
            reconcile_receipt(
                organization=self.org,
                actor=self.actor,
                invoice=invoice,
                transaction=BankTransaction.objects.get(organization=self.org),
                amount="25",
            )
            facts = dict(
                organization=self.org,
                actor=self.actor,
                statement=statement,
                period_start="2026-01-01",
                period_end="2026-01-31",
                opening_balance="0",
                closing_balance="25",
                total_in="25",
                total_out="0",
                transaction_count=1,
                source_order="ascending",
                evidence="Entire January statement checked",
            )
            bank_preview = preview_statement_period(**facts)
            bank_review = review_statement_period(
                **facts,
                expected_fingerprint=bank_preview["fingerprint"],
                confirmed=True,
            )
            target = self.activate(self.target(metric_key="cash_in", target="50"))
            period = create_source_period(
                organization=self.org,
                actor=self.actor,
                target=target,
                reference="FULL-CASH",
                sources=[
                    self.source_row(),
                    self.source_row(kind="bank", reference="BANK"),
                ],
                inventory_note="Full lead and bank inventory",
                confirm_all_sources=True,
            )
            approve_source_period(
                organization=self.org,
                actor=self.actor,
                period=period,
                expected_version=period.version,
                expected_fingerprint=period.data_fingerprint,
                reason="Control totals verified",
            )
            before = kpi.evaluate_target(target)
            self.assertTrue(before["official"])
            facts["evidence"] = (
                "Reference correction checked against same source amounts"
            )
            replacement_preview = preview_statement_period(**facts)
            review_statement_period(
                **facts,
                expected_fingerprint=replacement_preview["fingerprint"],
                supersedes=bank_review,
                confirmed=True,
            )
            after = kpi.evaluate_target(target)
            self.assertEqual(after["actual"], before["actual"])
            self.assertFalse(after["official"])
            self.assertNotEqual(after["fingerprint"], before["fingerprint"])

    def test_withdrawal_removes_official_status_without_erasing_prior_review(self):
        from .kpi_completeness import withdraw_source_period

        self.lead("APPROVED")
        target = self.activate(self.target())
        period = fixtures.certify_lead_sources(self.org, self.actor, target)
        before = kpi.evaluate_target(target)
        review = kpi.review_target(
            organization=self.org,
            actor=self.actor,
            target=target,
            expected_version=target.version,
            expected_fingerprint=before["fingerprint"],
            conclusion="Reviewed correct inventory",
        )
        withdraw_source_period(
            organization=self.org,
            actor=self.actor,
            period=period,
            expected_version=period.version,
            reason="Another registration source discovered",
        )
        after = kpi.evaluate_target(target)
        self.assertFalse(after["official"])
        self.assertNotEqual(after["fingerprint"], review.fingerprint)
        review.refresh_from_db()
        self.assertTrue(review.snapshot["official"])

    def test_recorded_response_is_provisional_until_source_period_is_checked(self):
        from .kpi_completeness import approve_source_period, create_source_period

        self.lead("RECORDED")
        target = self.activate(self.target())
        initial = kpi.evaluate_target(target)
        self.assertFalse(initial["official"])
        self.assertEqual(initial["completeness"]["state"], "missing")
        sources = [
            {
                "kind": "leads",
                "name": "Registration export",
                "reference": "JAN-EXPORT",
                "expected_count": 1,
                "imported_count": 1,
                "duplicate_count": 0,
                "excluded_count": 0,
                "unresolved_count": 0,
                "evidence": "Checked complete export",
            }
        ]
        period = create_source_period(
            organization=self.org,
            actor=self.marketing,
            target=target,
            reference="JAN-PERIOD",
            sources=sources,
            inventory_note="All registration and contact sources in target scope checked",
            confirm_all_sources=True,
        )
        approved = approve_source_period(
            organization=self.org,
            actor=self.actor,
            period=period,
            expected_version=period.version,
            expected_fingerprint=initial["basis"]["provenance"]["digest"],
            reason="Compared inventory and imported rows",
        )
        checked = kpi.evaluate_target(target)
        self.assertTrue(checked["official"])
        self.assertEqual(checked["completeness"]["period_id"], approved.pk)
        self.lead("LATE-ARRIVAL")
        changed = kpi.evaluate_target(target)
        self.assertFalse(changed["official"])
        self.assertEqual(changed["completeness"]["state"], "stale")
        with self.assertRaises(ValidationError):
            kpi.review_target(
                organization=self.org,
                actor=self.actor,
                target=target,
                expected_version=target.version,
                expected_fingerprint=changed["fingerprint"],
                conclusion="Cannot approve unreviewed late arrivals",
            )

    def test_verified_empty_lead_inventory_is_known_zero_not_missing(self):
        target = self.activate(self.target())
        self.assertFalse(kpi.evaluate_target(target)["official"])
        fixtures.certify_lead_sources(self.org, self.actor, target)
        result = kpi.evaluate_target(target)
        self.assertTrue(result["official"])
        self.assertEqual(
            (result["actual"], result["attainment"], result["state"]),
            ("0.00", "0.00", "not_met"),
        )

    @override_settings(MFA_REQUIRED=False)
    def test_director_can_inspect_source_inventory_before_approval(self):
        self.lead("HTTP-SOURCE")
        target = self.activate(self.target())
        self.client.force_login(self.marketing)
        response = self.client.get(f"/marketing/kpis/{target.pk}/sources/new/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Seluruh sumber")

    def test_director_brief_exposes_unreviewed_sources_and_waiting_work_without_claiming_success(
        self,
    ):
        from django.utils import timezone

        from .activities import create_activity, update_activity
        from .management import management_snapshot
        from .workflow import configure_member

        self.lead("BRIEF")
        self.activate(self.target())
        configure_member(
            organization=self.org,
            actor=self.actor,
            member=self.member,
            expected_version=self.member.version,
            account=self.marketing,
            active=True,
            reason="Assigned login",
        )
        work = create_activity(
            organization=self.org,
            actor=self.actor,
            title="Private work name",
            owner=self.member,
            channel="meta_ads",
            kind="follow_up",
            due_date=timezone.localdate(),
        )
        update_activity(
            organization=self.org,
            actor=self.marketing,
            activity=work,
            expected_version=work.version,
            operation="submit",
            occurred_on=timezone.localdate(),
            evidence="Private CRM",
            outcome="Call made",
        )
        snapshot = management_snapshot(
            organization=self.org, start="2026-01-01", end="2026-12-31"
        )
        self.assertEqual(snapshot["counts"]["kpi_sources_incomplete"], 1)
        self.assertEqual(snapshot["counts"]["work_waiting_review"], 1)
        self.assertEqual(snapshot["counts"]["kpi_met"], 0)
        self.assertNotIn("Private work name", str(snapshot["ai_facts"]))
        self.assertTrue(snapshot["fingerprint"])

    @override_settings(MFA_REQUIRED=False)
    def test_management_http_and_ai_read_the_same_current_source_warning(self):
        from .ai import marketing_advice
        from .analytics import marketing_snapshot

        self.lead("ADVICE")
        self.activate(self.target())
        snapshot = marketing_snapshot(
            organization=self.org, start="2026-01-01", end="2026-01-31"
        )
        advice = marketing_advice(
            organization=self.org, actor=self.marketing, snapshot=snapshot
        )
        self.assertIn(
            "complete_kpi_sources", [item["id"] for item in advice["recommendations"]]
        )
        self.assertTrue(advice["source_references"])
        self.client.force_login(self.actor)
        response = self.client.get(
            "/marketing/management/?start=2026-01-01&end=2026-01-31"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Target Januari")


class LinkedWorkWorkflowTests(TestCase):
    setUp = fixtures.MarketingKpiTests.setUp

    def test_work_links_to_target_and_corrected_work_retains_accepted_history(self):
        from django.utils import timezone

        from .activities import correct_activity, create_activity, update_activity
        from .workflow import configure_member

        configure_member(
            organization=self.org,
            actor=self.actor,
            member=self.member,
            expected_version=self.member.version,
            account=self.marketing,
            active=True,
            reason="Assigned login",
        )
        target = fixtures.MarketingKpiTests.target(self)
        work = create_activity(
            organization=self.org,
            actor=self.actor,
            title="Follow-up KPI",
            owner=self.member,
            channel="meta_ads",
            kind="follow_up",
            due_date=timezone.localdate(),
            kpi_target=target,
        )
        work = update_activity(
            organization=self.org,
            actor=self.marketing,
            activity=work,
            expected_version=work.version,
            operation="submit",
            occurred_on=timezone.localdate(),
            evidence="Private CRM reference",
            outcome="Follow-up completed",
        )
        work = update_activity(
            organization=self.org,
            actor=self.actor,
            activity=work,
            expected_version=work.version,
            operation="accept",
            review_notes="Reviewed proof",
        )
        replacement = correct_activity(
            organization=self.org,
            actor=self.actor,
            activity=work,
            expected_version=work.version,
            reason="Evidence needs a corrected deliverable",
            due_date=timezone.localdate(),
        )
        self.assertEqual(replacement.replaces_id, work.pk)
        self.assertEqual(replacement.kpi_target_id, target.pk)
        work.refresh_from_db()
        self.assertEqual(work.status, "accepted")
        self.assertEqual(replacement.status, "planned")

    def test_legacy_suggestion_requires_reviewed_work_before_claiming_completion(self):
        from django.utils import timezone

        from .action_workflow import adopt_action

        action = services.create_action(
            organization=self.org,
            actor=self.marketing,
            title="Fix response process",
            owner=self.member,
            due_date=timezone.localdate(),
            description="Follow the approved response process",
            metric_key="leads",
        )
        with self.assertRaises(ValidationError):
            services.update_action(
                organization=self.org,
                actor=self.marketing,
                action=action,
                expected_version=action.version,
                status="done",
                outcome="Claiming completion without review",
            )
        work = adopt_action(
            organization=self.org,
            actor=self.actor,
            action=action,
            expected_version=action.version,
            channel="meta_ads",
            kind="follow_up",
        )
        self.assertEqual(work.legacy_action_id, action.pk)
        self.assertEqual(work.status, "planned")

    @override_settings(MFA_REQUIRED=False)
    def test_private_evidence_download_is_scoped_and_preserves_attachment_digest(self):
        from io import BytesIO
        from tempfile import TemporaryDirectory

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.utils import timezone
        from PIL import Image

        from core.models import Membership, Organization

        from .activities import create_activity, update_activity
        from .work_evidence import attach_work_evidence

        work = create_activity(
            organization=self.org,
            actor=self.actor,
            title="Private proof",
            owner=self.member,
            channel="meta_ads",
            kind="ads",
            due_date=timezone.localdate(),
        )
        buffer = BytesIO()
        Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
        with TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            proof = attach_work_evidence(
                organization=self.org,
                actor=self.actor,
                activity=work,
                expected_version=work.version,
                upload=SimpleUploadedFile("proof.png", buffer.getvalue()),
                responsibility_reason="Supervisor archives evidence before review",
            )
            self.client.force_login(self.actor)
            response = self.client.get(f"/marketing/work-evidence/{proof.pk}/download/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.assertEqual(len(proof.sha256), 64)
            # Let the test client's streaming wrapper close the response safely.
            self.assertEqual(b"".join(response.streaming_content), buffer.getvalue())
            self.assertTrue(response.closed)
            other = Organization.objects.create(name="Other evidence scope")
            Membership.objects.create(organization=other, user=self.actor, role="owner")
            session = self.client.session
            session["organization_id"] = other.pk
            session.save()
            self.assertEqual(
                self.client.get(
                    f"/marketing/work-evidence/{proof.pk}/download/"
                ).status_code,
                404,
            )
            work.refresh_from_db()
            work = update_activity(
                organization=self.org,
                actor=self.actor,
                activity=work,
                expected_version=work.version,
                operation="submit",
                occurred_on=timezone.localdate(),
                evidence="Private attachment supplied",
                outcome="Evidence to review",
                responsibility_reason="Supervisor exception recorded",
            )
            with open(proof.file.path, "wb") as handle:
                handle.write(b"tampered outside the application")
            with self.assertRaisesMessage(ValidationError, "berbeda"):
                update_activity(
                    organization=self.org,
                    actor=self.director,
                    activity=work,
                    expected_version=work.version,
                    operation="accept",
                    review_notes="Must reject changed source bytes",
                )
            work.refresh_from_db()
            self.assertEqual(work.status, "submitted")
