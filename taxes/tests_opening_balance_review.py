"""Original cash reviews use real synthetic opening and bank commands.

The root owns execution. Fixtures create no invented invoice, Bill, revenue,
expense, tax obligation or certificate to clear an unknown tax treatment.
"""

from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse

from core.models import AuditEvent, Membership, Organization
from finance import opening_balances as opening
from finance import services as ledger
from finance.models import (
    BankPosting,
    Bill,
    Invoice,
    Journal,
    JournalLine,
    OpeningBalanceItem,
    OpeningBalanceSettlement,
    Party,
)
from finance.test_statement_fixtures import reviewed_statement
from finance.tests_tax_money import TaxMoneyFixtures

from . import opening_balance_review as review
from .models import (
    ExternalTaxReview,
    OpeningSettlementTaxReview,
    TaxEvidence,
    TaxObligation,
)


class OpeningReviewFixtures:
    evidence = TaxMoneyFixtures.evidence
    external = TaxMoneyFixtures.external
    movement = TaxMoneyFixtures.movement

    def setUp(self):
        TaxMoneyFixtures.setUp(self)
        self.client.force_login(self.actor)
        self.cutoff = date(2026, 9, 1)

    def item(self, amount="1000", kind="payable", party=None, source_reference=None):
        values = dict(
            kind=kind,
            party=party or (self.supplier if kind == "payable" else self.party),
            date=self.cutoff,
            source_date=self.cutoff - timedelta(days=12),
            source_reference=source_reference or f"SYN-OLD-{uuid4()}",
            amount=amount,
        )
        context = opening.opening_balance_action_context(
            organization=self.org, action="record", **values
        )
        return opening.record_opening_balance(
            organization=self.org,
            actor=self.actor,
            **values,
            reason="Synthetic verified principal outstanding at the cutoff.",
            evidence="Synthetic original document and verified outstanding balance.",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )

    def cash(self, item=None, amount="100", kind="payable", day=None):
        item = item or self.item(kind=kind)
        bank = self.movement(
            amount if item.kind == "receivable" else f"-{amount}",
            day=day or self.day,
            reference=f"SYN-OPENING-CASH-{uuid4()}",
        )
        context = opening.opening_balance_action_context(
            organization=self.org,
            action="settle",
            item=item,
            transaction=bank,
            amount=amount,
        )
        return opening.settle_opening_balance(
            organization=self.org,
            actor=self.actor,
            item=item,
            transaction=bank,
            amount=amount,
            reason="Synthetic actual principal cash matched.",
            evidence="Synthetic bank statement and original balance document.",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )

    def context(self, settlement):
        return review.opening_settlement_review_context(
            organization=self.org, settlement=settlement
        )

    def review_data(self, settlement, **changes):
        context = self.context(settlement)
        decision = {
            "schema": review.SCHEMA,
            "version": 1,
            "scope": "original_cash_event",
            "source_fingerprint": context["source_fingerprint"],
            "resolution": "no_withholding",
            "reviewed_facts": {
                "cash_amount": format(settlement.amount, ".2f"),
                "payment_date": settlement.date.isoformat(),
                "contract_reference": settlement.item.source_reference,
                "gross_amount": None,
                "withholding_amount": "0.00",
                "legal_basis_reference": "Synthetic professional report paragraph 4",
                "professional_instruction": "Synthetic exact original cash conclusion; gross remains unspecified.",
            },
        }
        data = dict(
            organization=self.org,
            actor=self.actor,
            settlement=settlement,
            decision=decision,
            evidence=self.evidence(),
            professional_name="Synthetic professional",
            qualification_reference="Synthetic documented assignment",
            reviewed_on=date(2026, 9, 12),
            statement="Synthetic received report concludes no withholding for this exact original cash event.",
            report_confirmed=True,
            expected_version=context["version"],
            expected_fingerprint=context["fingerprint"],
            request_key=str(uuid4()),
        )
        data.update(changes)
        return data

    def zero_review(self, settlement):
        return review.record_opening_settlement_review(**self.review_data(settlement))

    def counts(self):
        return tuple(
            model.objects.count()
            for model in (
                OpeningSettlementTaxReview,
                ExternalTaxReview,
                AuditEvent,
                Journal,
                BankPosting,
                TaxObligation,
                Invoice,
                Bill,
            )
        )

    def user(self, role, organization=None):
        actor = get_user_model().objects.create_user(
            username=f"opening-review-{role}-{uuid4()}"
        )
        Membership.objects.create(
            organization=organization or self.org, user=actor, role=role
        )
        return actor

    def tamper(self, model, pk, field, value):
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {quote(model._meta.db_table)} SET {quote(field)} = %s WHERE id = %s",
                [value, pk],
            )


