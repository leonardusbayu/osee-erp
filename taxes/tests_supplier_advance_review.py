"""Professional cash-event review contracts, using actual synthetic bank posting.

Execution belongs to the root runtime lane. These fixtures do not manufacture
issued bills, bank postings, journals or supplier advance authority.
"""

from copy import deepcopy
from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse

from core.models import AuditEvent, Membership, Organization
from finance import services as ledger
from finance.models import BankPosting, Journal, SupplierAdvance
from finance.test_statement_fixtures import reviewed_statement
from finance.tests_supplier_advances import SupplierAdvanceFixtures
from finance.tests_tax_money import TaxMoneyFixtures

from . import supplier_advance_review as review
from .models import (
    ExternalTaxReview,
    SupplierAdvanceTaxReview,
    TaxEvidence,
    TaxObligation,
)


class SupplierAdvanceReviewFixtures:
    fixture_setup = TaxMoneyFixtures.setUp
    movement = SupplierAdvanceFixtures.movement
    evidence = SupplierAdvanceFixtures.evidence
    external = SupplierAdvanceFixtures.external
    command = SupplierAdvanceFixtures.command
    advance = SupplierAdvanceFixtures.advance
    bill = SupplierAdvanceFixtures.bill

    def setUp(self):
        self.fixture_setup()
        self.day = date(2026, 1, 1)
        self.client.force_login(self.actor)

    def context(self, advance):
        return review.supplier_advance_tax_context(
            organization=self.org, advance=advance
        )

    def review_data(self, advance, **overrides):
        context = self.context(advance)
        decision = {
            "schema": "supplier-advance-tax-review",
            "version": 1,
            "scope": "original_cash_event",
            "source_fingerprint": context["source_fingerprint"],
            "resolution": "no_withholding",
            "reviewed_facts": {
                "cash_amount": format(advance.amount, ".2f"),
                "payment_date": advance.date.isoformat(),
                "contract_reference": advance.source_reference,
                "gross_amount": None,
                "withholding_amount": "0.00",
                "legal_basis_reference": "Synthetic professional report paragraph 4",
                "professional_instruction": "Synthetic professional conclusion for this exact cash event.",
            },
        }
        data = dict(
            organization=self.org,
            actor=self.actor,
            advance=advance,
            decision=decision,
            evidence=self.evidence(actor=overrides.get("actor", self.actor)),
            professional_name="Synthetic professional",
            qualification_reference="Synthetic documented assignment",
            reviewed_on=date(2026, 1, 2),
            statement="Synthetic received report concludes this original cash event needs no withholding.",
            report_confirmed=True,
            expected_version=context["version"],
            expected_fingerprint=context["fingerprint"],
            request_key=str(uuid4()),
        )
        data.update(overrides)
        return data

    def zero_review(self, advance):
        return review.record_supplier_advance_review(**self.review_data(advance))

    def counts(self):
        return tuple(
            model.objects.count()
            for model in (
                SupplierAdvanceTaxReview,
                ExternalTaxReview,
                AuditEvent,
                Journal,
                BankPosting,
                TaxObligation,
            )
        )

    def user(self, role, organization=None):
        actor = get_user_model().objects.create_user(
            username=f"cash-review-{role}-{uuid4()}"
        )
        Membership.objects.create(
            organization=organization or self.org, user=actor, role=role
        )
        return actor

    def tamper(self, model, pk, field, value):
        # Deliberate stored-data corruption, not an application write seam.
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {quote(model._meta.db_table)} SET {quote(field)} = %s WHERE id = %s",
                [value, pk],
            )


