"""Public fiscal recovery guards preserve received original cash authority."""

from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase

from finance import opening_balances as opening
from finance import services as ledger

from . import opening_balance_review_dependencies as dependencies
from .tests_opening_balance_review import OpeningReviewFixtures


class OpeningCashRecoveryTests(OpeningReviewFixtures, TestCase):
    def test_malformed_original_bank_snapshot_blocks_cancellation_as_validation_error(
        self,
    ):
        from django.core.exceptions import ValidationError

        cash = self.cash(kind="receivable")
        before = self.counts(), ledger.report_balances(organization=self.org)
        with (
            patch(
                "finance.opening_balances.opening_settlement_source_snapshot",
                return_value={"bank": None},
            ),
            self.assertRaises(ValidationError),
        ):
            dependencies.opening_cash_recovery_blockers(
                organization=self.org,
                settlement=cash,
                date=cash.date,
                action="cancel",
            )
        self.assertEqual(
            (self.counts(), ledger.report_balances(organization=self.org)), before
        )

    def test_received_zero_report_blocks_cancellation_without_downstream_use_and_pending_does_not(
        self,
    ):
        reviewed = self.cash(kind="receivable")
        authority = self.zero_review(reviewed)
        pending = self.cash(kind="receivable")
        original = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=reviewed
        )
        saved = deepcopy(
            (
                authority.source_snapshot,
                authority.decision,
                authority.report_snapshot,
                authority.snapshot_hash,
            )
        )
        report_id = authority.external_review_id
        report_pdf = bytes(authority.external_review.evidence.content)
        before_counts, before_balances = (
            self.counts(),
            ledger.report_balances(organization=self.org),
        )
        self.assertEqual(self.context(reviewed)["status"], "reviewed")
        self.assertEqual(self.context(pending)["status"], "pending")
        self.assertEqual(
            authority.decision["reviewed_facts"]["withholding_amount"], "0.00"
        )

        # Both outcomes are checked through the same public guard. An intact
        # pending review is distinct from received authority with explicit zero.
        self.assertEqual(
            dependencies.opening_cash_recovery_blockers(
                organization=self.org,
                settlement=pending,
                date=pending.date,
                action="cancel",
            ),
            [],
        )
        blockers = dependencies.opening_cash_recovery_blockers(
            organization=self.org,
            settlement=reviewed,
            date=reviewed.date,
            action="cancel",
        )
        self.assertTrue(blockers)

        self.assertEqual(self.counts(), before_counts)
        self.assertEqual(ledger.report_balances(organization=self.org), before_balances)
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=reviewed
            ),
            original,
        )
        self.assertEqual(self.context(reviewed)["status"], "reviewed")
        authority.refresh_from_db()
        authority.external_review.evidence.refresh_from_db()
        self.assertEqual(
            (
                authority.source_snapshot,
                authority.decision,
                authority.report_snapshot,
                authority.snapshot_hash,
            ),
            saved,
        )
        self.assertEqual(authority.external_review_id, report_id)
        self.assertEqual(bytes(authority.external_review.evidence.content), report_pdf)

    def test_canceled_rematched_receipt_preserves_lineage_requires_exclusion_and_allocates_real_cash_once(
        self,
    ):
        from uuid import uuid4

        from django.core.exceptions import ValidationError

        from finance.models import BankTransaction, Bill, Invoice

        from . import tests_monthly_packages as package_fixtures
        from .models import TaxObligation
        from .monthly_source_workflow import (
            approve_monthly_package,
            monthly_decision,
            package_status,
            prepare_monthly_package,
        )

        package_fixtures.MonthlySourcePackageTests.profile(self)
        first_item = self.item("300", kind="receivable")
        second_item = self.item("200", kind="receivable")
        original = self.cash(first_item, amount="100")
        bank = original.bank_posting.transaction
        original_proof = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=original
        )
        before = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        before_snapshot = deepcopy(before.snapshot)
        preview = opening.opening_balance_action_context(
            organization=self.org, action="cancel", settlement=original
        )
        self.assertEqual(preview["blockers"], [])
        cancellation = opening.cancel_opening_settlement(
            organization=self.org,
            actor=self.actor,
            settlement=original,
            reason="Synthetic original bank receipt was assigned to the wrong old document.",
            evidence="Synthetic bank receipt and documented reconciliation identify the correct old document.",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        preview = opening.opening_balance_action_context(
            organization=self.org,
            action="settle",
            item=second_item,
            transaction=bank,
            amount="100",
        )
        self.assertEqual(preview["blockers"], [])
        rematched = opening.settle_opening_balance(
            organization=self.org,
            actor=self.actor,
            item=second_item,
            transaction=bank,
            amount="100",
            reason="Synthetic same original bank receipt belongs to this second old document.",
            evidence="Synthetic original bank receipt and verified document identity.",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=original
            ),
            original_proof,
        )
        self.assertEqual(
            BankTransaction.objects.filter(organization=self.org).count(), 1
        )
        bank.refresh_from_db()
        self.assertEqual(bank.amount, 100)
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["AR"],
                balances["BANK"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (400, 100, 0, 0),
        )

        pending = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        receipts = {
            row["source_id"]: row
            for row in pending.snapshot["rows"]
            if row["source_kind"] == "opening-receipt"
        }
        self.assertEqual(set(receipts), {original.pk, rematched.pk})
        canceled_row, active_row = receipts[original.pk], receipts[rematched.pk]
        cancellation_proof = opening.opening_cancellation_source_snapshot(
            organization=self.org, cancellation=cancellation
        )
        self.assertEqual(canceled_row["opening_cancellation"], cancellation_proof)
        self.assertIsNone(active_row.get("opening_cancellation"))
        lineage = {"kind": "bank_transaction", "transaction_id": bank.pk}
        self.assertEqual(
            (canceled_row["cash_lineage"], active_row["cash_lineage"]),
            (lineage, lineage),
        )
        self.assertEqual(canceled_row["source_proof"], original_proof)
        self.assertFalse(canceled_row["proposed_inclusion"])
        self.assertEqual(
            (
                canceled_row["amount"],
                active_row["amount"],
                canceled_row["invoice_id"],
                active_row["invoice_id"],
            ),
            ("100.00", "100.00", None, None),
        )

        values = {
            "completeness_confirmed": True,
            "inventory_note": "Synthetic all sources reviewed; the original wrong assignment and replacement share one bank receipt.",
            "nil_confirmed": True,
            "rule_reference": "Synthetic received classification of the corrected bank lineage",
        }
        excluded = [
            {
                "row_id": row["row_id"],
                "decision": "exclude" if row["source_id"] == original.pk else "include",
                "reason": "Synthetic received report excludes the canceled assignment and classifies only the correct receipt.",
            }
            for row in (canceled_row, active_row)
        ]
        both_included = [{**row, "decision": "include"} for row in excluded]
        with self.assertRaises(ValidationError):
            monthly_decision(pending, rows=both_included, **values)
        evidence = self.evidence()
        for kind in ("timing", "gross_basis", "correction"):
            rows = excluded if kind == "timing" else both_included
            with self.assertRaises(ValidationError):
                monthly_decision(
                    pending,
                    rows=rows,
                    adjustments=[
                        {
                            "kind": kind,
                            "amount": "100",
                            "reason": "Synthetic attempted allocation on the canceled wrong assignment must remain rejected.",
                            "evidence_id": evidence.pk,
                            "source_row_ids": [canceled_row["row_id"]],
                        }
                    ],
                    **values,
                )

        canceled_authority = self.zero_review(original)
        active_authority = self.zero_review(rematched)
        self.assertEqual(
            canceled_authority.source_snapshot["opening_cancellation"],
            cancellation_proof,
        )
        self.assertNotIn("opening_cancellation", active_authority.source_snapshot)
        self.assertNotEqual(
            canceled_authority.external_review_id, active_authority.external_review_id
        )
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        decision = monthly_decision(package, rows=excluded, **values)
        approval = approve_monthly_package(
            organization=self.org,
            actor=self.actor,
            package=package,
            decision=decision,
            external_review=self.external(package, decision),
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        self.assertEqual(approval.result["reviewed_bridge_amount"], "100.00")
        self.assertEqual(
            approval.result["allocations"],
            [
                {
                    "kind": "opening_cash",
                    "invoice_id": None,
                    "source_row_ids": [f"opening-receipt:{rematched.pk}"],
                    "amount": "100.00",
                }
            ],
        )
        self.assertIsNone(approval.result["taxable_turnover"])
        self.assertIsNone(approval.result["final_tax"])
        self.assertEqual(
            package_status(organization=self.org, package=package)["status"], "approved"
        )
        self.assertEqual(ledger.report_balances(organization=self.org), balances)
        self.assertFalse(Invoice.objects.filter(organization=self.org).exists())
        self.assertFalse(Bill.objects.filter(organization=self.org).exists())
        self.assertFalse(TaxObligation.objects.filter(organization=self.org).exists())
        self.assertNotEqual(package.source_hash, before.source_hash)
        before.refresh_from_db()
        self.assertEqual(before.snapshot, before_snapshot)
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=original
            ),
            original_proof,
        )

    def cancel(self, settlement):
        from uuid import uuid4

        preview = opening.opening_balance_action_context(
            organization=self.org, action="cancel", settlement=settlement
        )
        return opening.cancel_opening_settlement(
            organization=self.org,
            actor=self.actor,
            settlement=settlement,
            reason="Synthetic received reconciliation proves the original assignment was wrong.",
            evidence="Synthetic original bank record and independently verified document relationship.",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )

    def match(self, item, bank, amount):
        from uuid import uuid4

        preview = opening.opening_balance_action_context(
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
            reason="Synthetic verified part of the same actual bank receipt.",
            evidence="Synthetic original statement and independently matched outstanding document.",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )

    def test_active_split_claims_share_recovered_bank_and_keep_received_active_report_identity(
        self,
    ):
        from . import tests_monthly_packages as fixtures
        from .monthly_source_workflow import (
            approve_monthly_package,
            monthly_decision,
            prepare_monthly_package,
        )

        fixtures.MonthlySourcePackageTests.profile(self)
        wrong, first, second = [self.item("200", kind="receivable") for _ in range(3)]
        bank = self.movement("200", reference="SYN-SPLIT-RECOVERY")
        original = self.match(wrong, bank, "100")
        active_first = self.match(first, bank, "100")
        received = self.zero_review(active_first)
        saved = deepcopy(received.source_snapshot)
        self.cancel(original)
        active_second = self.match(second, bank, "100")
        self.assertEqual(self.context(active_first)["status"], "reviewed")
        received.refresh_from_db()
        self.assertEqual(received.source_snapshot, saved)
        self.zero_review(original)
        self.zero_review(active_second)
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        sources = package.snapshot["rows"]
        self.assertEqual(len(sources), 3)
        self.assertTrue(
            all(row["cash_lineage"]["transaction_id"] == bank.pk for row in sources)
        )
        rows = [
            {
                "row_id": row["row_id"],
                "decision": "exclude" if row["source_id"] == original.pk else "include",
                "reason": "Synthetic received report excludes the wrong assignment and includes the two distinct active claims.",
            }
            for row in sources
        ]
        decision = monthly_decision(
            package,
            rows=rows,
            completeness_confirmed=True,
            inventory_note="Synthetic all sources reviewed; two active claims each receive exactly half of one actual bank deposit.",
            rule_reference="Synthetic verified split claim classification",
            nil_confirmed=True,
        )
        approval = approve_monthly_package(
            organization=self.org,
            actor=self.actor,
            package=package,
            decision=decision,
            external_review=self.external(package, decision),
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        self.assertEqual(approval.result["reviewed_bridge_amount"], "200.00")
        self.assertEqual(
            {tuple(row["source_row_ids"]) for row in approval.result["allocations"]},
            {
                (f"opening-receipt:{active_first.pk}",),
                (f"opening-receipt:{active_second.pk}",),
            },
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["AR"],
                balances["BANK"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (400, 200, 0, 0),
        )

    def test_foreign_cancellation_with_no_local_item_keeps_original_bank_and_audit_repairs_in_fiscal_tokens(
        self,
    ):
        import json
        from datetime import date

        from core.models import AuditEvent, Membership, Organization
        from finance.models import (
            BankAccount,
            BankPosting,
            BankTransaction,
            Journal,
            JournalLine,
            OpeningBalanceCancellation,
            OpeningBalanceItem,
            OpeningBalanceSettlement,
        )

        from . import workflow
        from .monthly_sources import build_monthly_sources

        item = self.item("300", kind="receivable")
        cash = self.cash(item, amount="100")
        cancellation = self.cancel(cash)
        bank, posting = cash.bank_posting.transaction, cash.bank_posting
        other = Organization.objects.create(
            name="Synthetic foreign recovery corruption"
        )
        Membership.objects.create(organization=other, user=self.actor, role="owner")
        journals = (item.journal, cash.journal, cancellation.journal)
        for model, pk in (
            (OpeningBalanceItem, item.pk),
            (OpeningBalanceSettlement, cash.pk),
            (OpeningBalanceCancellation, cancellation.pk),
            (BankPosting, posting.pk),
            (BankTransaction, bank.pk),
            (BankAccount, bank.account_id),
        ):
            self.tamper(model, pk, "organization_id", other.pk)
        for journal in journals:
            self.tamper(Journal, journal.pk, "organization_id", other.pk)
            for line in JournalLine.objects.filter(journal_id=journal.pk):
                self.tamper(JournalLine, line.pk, "organization_id", other.pk)
        for row in (item, cash, cancellation):
            moved = deepcopy(row.source_snapshot)
            moved["organization_id"] = other.pk
            self.tamper(type(row), row.pk, "source_snapshot", json.dumps(moved))
        self.assertFalse(
            OpeningBalanceItem.objects.filter(organization=self.org).exists()
        )
        self.assertFalse(
            OpeningBalanceSettlement.objects.filter(organization=self.org).exists()
        )
        self.assertFalse(
            OpeningBalanceCancellation.objects.filter(organization=self.org).exists()
        )
        month = build_monthly_sources(self.org, "2026-09")
        annual = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        audit = AuditEvent.objects.get(
            organization=self.org,
            action="finance.opening_balance.cancel",
            object_type="OpeningBalanceCancellation",
            object_id=str(cancellation.pk),
        )
        repairs = [
            (
                BankTransaction,
                bank.pk,
                "description",
                "Synthetic corrected original bank annotation.",
            )
        ]
        detail = deepcopy(audit.detail)
        detail["repair_note"] = (
            "Synthetic changed original cancellation audit annotation."
        )
        repairs.append((AuditEvent, audit.pk, "detail", json.dumps(detail)))
        for model, pk, field, value in repairs:
            self.assertTrue(
                any(
                    row["code"] == "opening_balance_integrity" and row["hard_blocker"]
                    for row in month["issues"]
                )
            )
            self.assertTrue(annual["opening_principal_sources"]["blockers"])
            self.assertEqual(month["rows"], [])
            self.assertIsNone(month["taxable_turnover"])
            previous_month, previous_annual = month, annual
            self.tamper(model, pk, field, value)
            month = build_monthly_sources(self.org, "2026-09")
            annual = workflow.annual_tax_snapshot(
                self.org, date(2026, 1, 1), date(2026, 12, 31)
            )
            self.assertNotEqual(month["source_hash"], previous_month["source_hash"])
            self.assertNotEqual(
                annual["opening_principal_sources"]["fingerprint"],
                previous_annual["opening_principal_sources"]["fingerprint"],
            )
        self.assertTrue(annual["opening_principal_sources"]["blockers"])

    def test_orphan_cancellation_journal_without_item_is_held_and_journal_and_audit_repairs_change_tokens(
        self,
    ):
        import json
        from datetime import date
        from uuid import uuid4

        from core.models import AuditEvent
        from finance.models import Journal, OpeningBalanceItem

        from . import workflow
        from .monthly_sources import build_monthly_sources

        journal = ledger.post_journal(
            organization=self.org,
            actor=self.actor,
            date=self.day,
            source_key=f"opening:opening-cancel:{uuid4()}",
            description="Synthetic cancellation orphan without source decision.",
            entries=[("AR", "10", "0"), ("BANK", "0", "10")],
        )
        self.assertFalse(
            OpeningBalanceItem.objects.filter(organization=self.org).exists()
        )
        month = build_monthly_sources(self.org, "2026-09")
        annual = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        audit = AuditEvent.objects.get(
            organization=self.org,
            action="finance.journal.posted",
            object_type="Journal",
            object_id=str(journal.pk),
        )
        for model, pk, field, value in (
            (
                Journal,
                journal.pk,
                "description",
                "Synthetic repaired orphan journal annotation.",
            ),
            (
                AuditEvent,
                audit.pk,
                "detail",
                json.dumps(
                    {"repair_note": "Synthetic updated journal audit evidence."}
                ),
            ),
        ):
            self.assertTrue(
                any(
                    row["code"] == "opening_balance_integrity" and row["hard_blocker"]
                    for row in month["issues"]
                )
            )
            self.assertTrue(annual["opening_principal_sources"]["blockers"])
            self.assertEqual(month["rows"], [])
            previous_month, previous_annual = month, annual
            self.tamper(model, pk, field, value)
            month = build_monthly_sources(self.org, "2026-09")
            annual = workflow.annual_tax_snapshot(
                self.org, date(2026, 1, 1), date(2026, 12, 31)
            )
            self.assertNotEqual(month["source_hash"], previous_month["source_hash"])
            self.assertNotEqual(
                annual["opening_principal_sources"]["fingerprint"],
                previous_annual["opening_principal_sources"]["fingerprint"],
            )
        self.assertTrue(annual["opening_principal_sources"]["blockers"])
        self.assertEqual((annual["obligations"], annual["verifications"]), ([], []))
        self.assertNotIn("opening_cash_reviews", annual)

    def test_refund_remains_unsupported_for_pending_or_explicit_zero_without_inheriting_original_tax_release(
        self,
    ):
        cash = self.cash(kind="receivable")
        before = ledger.report_balances(organization=self.org)
        for reviewed in (False, True):
            if reviewed:
                self.zero_review(cash)
            counts = self.counts()
            self.assertTrue(
                dependencies.opening_cash_recovery_blockers(
                    organization=self.org,
                    settlement=cash,
                    date=cash.date,
                    action="refund",
                )
            )
            self.assertEqual(
                self.context(cash)["status"], "reviewed" if reviewed else "pending"
            )
            self.assertEqual(self.counts(), counts)
            self.assertEqual(ledger.report_balances(organization=self.org), before)

    def test_received_positive_unknown_and_raw_professional_authority_block_cancellation_without_tax_fabrication(
        self,
    ):
        from django.core.exceptions import ValidationError

        from . import opening_balance_review as reviews
        from . import workflow
        from .models import OpeningSettlementTaxReview, TaxObligation

        for treatment in ("positive", "unknown", "raw"):
            cash = self.cash(kind="receivable")
            data = self.review_data(cash)
            if treatment != "raw":
                data["decision"]["resolution"] = (
                    "unsupported_treatment"
                    if treatment == "positive"
                    else "more_evidence"
                )
                data["decision"]["reviewed_facts"]["withholding_amount"] = (
                    "2.00" if treatment == "positive" else None
                )
                reviews.record_opening_settlement_review(**data)
                self.assertEqual(self.context(cash)["status"], "held")
            else:
                workflow.record_external_review(
                    organization=self.org,
                    actor=self.actor,
                    subject=cash,
                    decision=data["decision"],
                    evidence=data["evidence"],
                    professional_name=data["professional_name"],
                    qualification_reference=data["qualification_reference"],
                    reviewed_on=data["reviewed_on"],
                    statement=data["statement"],
                    report_confirmed=True,
                )
                self.assertFalse(
                    OpeningSettlementTaxReview.objects.filter(settlement=cash).exists()
                )
                self.assertEqual(self.context(cash)["status"], "pending")
            before, counts = (
                ledger.report_balances(organization=self.org),
                self.counts(),
            )
            self.assertTrue(
                dependencies.opening_cash_recovery_blockers(
                    organization=self.org,
                    settlement=cash,
                    date=cash.date,
                    action="cancel",
                )
            )
            with self.assertRaises(ValidationError):
                self.cancel(cash)
            self.assertEqual(ledger.report_balances(organization=self.org), before)
            self.assertEqual(self.counts(), counts)
            self.assertFalse(
                TaxObligation.objects.filter(organization=self.org).exists()
            )

    def test_cash_after_principal_only_monthly_approval_stales_review_but_cancellation_cannot_release_it(
        self,
    ):
        from django.core.exceptions import ValidationError

        from . import tests_monthly_packages as fixtures
        from .monthly_source_workflow import (
            approve_monthly_package,
            monthly_decision,
            package_status,
            prepare_monthly_package,
            revise_monthly_review,
        )

        fixtures.MonthlySourcePackageTests.profile(self)
        item = self.item("300", kind="receivable")
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        decision = monthly_decision(
            package,
            rows=[],
            completeness_confirmed=True,
            inventory_note="Synthetic received professional inventory confirms only evidenced old principal before cash arrives.",
            nil_confirmed=True,
            rule_reference="Synthetic principal-only monthly classification",
        )
        approval = approve_monthly_package(
            organization=self.org,
            actor=self.actor,
            package=package,
            decision=decision,
            external_review=self.external(package, decision),
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        saved = deepcopy(package.snapshot)
        self.assertEqual(
            package_status(organization=self.org, package=package)["status"], "approved"
        )
        # Existing public cash intake accepts this later source; this distinct
        # recovery limitation is observed, not bypassed with a fabricated draft.
        cash = self.cash(item, amount="100")
        self.assertTrue(package_status(organization=self.org, package=package)["stale"])
        before = ledger.report_balances(organization=self.org)
        blockers = dependencies.opening_cash_recovery_blockers(
            organization=self.org, settlement=cash, date=cash.date, action="cancel"
        )
        self.assertTrue(any("Paket fiskal 09/2026" in message for message in blockers))
        with self.assertRaises(ValidationError):
            revise_monthly_review(
                organization=self.org,
                actor=self.actor,
                package=package,
                reason="Synthetic explicit review request cannot bypass already stale financial sources.",
                expected_fingerprint=package.package_hash,
                expected_version=package.revision,
            )
        with self.assertRaises(ValidationError):
            self.cancel(cash)
        self.assertEqual(ledger.report_balances(organization=self.org), before)
        package.refresh_from_db()
        self.assertEqual(package.snapshot, saved)
        self.assertEqual(
            package_status(organization=self.org, package=package)["approval"].pk,
            approval.pk,
        )

    def test_continuing_reviewed_monthly_obligation_keeps_principal_cash_cancellation_protected(
        self,
    ):
        from django.core.exceptions import ValidationError

        from . import tests_monthly_packages as fixtures
        from . import workflow
        from .monthly_source_workflow import (
            approve_monthly_package,
            generate_monthly_obligation,
            prepare_monthly_package,
        )

        fixtures.MonthlySourcePackageTests.profile(self, "final")
        item = self.item("300", kind="receivable")
        fixtures.MonthlySourcePackageTests.sale(
            self, "200000", suffix="CANCEL-PROTECTED-TAX"
        )
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        decision = fixtures.MonthlySourcePackageTests.decision(self, package)
        approve_monthly_package(
            organization=self.org,
            actor=self.actor,
            package=package,
            decision=decision,
            external_review=self.external(package, decision),
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        obligation = generate_monthly_obligation(
            organization=self.org,
            actor=self.actor,
            package=package,
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        obligation = workflow.approve_tax_obligation(
            obligation,
            self.actor,
            external_review=self.external(obligation, {"action": "approve_obligation"}),
        )
        self.assertEqual(obligation.amount, 1000)
        cash = self.cash(item, amount="100")
        before = ledger.report_balances(organization=self.org)
        blockers = dependencies.opening_cash_recovery_blockers(
            organization=self.org, settlement=cash, date=cash.date, action="cancel"
        )
        self.assertTrue(
            any("Kewajiban pajak 09/2026" in message for message in blockers)
        )
        with self.assertRaises(ValidationError):
            self.cancel(cash)
        with self.assertRaises(ValidationError):
            workflow.withdraw_obligation(
                organization=self.org,
                actor=self.actor,
                obligation=obligation,
                reason="Synthetic active approved obligation cannot be withdrawn to release a wrong cash match.",
                expected_version=obligation.updated_at.isoformat(),
            )
        obligation.refresh_from_db()
        self.assertEqual(
            (obligation.status, obligation.source_package_id), ("approved", package.pk)
        )
        self.assertEqual(ledger.report_balances(organization=self.org), before)

    def test_approved_filed_annual_and_closed_source_date_independently_protect_original_cash(
        self,
    ):
        import calendar
        from datetime import date

        from django.core.exceptions import ValidationError

        from finance.test_statement_fixtures import reviewed_statement
        from finance.tests_tax_money import TaxMoneyFixtures

        from . import workflow

        self.cutoff, self.day = date(2025, 9, 1), date(2025, 9, 10)
        cash = self.cash(self.item("300", kind="receivable"), amount="100")
        self.zero_review(cash)
        TaxMoneyFixtures.profile(self)
        for month in range(1, 13):
            reviewed_statement(
                self,
                organization=self.org,
                actor=self.actor,
                account=self.account,
                start=date(2025, month, 1),
                end=date(2025, month, calendar.monthrange(2025, month)[1]),
                opening="0" if month <= 9 else "100",
                closing="0" if month < 9 else "100",
                total_in="100" if month == 9 else "0",
                count=1 if month == 9 else 0,
            )
            ledger.close_month(
                organization=self.org, actor=self.actor, year=2025, month=month
            )
        workpaper = workflow.prepare_annual_workpaper(
            organization=self.org,
            actor=self.actor,
            year=2025,
            positive_adjustments="0",
            negative_adjustments="0",
            loss_compensation="0",
            turnover="0",
            regime_code="corporate_22",
            instalments="0",
            support_evidence=self.evidence(),
            reconciliation_note="Synthetic received annual reconciliation verifies original old principal and actual bank receipt.",
        )
        approval = workflow.approve_annual_workpaper(
            organization=self.org,
            actor=self.actor,
            workpaper=workpaper,
            external_review=self.external(workpaper, {"action": "approve_annual"}),
        )
        before, saved = (
            ledger.report_balances(organization=self.org),
            deepcopy(workpaper.inputs),
        )
        for filed in (False, True):
            if filed:
                filing = workflow.verify_annual_filing(
                    organization=self.org,
                    actor=self.actor,
                    workpaper=workpaper,
                    evidence=self.evidence(),
                    reference="SYN-CANCEL-PROTECTED-BPE-2025",
                    taxpayer_reference="1234567890123456",
                    year=2025,
                    return_version=0,
                    reported_tax=workpaper.result["income_tax"],
                    reported_balance_due=workpaper.result["balance_due"],
                    matches_confirmed=True,
                )
            blockers = dependencies.opening_cash_recovery_blockers(
                organization=self.org, settlement=cash, date=cash.date, action="cancel"
            )
            self.assertTrue(any("SPT tahunan 2025" in message for message in blockers))
            self.assertTrue(
                any("Buku" in message or "ditutup" in message for message in blockers)
            )
            with self.assertRaises(ValidationError):
                self.cancel(cash)
            self.assertEqual(ledger.report_balances(organization=self.org), before)
            self.assertTrue(
                workflow.annual_snapshot_status(self.org, workpaper)["snapshot_current"]
            )
        workpaper.refresh_from_db()
        self.assertEqual(workpaper.inputs, saved)
        self.assertEqual(
            (approval.workpaper_id, filing.workpaper_id), (workpaper.pk, workpaper.pk)
        )

    def test_raw_received_report_moved_to_another_cash_keeps_original_authority_cancellation_blocked(
        self,
    ):
        from django.core.exceptions import ValidationError

        from core.models import AuditEvent
        from finance.models import OpeningBalanceCancellation

        from . import workflow
        from .models import ExternalTaxReview, OpeningSettlementTaxReview

        original, other = self.cash(kind="receivable"), self.cash(kind="receivable")
        data = self.review_data(original)
        report = workflow.record_external_review(
            organization=self.org,
            actor=self.actor,
            subject=original,
            decision=data["decision"],
            evidence=data["evidence"],
            professional_name=data["professional_name"],
            qualification_reference=data["qualification_reference"],
            reviewed_on=data["reviewed_on"],
            statement=data["statement"],
            report_confirmed=True,
        )
        audit = AuditEvent.objects.get(
            organization=self.org,
            action="tax.external_review.recorded",
            object_type="ExternalTaxReview",
            object_id=str(report.pk),
        )
        self.assertEqual(
            (audit.detail["subject_type"], audit.detail["subject_id"]),
            ("openingbalancesettlement", original.pk),
        )
        self.assertFalse(
            OpeningSettlementTaxReview.objects.filter(settlement=original).exists()
        )
        self.assertTrue(
            dependencies.opening_cash_recovery_blockers(
                organization=self.org,
                settlement=original,
                date=original.date,
                action="cancel",
            )
        )
        original_proof = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=original
        )
        report_pdf, report_decision, original_audit = (
            bytes(report.evidence.content),
            deepcopy(report.decision),
            deepcopy(audit.detail),
        )
        self.tamper(ExternalTaxReview, report.pk, "subject_id", other.pk)
        before, counts = ledger.report_balances(organization=self.org), self.counts()
        self.assertTrue(
            dependencies.opening_cash_recovery_blockers(
                organization=self.org,
                settlement=original,
                date=original.date,
                action="cancel",
            )
        )
        with self.assertRaises(ValidationError):
            self.cancel(original)
        self.assertEqual(self.counts(), counts)
        self.assertFalse(
            OpeningBalanceCancellation.objects.filter(settlement=original).exists()
        )
        self.assertEqual(ledger.report_balances(organization=self.org), before)
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=original
            ),
            original_proof,
        )
        report.refresh_from_db()
        report.evidence.refresh_from_db()
        audit.refresh_from_db()
        self.assertEqual(
            (report.subject_id, report.decision, bytes(report.evidence.content)),
            (other.pk, report_decision, report_pdf),
        )
        self.assertEqual(audit.detail, original_audit)

    def test_explicit_monthly_revision_before_cash_preserves_approval_and_permits_unreviewed_wrong_match_recovery(
        self,
    ):
        from core.models import AuditEvent

        from . import tests_monthly_packages as fixtures
        from .monthly_source_workflow import (
            approve_monthly_package,
            monthly_decision,
            package_status,
            prepare_monthly_package,
            revise_monthly_review,
        )

        fixtures.MonthlySourcePackageTests.profile(self)
        item = self.item("300", kind="receivable")
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        decision = monthly_decision(
            package,
            rows=[],
            completeness_confirmed=True,
            inventory_note="Synthetic verified old principal inventory before the first actual bank receipt.",
            nil_confirmed=True,
            rule_reference="Synthetic received principal-only period classification",
        )
        approval = approve_monthly_package(
            organization=self.org,
            actor=self.actor,
            package=package,
            decision=decision,
            external_review=self.external(package, decision),
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        saved = deepcopy(package.snapshot)
        revised = revise_monthly_review(
            organization=self.org,
            actor=self.actor,
            package=package,
            reason="Synthetic explicit professional recheck starts before a new financial source arrives.",
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        release = AuditEvent.objects.get(
            organization=self.org,
            action="tax.monthly_package.review_revision",
            object_type="MonthlyTaxSourcePackage",
            object_id=str(revised.pk),
        )
        self.assertEqual(
            (revised.previous_id, release.detail["previous_approval_id"]),
            (package.pk, approval.pk),
        )
        cash = self.cash(item, amount="100")
        original_proof = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=cash
        )
        self.assertEqual(
            dependencies.opening_cash_recovery_blockers(
                organization=self.org, settlement=cash, date=cash.date, action="cancel"
            ),
            [],
        )
        cancellation = self.cancel(cash)
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["AR"],
                balances["BANK"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (300, 0, 0, 0),
        )
        self.assertEqual(cancellation.settlement_id, cash.pk)
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            ),
            original_proof,
        )
        package.refresh_from_db()
        self.assertEqual(package.snapshot, saved)
        self.assertEqual(
            package_status(organization=self.org, package=package)["approval"].pk,
            approval.pk,
        )
        current = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        self.assertEqual(current.snapshot["rows"][0]["source_id"], cash.pk)
        self.assertFalse(current.snapshot["rows"][0]["proposed_inclusion"])
        self.assertIn("opening_cancellation", current.snapshot["rows"][0])
        self.assertIsNone(
            package_status(organization=self.org, package=current)["taxable_turnover"]
        )