class OpeningCashReviewTests(OpeningReviewFixtures, TestCase):
    def test_actual_ap_cash_pending_then_received_zero_report_changes_no_finance(self):
        settlement = self.cash()
        before = (
            ledger.report_balances(organization=self.org),
            Journal.objects.count(),
            BankPosting.objects.count(),
        )
        context = self.context(settlement)
        self.assertEqual(
            (context["status"], context["version"], context["history"]),
            ("pending", 0, []),
        )
        self.assertTrue(context["blockers"])
        reads = self.counts()
        self.assertEqual(
            self.context(settlement)["fingerprint"], context["fingerprint"]
        )
        self.assertEqual(self.counts(), reads)
        accepted = self.zero_review(settlement)
        self.assertEqual(
            (self.context(settlement)["status"], self.context(settlement)["blockers"]),
            ("reviewed", []),
        )
        self.assertIsNone(accepted.decision["reviewed_facts"]["gross_amount"])
        self.assertEqual(
            accepted.external_review.subject_type, "openingbalancesettlement"
        )
        self.assertEqual(accepted.external_review.subject_id, settlement.pk)
        self.assertEqual(
            before,
            (
                ledger.report_balances(organization=self.org),
                Journal.objects.count(),
                BankPosting.objects.count(),
            ),
        )
        self.assertFalse(TaxObligation.objects.exists())
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(Bill.objects.exists())

    def test_customer_and_reseller_ar_are_reviewable_without_invoice(self):
        for kind in ("customer", "reseller"):
            party = Party.objects.create(
                organization=self.org, name=f"Synthetic {kind}", kind=kind
            )
            settlement = self.cash(self.item(kind="receivable", party=party))
            self.zero_review(settlement)
            self.assertEqual(self.context(settlement)["status"], "reviewed")
        self.assertFalse(Invoice.objects.exists())

    def test_zero_residual_keeps_cash_tax_task_and_later_cash_does_not_rewrite_first_review(
        self,
    ):
        item = self.item("300")
        first = self.cash(item, amount="100")
        self.zero_review(first)
        prior = self.context(first)
        second = self.cash(item, amount="200", day=date(2026, 9, 11))
        self.assertEqual(
            opening.opening_balance_position(organization=self.org, item=item)[
                "remaining"
            ],
            0,
        )
        gates = review.opening_balance_tax_gates(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertEqual(
            [row["settlement_id"] for row in gates["inventory"]], [first.pk, second.pk]
        )
        self.assertEqual(
            [row["status"] for row in gates["inventory"]], ["reviewed", "pending"]
        )
        self.assertTrue(gates["blockers"])
        self.assertEqual(self.context(first)["fingerprint"], prior["fingerprint"])
        self.zero_review(second)
        self.assertFalse(
            review.opening_balance_tax_gates(
                organization=self.org, cutoff=date(2026, 9, 30)
            )["blockers"]
        )

    def test_positive_unknown_or_unresolved_professional_facts_never_become_assumed_zero(
        self,
    ):
        for resolution, amount in (
            ("no_withholding", None),
            ("no_withholding", "1.00"),
            ("more_evidence", "0.00"),
            ("unsupported_treatment", "0.00"),
        ):
            with self.subTest(resolution=resolution, amount=amount):
                settlement = self.cash()
                data = self.review_data(settlement)
                data["decision"]["resolution"] = resolution
                data["decision"]["reviewed_facts"]["withholding_amount"] = amount
                review.record_opening_settlement_review(**data)
                self.assertEqual(self.context(settlement)["status"], "held")
                self.assertTrue(self.context(settlement)["blockers"])
        self.assertFalse(TaxObligation.objects.exists())
        self.assertEqual(
            ledger.report_balances(organization=self.org)["TAX_PAYABLE"], 0
        )

    def test_exact_fact_schema_original_cash_and_no_inferred_gross_are_strict(self):
        settlement = self.cash()
        data = self.review_data(settlement)
        mutations = [
            lambda value: value.update(version=True),
            lambda value: value.update(extra="ignored"),
            lambda value: value.update(scope="old_invoice"),
            lambda value: value["reviewed_facts"].update(cash_amount="999.00"),
            lambda value: value["reviewed_facts"].update(payment_date="2026-08-20"),
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
            decision = deepcopy(data["decision"])
            mutate(decision)
            with self.assertRaises(ValidationError):
                review.record_opening_settlement_review(
                    **{**data, "decision": decision}
                )
            self.assertEqual(self.counts(), before)

    def test_owner_scope_revocation_and_immutable_review_receipt_are_enforced(self):
        settlement = self.cash()
        data = self.review_data(settlement)
        before = self.counts()
        for role in ("finance", "reviewer", "auditor", "director", "marketing"):
            with self.subTest(role=role), self.assertRaises(PermissionDenied):
                review.record_opening_settlement_review(
                    **{**data, "actor": self.user(role)}
                )
        foreign = Organization.objects.create(name="Synthetic foreign opening review")
        outsider = self.user("owner", foreign)
        with self.assertRaises(ValidationError):
            review.record_opening_settlement_review(
                **{**data, "organization": foreign, "actor": outsider}
            )
        self.assertEqual(self.counts(), before)
        accepted = review.record_opening_settlement_review(**data)
        for action in (
            lambda: accepted.save(),
            lambda: accepted.delete(),
            lambda: OpeningSettlementTaxReview.objects.filter(pk=accepted.pk).update(
                version=2
            ),
            lambda: OpeningSettlementTaxReview.objects.filter(pk=accepted.pk).delete(),
            lambda: OpeningSettlementTaxReview.objects.bulk_update(
                [accepted], ["version"]
            ),
            lambda: OpeningSettlementTaxReview.objects.bulk_create([accepted]),
        ):
            with self.assertRaises(ValidationError):
                action()
        self.assertNotIn("_tax_write", accepted.__dict__)
        self.assertNotIn("_opening_balance_review_write", accepted.__dict__)
        Membership.all_objects.filter(organization=self.org, user=self.actor).update(
            is_active=False
        )
        with self.assertRaises(PermissionDenied):
            review.record_opening_settlement_review(**data)
        self.assertEqual(self.context(settlement)["status"], "reviewed")

    def test_exact_retry_precedes_closed_correction_boundary_and_changed_payload_conflicts(
        self,
    ):
        settlement = self.cash()
        data = self.review_data(settlement)
        accepted = review.record_opening_settlement_review(**data)
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.actor,
            account=self.account,
            start=date(2026, 9, 1),
            end=date(2026, 9, 30),
            closing="-100",
            total_out="100",
            count=1,
        )
        ledger.close_month(organization=self.org, actor=self.actor, year=2026, month=9)
        # Preparing a new received PDF has its own legitimate upload audit.
        # Keep all fixture preparation outside every command's no-write proof.
        revision_data = self.review_data(settlement)
        before = self.counts()
        self.assertEqual(
            review.record_opening_settlement_review(**data).pk, accepted.pk
        )
        self.assertEqual(self.counts(), before)
        with self.assertRaises(ValidationError):
            review.record_opening_settlement_review(
                **{**data, "statement": "Synthetic changed conclusion"}
            )
        with self.assertRaises(ValidationError):
            review.record_opening_settlement_review(**revision_data)
        self.assertEqual(self.counts(), before)

    def test_changed_party_is_stale_and_new_received_report_appends_history(self):
        settlement = self.cash()
        first = self.zero_review(settlement)
        old = self.review_data(settlement)
        self.supplier.tax_id = "SYN-CHANGED-LEGAL-ID"
        self.supplier.save()
        context = self.context(settlement)
        self.assertEqual(context["status"], "stale")
        before = self.counts()
        with self.assertRaises(ValidationError):
            review.record_opening_settlement_review(**old)
        self.assertEqual(self.counts(), before)
        second = self.zero_review(settlement)
        self.assertEqual(
            (second.previous_id, second.version, self.context(settlement)["status"]),
            (first.pk, 2, "reviewed"),
        )

    def test_original_journal_damage_is_held_and_each_damaged_graph_edit_changes_token(
        self,
    ):
        settlement = self.cash()
        prior = self.context(settlement)
        line = JournalLine.objects.get(journal_id=settlement.journal_id, account="AP")
        self.tamper(JournalLine, line.pk, "account", "EXPENSE")
        first = self.context(settlement)
        self.assertEqual(first["status"], "held")
        self.assertNotEqual(first["fingerprint"], prior["fingerprint"])
        self.tamper(JournalLine, line.pk, "account", "PREPAID")
        second = self.context(settlement)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.zero_review(settlement)
        self.assertEqual(self.counts()[:2], before[:2])

    def test_report_pdf_metadata_and_history_damage_hold_and_invalidate_each_token(
        self,
    ):
        for model, field, value in (
            (TaxEvidence, "content", b"%PDF-damaged"),
            (ExternalTaxReview, "evidence_sha256", ""),
            (ExternalTaxReview, "professional_name", "Synthetic changed professional"),
            (OpeningSettlementTaxReview, "snapshot_hash", "0" * 64),
        ):
            with self.subTest(field=field):
                settlement = self.cash()
                accepted = self.zero_review(settlement)
                prior = self.context(settlement)
                pk = (
                    accepted.pk
                    if model is OpeningSettlementTaxReview
                    else (
                        accepted.external_review.evidence_id
                        if model is TaxEvidence
                        else accepted.external_review_id
                    )
                )
                self.tamper(model, pk, field, value)
                current = self.context(settlement)
                self.assertEqual(current["status"], "held")
                self.assertNotEqual(current["fingerprint"], prior["fingerprint"])
                before = self.counts()
                with self.assertRaises(ValidationError):
                    self.zero_review(settlement)
                self.assertEqual(self.counts()[:2], before[:2])

    def test_tax_queue_retains_bank_anchored_event_if_its_stored_date_is_changed(self):
        settlement = self.cash()
        self.tamper(OpeningBalanceSettlement, settlement.pk, "date", "2027-01-01")
        gates = review.opening_balance_tax_gates(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertEqual(
            [row["settlement_id"] for row in gates["inventory"]], [settlement.pk]
        )
        self.assertEqual(gates["inventory"][0]["status"], "held")
        self.assertTrue(gates["blockers"])

    def test_final_audit_failure_rolls_back_received_report_and_typed_decision(self):
        settlement = self.cash()
        data = self.review_data(settlement)
        before = self.counts()
        with (
            patch(
                "taxes.opening_balance_review.record",
                side_effect=ValidationError("Synthetic audit failure"),
            ),
            self.assertRaises(ValidationError),
        ):
            review.record_opening_settlement_review(**data)
        self.assertEqual(self.counts(), before)
        self.assertEqual(
            (self.context(settlement)["status"], self.context(settlement)["version"]),
            ("pending", 0),
        )

    def test_unconfirmed_future_or_pre_event_report_records_no_authority(self):
        settlement = self.cash()
        data = self.review_data(settlement)
        before = self.counts()
        for changes in (
            {"report_confirmed": False},
            {"reviewed_on": date(2099, 1, 1)},
            {"reviewed_on": settlement.date - timedelta(days=1)},
        ):
            with self.assertRaises(ValidationError):
                review.record_opening_settlement_review(**{**data, **changes})
            self.assertEqual(self.counts(), before)

    def test_equal_pdf_bytes_do_not_authorize_rebinding_to_another_received_document(
        self,
    ):
        from django.core.files.uploadedfile import SimpleUploadedFile

        settlement = self.cash()
        accepted = self.zero_review(settlement)
        original = accepted.external_review.evidence
        replacement = review.workflow.upload_evidence(
            organization=self.org,
            actor=self.actor,
            file=SimpleUploadedFile(
                "same-bytes-different-receipt.pdf", bytes(original.content)
            ),
        )
        self.assertEqual(replacement.sha256, original.sha256)
        prior = self.context(settlement)
        self.tamper(
            ExternalTaxReview,
            accepted.external_review_id,
            "evidence_id",
            replacement.pk,
        )
        current = self.context(settlement)
        self.assertEqual(current["status"], "held")
        self.assertNotEqual(current["fingerprint"], prior["fingerprint"])
        with self.assertRaises(ValidationError):
            self.zero_review(settlement)

    def test_foreign_received_pdf_cannot_be_used_for_this_company(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        settlement = self.cash()
        data = self.review_data(settlement)
        foreign = Organization.objects.create(name="Synthetic foreign evidence company")
        actor = self.user("owner", foreign)
        evidence = review.workflow.upload_evidence(
            organization=foreign,
            actor=actor,
            file=SimpleUploadedFile(
                "foreign-cash-review.pdf", bytes(data["evidence"].content)
            ),
        )
        before = self.counts()
        with self.assertRaises(ValidationError):
            review.record_opening_settlement_review(**{**data, "evidence": evidence})
        self.assertEqual(self.counts(), before)


class OpeningCashReviewHTTPTests(OpeningReviewFixtures, TestCase):
    def url(self, settlement):
        return reverse("opening_balance_tax:review", args=[settlement.pk])

    def post_data(self, settlement):
        data = self.review_data(settlement)
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

    def test_owner_pure_get_human_form_save_and_exact_http_retry(self):
        settlement = self.cash()
        data = self.post_data(settlement)
        before = self.counts()
        response = self.client.get(self.url(settlement))
        self.assertContains(response, "Peristiwa kas asli")
        self.assertNotContains(response, 'textarea name="decision"')
        self.assertEqual(self.counts(), before)
        self.assertRedirects(
            self.client.post(self.url(settlement), data), self.url(settlement)
        )
        accepted = OpeningSettlementTaxReview.objects.get(settlement=settlement)
        self.assertIsNone(accepted.decision["reviewed_facts"]["gross_amount"])
        response = self.client.get(self.url(settlement))
        self.assertContains(response, "Laporan dicocokkan")
        self.assertContains(response, "Belum ditentukan")
        before = self.counts()
        self.assertRedirects(
            self.client.post(self.url(settlement), data), self.url(settlement)
        )
        self.assertEqual(self.counts(), before)

    def test_finance_rechecks_readers_read_only_and_marketing_denied(self):
        settlement = self.cash()
        data = self.post_data(settlement)
        before = self.counts()
        self.client.force_login(self.user("finance"))
        self.assertContains(self.client.get(self.url(settlement)), "Periksa kembali")
        self.assertNotContains(
            self.client.get(self.url(settlement)), "Simpan kesimpulan laporan"
        )
        self.assertEqual(
            self.client.post(
                self.url(settlement), {**data, "operation": "recheck"}
            ).status_code,
            200,
        )
        self.assertEqual(self.client.post(self.url(settlement), data).status_code, 403)
        for role in ("reviewer", "director", "auditor"):
            self.client.force_login(self.user(role))
            self.assertEqual(self.client.get(self.url(settlement)).status_code, 200)
            self.assertEqual(
                self.client.post(self.url(settlement), data).status_code, 403
            )
        self.client.force_login(self.user("marketing"))
        self.assertEqual(self.client.get(self.url(settlement)).status_code, 403)
        self.assertEqual(self.counts(), before)

    def test_stale_form_retains_human_facts_then_recheck_rebinds_and_unchecks(self):
        settlement = self.cash()
        data = self.post_data(settlement)
        self.supplier.name = "Synthetic corrected supplier legal name"
        self.supplier.save()
        before = self.counts()
        failed = self.client.post(self.url(settlement), data)
        self.assertEqual(failed.status_code, 400)
        for field in (
            "statement",
            "legal_basis_reference",
            "professional_instruction",
            "request_key",
        ):
            self.assertEqual(
                str(failed.context["form"][field].value()), str(data[field])
            )
        checked = self.client.post(
            self.url(settlement), {**data, "operation": "recheck"}
        )
        form = checked.context["form"]
        self.assertFalse(form["report_confirmed"].value())
        self.assertNotEqual(str(form["request_key"].value()), data["request_key"])
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
            self.client.post(self.url(settlement), fresh), self.url(settlement)
        )

    def test_duplicate_csrf_foreign_scope_and_damaged_foreign_party_do_not_leak(self):
        settlement = self.cash()
        data = self.post_data(settlement)
        before = self.counts()
        self.assertEqual(
            self.client.post(
                self.url(settlement), {**data, "withholding_amount": ["0.00", "1.00"]}
            ).status_code,
            400,
        )
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.actor)
        self.assertEqual(protected.post(self.url(settlement), data).status_code, 403)
        foreign = Organization.objects.create(name="Synthetic foreign cash review")
        outsider = self.user("owner", foreign)
        private = Party.objects.create(
            organization=foreign,
            name="Foreign private supplier must stay hidden",
            kind="supplier",
        )
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.url(settlement)).status_code, 404)
        self.client.force_login(self.actor)
        self.tamper(OpeningBalanceItem, settlement.item_id, "party_id", private.pk)
        page = self.client.get(self.url(settlement))
        self.assertContains(page, "Ditahan")
        self.assertNotContains(page, private.name)
        self.assertEqual(self.client.post(self.url(settlement), data).status_code, 400)
        self.assertEqual(self.counts(), before)