class SupplierAdvanceTaxReviewTests(SupplierAdvanceReviewFixtures, TestCase):
    def test_actual_cash_pending_then_received_professional_zero_review_changes_no_finance(
        self,
    ):
        advance = self.advance()
        finance_before = (
            ledger.report_balances(organization=self.org),
            Journal.objects.count(),
            BankPosting.objects.count(),
        )
        pending = self.context(advance)
        before_read = self.counts()
        self.assertEqual(
            (pending["status"], pending["version"], pending["history"]),
            ("pending", 0, []),
        )
        self.assertTrue(pending["blockers"])
        self.assertEqual(self.context(advance)["fingerprint"], pending["fingerprint"])
        self.assertEqual(self.counts(), before_read)
        decision = self.zero_review(advance)
        current = self.context(advance)
        self.assertEqual(
            (current["status"], current["version"], current["blockers"]),
            ("reviewed", 1, []),
        )
        self.assertEqual([row.pk for row in current["history"]], [decision.pk])
        self.assertIsNone(decision.decision["reviewed_facts"]["gross_amount"])
        self.assertEqual(
            decision.decision["reviewed_facts"]["withholding_amount"], "0.00"
        )
        self.assertEqual(
            finance_before,
            (
                ledger.report_balances(organization=self.org),
                Journal.objects.count(),
                BankPosting.objects.count(),
            ),
        )
        self.assertFalse(TaxObligation.objects.exists())
        self.assertEqual(decision.external_review.subject_type, "supplieradvance")
        self.assertEqual(decision.external_review.subject_id, advance.pk)
        self.assertEqual(
            decision.external_review.evidence_sha256,
            decision.external_review.evidence.sha256,
        )

    def test_zero_residual_pending_still_blocks_and_review_survives_application_and_refund(
        self,
    ):
        advance = self.advance("100")
        self.command(
            "refund",
            advance=advance,
            amount="100",
            transaction=self.movement(
                "100", day=date(2026, 1, 3), reference="SYN-ZERO-REFUND"
            ),
        )
        gates = review.supplier_advance_tax_gates(
            organization=self.org, cutoff=date(2026, 1, 31)
        )
        self.assertEqual(
            [row["advance_id"] for row in gates["inventory"]], [advance.pk]
        )
        self.assertEqual(gates["inventory"][0]["status"], "pending")
        self.assertTrue(gates["blockers"])
        self.zero_review(advance)
        self.assertFalse(
            review.supplier_advance_tax_gates(
                organization=self.org, cutoff=date(2026, 1, 31)
            )["blockers"]
        )
        second = self.advance("200", reference="SYN-SECOND-ORIGINAL")
        self.zero_review(second)
        before = self.context(second)
        self.command(
            "apply",
            advance=second,
            bill=self.bill("100"),
            amount="100",
            date=date(2026, 1, 4),
        )
        self.command(
            "refund",
            advance=second,
            amount="100",
            transaction=self.movement(
                "100", day=date(2026, 1, 5), reference="SYN-SECOND-REFUND"
            ),
        )
        after = self.context(second)
        self.assertEqual(
            (after["status"], after["source_fingerprint"], after["fingerprint"]),
            ("reviewed", before["source_fingerprint"], before["fingerprint"]),
        )
        gates = review.supplier_advance_tax_gates(
            organization=self.org, cutoff=date(2026, 1, 31)
        )
        self.assertEqual(
            [row["advance_id"] for row in gates["inventory"]], [advance.pk, second.pk]
        )
        self.assertFalse(gates["blockers"])

    def test_nonzero_unknown_withholding_or_unresolved_instruction_never_releases_gate(
        self,
    ):
        for resolution, amount in (
            ("no_withholding", None),
            ("no_withholding", "1.00"),
            ("more_evidence", "0.00"),
            ("unsupported_treatment", "0.00"),
        ):
            with self.subTest(resolution=resolution, withholding=amount):
                advance = self.advance("10")
                data = self.review_data(advance)
                data["decision"]["resolution"] = resolution
                data["decision"]["reviewed_facts"]["withholding_amount"] = amount
                row = review.record_supplier_advance_review(**data)
                self.assertEqual(
                    (self.context(advance)["status"], row.version), ("held", 1)
                )
                self.assertTrue(self.context(advance)["blockers"])
        self.assertFalse(TaxObligation.objects.exists())

    def test_schema_exact_cash_date_contract_and_uninferred_gross_are_strict(self):
        advance = self.advance()
        data = self.review_data(advance)
        mutations = [
            lambda value: value.update(version=True),
            lambda value: value.update(extra="ignored"),
            lambda value: value.update(scope="gross_contract"),
            lambda value: value["reviewed_facts"].update(cash_amount="999.00"),
            lambda value: value["reviewed_facts"].update(payment_date="2026-01-02"),
            lambda value: value["reviewed_facts"].update(
                contract_reference="different"
            ),
            lambda value: value["reviewed_facts"].update(gross_amount=0),
            lambda value: value["reviewed_facts"].update(withholding_amount="0"),
            lambda value: value["reviewed_facts"].update(legal_basis_reference=""),
            lambda value: value["reviewed_facts"].pop("professional_instruction"),
        ]
        before = self.counts()
        for mutate in mutations:
            value = deepcopy(data["decision"])
            mutate(value)
            with self.assertRaises(ValidationError):
                review.record_supplier_advance_review(**{**data, "decision": value})
            self.assertEqual(self.counts(), before)

    def test_owner_only_current_scope_and_revoked_writer_are_enforced(self):
        advance = self.advance()
        data = self.review_data(advance)
        before = self.counts()
        for role in ("finance", "reviewer", "auditor", "director", "marketing"):
            with self.subTest(role=role), self.assertRaises(PermissionDenied):
                review.record_supplier_advance_review(
                    **{**data, "actor": self.user(role)}
                )
        foreign = Organization.objects.create(
            name="Synthetic foreign cash review", is_demo=True
        )
        outsider = self.user("owner", foreign)
        with self.assertRaises(PermissionDenied):
            review.record_supplier_advance_review(**{**data, "actor": outsider})
        with self.assertRaises(ValidationError):
            review.record_supplier_advance_review(
                **{**data, "organization": foreign, "actor": outsider}
            )
        Membership.all_objects.filter(organization=self.org, user=self.actor).update(
            is_active=False
        )
        with self.assertRaises(PermissionDenied):
            review.record_supplier_advance_review(**data)
        self.assertEqual(self.counts(), before)

    def test_exact_transport_retry_returns_same_report_and_changed_payload_conflicts(
        self,
    ):
        advance = self.advance()
        data = self.review_data(advance)
        accepted = review.record_supplier_advance_review(**data)
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.actor,
            account=self.account,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            closing="-1000",
            total_out="1000",
            count=1,
        )
        ledger.close_month(organization=self.org, actor=self.actor, year=2026, month=1)
        before = self.counts()
        self.assertEqual(review.record_supplier_advance_review(**data).pk, accepted.pk)
        self.assertEqual(self.counts(), before)
        for values in (
            {"statement": "Synthetic changed conclusion"},
            {"expected_version": 1},
            {"professional_name": "Synthetic different professional"},
        ):
            with self.assertRaises(ValidationError):
                review.record_supplier_advance_review(**{**data, **values})
            self.assertEqual(self.counts(), before)

    def test_source_identity_change_is_stale_and_requires_fresh_report_revision(self):
        advance = self.advance()
        first = self.zero_review(advance)
        old = self.review_data(advance)
        self.supplier.name = "Synthetic supplier renamed by source correction"
        self.supplier.save()
        context = self.context(advance)
        self.assertEqual(context["status"], "stale")
        self.assertNotEqual(
            context["source_fingerprint"], old["decision"]["source_fingerprint"]
        )
        before = self.counts()
        with self.assertRaises(ValidationError):
            review.record_supplier_advance_review(**old)
        self.assertEqual(self.counts(), before)
        revised = self.zero_review(advance)
        self.assertEqual(
            (revised.previous_id, revised.version, self.context(advance)["status"]),
            (first.pk, 2, "reviewed"),
        )
        self.assertEqual(
            [row.pk for row in self.context(advance)["history"]], [first.pk, revised.pk]
        )

    def test_closed_dependency_blocks_revision_but_does_not_invalidate_historical_actor(
        self,
    ):
        advance = self.advance()
        first = self.zero_review(advance)
        Membership.all_objects.filter(organization=self.org, user=self.actor).update(
            is_active=False
        )
        self.assertEqual(self.context(advance)["status"], "reviewed")
        actor = self.user("owner")
        data = self.review_data(advance, actor=actor)
        reviewed_statement(
            self,
            organization=self.org,
            actor=actor,
            account=self.account,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            closing="-1000",
            total_out="1000",
            count=1,
        )
        ledger.close_month(organization=self.org, actor=actor, year=2026, month=1)
        before = self.counts()
        with self.assertRaises(ValidationError):
            review.record_supplier_advance_review(**data)
        self.assertEqual(self.counts(), before)
        first.refresh_from_db()
        self.assertEqual(first.actor_id, self.actor.pk)

    def test_received_pdf_hash_metadata_and_immutable_history_tamper_are_held(self):
        for model, field, value in (
            (TaxEvidence, "content", b"%PDF-damaged"),
            (ExternalTaxReview, "evidence_sha256", ""),
            (ExternalTaxReview, "professional_name", "Changed professional"),
            (SupplierAdvanceTaxReview, "snapshot_hash", "0" * 64),
        ):
            with self.subTest(field=field):
                advance = self.advance("10")
                accepted = self.zero_review(advance)
                old = self.context(advance)
                pk = (
                    accepted.pk
                    if model is SupplierAdvanceTaxReview
                    else (
                        accepted.external_review.evidence_id
                        if model is TaxEvidence
                        else accepted.external_review_id
                    )
                )
                self.tamper(model, pk, field, value)
                before = self.counts()
                context = self.context(advance)
                self.assertEqual(context["status"], "held")
                self.assertTrue(context["blockers"])
                self.assertNotEqual(context["fingerprint"], old["fingerprint"])
                self.assertEqual(self.counts(), before)
                with self.assertRaises(ValidationError):
                    self.zero_review(advance)
                self.assertEqual(
                    SupplierAdvanceTaxReview.objects.filter(advance=advance).count(), 1
                )

    def test_broken_original_cash_proof_is_held_not_silently_empty(self):
        advance = self.advance()
        self.tamper(SupplierAdvance, advance.pk, "source_hash", "0" * 64)
        before = self.counts()
        self.assertEqual(self.context(advance)["status"], "held")
        gates = review.supplier_advance_tax_gates(
            organization=self.org, cutoff=date(2026, 1, 31)
        )
        self.assertEqual(len(gates["inventory"]), 1)
        self.assertTrue(gates["blockers"])
        with self.assertRaises(ValidationError):
            self.zero_review(advance)
        self.assertEqual(SupplierAdvanceTaxReview.objects.count(), 0)
        self.assertEqual(self.counts()[:2], before[:2])

    def test_equal_pdf_content_does_not_allow_evidence_identity_rebinding(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        advance = self.advance()
        accepted = self.zero_review(advance)
        original = accepted.external_review.evidence
        replacement = review.workflow.upload_evidence(
            organization=self.org,
            actor=self.actor,
            file=SimpleUploadedFile(
                "same-content-different-receipt.pdf", bytes(original.content)
            ),
        )
        self.assertEqual(replacement.sha256, original.sha256)
        self.assertNotEqual(replacement.pk, original.pk)
        old = self.context(advance)
        self.tamper(
            ExternalTaxReview,
            accepted.external_review_id,
            "evidence_id",
            replacement.pk,
        )
        before = self.counts()
        context = self.context(advance)
        self.assertEqual(context["status"], "held")
        self.assertNotEqual(context["fingerprint"], old["fingerprint"])
        self.assertEqual(self.counts(), before)
        self.assertEqual(SupplierAdvanceTaxReview.objects.count(), 1)

    def test_ordinary_orm_mutations_are_rejected_and_write_flags_clear(self):
        accepted = self.zero_review(self.advance())
        self.assertNotIn("_tax_write", accepted.__dict__)
        self.assertNotIn("_supplier_advance_review_write", accepted.__dict__)
        for action in (
            lambda: accepted.save(),
            lambda: accepted.delete(),
            lambda: SupplierAdvanceTaxReview.objects.filter(pk=accepted.pk).update(
                version=2
            ),
            lambda: SupplierAdvanceTaxReview.objects.filter(pk=accepted.pk).delete(),
            lambda: SupplierAdvanceTaxReview.objects.bulk_update(
                [accepted], ["version"]
            ),
            lambda: SupplierAdvanceTaxReview.objects.bulk_create([accepted]),
        ):
            with self.assertRaises(ValidationError):
                action()
        fresh = SupplierAdvanceTaxReview(
            organization=self.org, advance=accepted.advance, version=2
        )
        with self.assertRaises(ValidationError):
            fresh.save()
        self.assertNotIn("_tax_write", fresh.__dict__)
        self.assertNotIn("_supplier_advance_review_write", fresh.__dict__)

    def test_unconfirmed_or_foreign_pdf_creates_no_external_report(self):
        advance = self.advance()
        data = self.review_data(advance)
        before = self.counts()
        with self.assertRaises(ValidationError):
            review.record_supplier_advance_review(**{**data, "report_confirmed": False})
        self.assertEqual(self.counts(), before)
        foreign = Organization.objects.create(
            name="Synthetic foreign evidence", is_demo=True
        )
        actor = self.user("owner", foreign)
        from django.core.files.uploadedfile import SimpleUploadedFile

        foreign_pdf = review.workflow.upload_evidence(
            organization=foreign,
            actor=actor,
            file=SimpleUploadedFile(
                "foreign-synthetic.pdf", bytes(data["evidence"].content)
            ),
        )
        before = self.counts()
        with self.assertRaises(ValidationError):
            review.record_supplier_advance_review(**{**data, "evidence": foreign_pdf})
        self.assertEqual(self.counts(), before)

    def test_failure_recording_final_audit_rolls_back_both_typed_decision_and_report(
        self,
    ):
        advance = self.advance()
        data = self.review_data(advance)
        before = self.counts()
        with (
            patch(
                "taxes.supplier_advance_review.record",
                side_effect=ValidationError("Synthetic audit write failure"),
            ),
            self.assertRaises(ValidationError),
        ):
            review.record_supplier_advance_review(**data)
        self.assertEqual(self.counts(), before)
        self.assertEqual(
            (self.context(advance)["status"], self.context(advance)["version"]),
            ("pending", 0),
        )


class SupplierAdvanceTaxReviewHTTPTests(SupplierAdvanceReviewFixtures, TestCase):
    def url(self, advance):
        return reverse("supplier_advance_tax:review", args=[advance.pk])

    def post_data(self, advance):
        data = self.review_data(advance)
        facts = data["decision"]["reviewed_facts"]
        return {
            "evidence": str(data["evidence"].pk),
            "professional_name": data["professional_name"],
            "qualification_reference": data["qualification_reference"],
            "reviewed_on": data["reviewed_on"].isoformat(),
            "statement": data["statement"],
            "report_confirmed": "on",
            "resolution": "no_withholding",
            "gross_amount": "",
            "withholding_amount": "0.00",
            "legal_basis_reference": facts["legal_basis_reference"],
            "professional_instruction": facts["professional_instruction"],
            "source_fingerprint": data["decision"]["source_fingerprint"],
            "expected_version": data["expected_version"],
            "expected_fingerprint": data["expected_fingerprint"],
            "request_key": data["request_key"],
            "operation": "save",
        }

    def test_owner_get_is_pure_save_persists_and_exact_post_retry_creates_nothing(self):
        advance = self.advance()
        data = self.post_data(advance)
        before = self.counts()
        page = self.client.get(self.url(advance))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(self.counts(), before)
        self.assertContains(page, "Peristiwa kas asli")
        self.assertRedirects(
            self.client.post(self.url(advance), data), self.url(advance)
        )
        row = SupplierAdvanceTaxReview.objects.get(advance=advance)
        self.assertEqual(row.decision["reviewed_facts"]["gross_amount"], None)
        page = self.client.get(self.url(advance))
        self.assertContains(page, "Laporan dicocokkan")
        self.assertContains(page, "Versi 1")
        self.assertContains(page, "Belum ditentukan")
        before = self.counts()
        self.assertRedirects(
            self.client.post(self.url(advance), data), self.url(advance)
        )
        self.assertEqual(self.counts(), before)

    def test_finance_prepares_rechecks_but_only_owner_saves_readers_and_marketing_are_denied(
        self,
    ):
        advance = self.advance()
        data = self.post_data(advance)
        before = self.counts()
        finance = self.user("finance")
        self.client.force_login(finance)
        page = self.client.get(self.url(advance))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Periksa kembali")
        self.assertNotContains(page, "Simpan kesimpulan laporan")
        self.assertEqual(
            self.client.post(
                self.url(advance), {**data, "operation": "recheck"}
            ).status_code,
            200,
        )
        self.assertEqual(self.client.post(self.url(advance), data).status_code, 403)
        for role in ("reviewer", "director", "auditor"):
            self.client.force_login(self.user(role))
            self.assertEqual(self.client.get(self.url(advance)).status_code, 200)
            self.assertEqual(self.client.post(self.url(advance), data).status_code, 403)
        self.client.force_login(self.user("marketing"))
        self.assertEqual(self.client.get(self.url(advance)).status_code, 403)
        self.assertEqual(self.counts(), before)

    def test_stale_post_retains_report_then_explicit_recheck_changes_key_and_clears_confirmation(
        self,
    ):
        advance = self.advance()
        data = self.post_data(advance)
        self.supplier.tax_id = "SYN-REGISTRATION-CHANGED"
        self.supplier.save()
        before = self.counts()
        failed = self.client.post(self.url(advance), data)
        self.assertEqual(failed.status_code, 400)
        form = failed.context["form"]
        for field in (
            "statement",
            "legal_basis_reference",
            "professional_instruction",
            "withholding_amount",
            "request_key",
        ):
            self.assertEqual(str(form[field].value()), str(data[field]))
        self.assertEqual(self.counts(), before)
        checked = self.client.post(self.url(advance), {**data, "operation": "recheck"})
        self.assertEqual(checked.status_code, 200)
        form = checked.context["form"]
        self.assertFalse(form["report_confirmed"].value())
        self.assertNotEqual(str(form["request_key"].value()), str(data["request_key"]))
        self.assertNotEqual(
            form["expected_fingerprint"].value(), data["expected_fingerprint"]
        )
        self.assertEqual(form["statement"].value(), data["statement"])
        self.assertEqual(self.counts(), before)
        fresh = {
            **data,
            **{
                field: form[field].value()
                for field in (
                    "request_key",
                    "expected_version",
                    "expected_fingerprint",
                    "source_fingerprint",
                )
            },
        }
        self.assertRedirects(
            self.client.post(self.url(advance), fresh), self.url(advance)
        )
        self.assertEqual(self.context(advance)["status"], "reviewed")

    def test_duplicate_csrf_foreign_scope_and_broken_source_are_safe(self):
        advance = self.advance()
        data = self.post_data(advance)
        before = self.counts()
        duplicate = {**data, "withholding_amount": ["0.00", "1.00"]}
        self.assertEqual(
            self.client.post(self.url(advance), duplicate).status_code, 400
        )
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.actor)
        self.assertEqual(protected.post(self.url(advance), data).status_code, 403)
        foreign = Organization.objects.create(
            name="Synthetic other taxpayer", is_demo=True
        )
        self.client.force_login(self.user("owner", foreign))
        self.assertEqual(self.client.get(self.url(advance)).status_code, 404)
        self.assertEqual(self.client.post(self.url(advance), data).status_code, 404)
        self.client.force_login(self.actor)
        self.tamper(SupplierAdvance, advance.pk, "source_hash", "0" * 64)
        damaged = self.client.get(self.url(advance))
        self.assertEqual(damaged.status_code, 200)
        self.assertContains(damaged, "Ditahan")
        self.assertEqual(self.client.post(self.url(advance), data).status_code, 400)
        self.assertEqual(self.counts(), before)
