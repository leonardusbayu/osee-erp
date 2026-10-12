"""Real PostgreSQL contention; every outcome is checked after both writers exit."""

from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from threading import Barrier
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from finance import services as finance
from operations import services as operations
from operations.attribution import correct_lead_invoice
from operations.models import Order

from . import tests_identity_legacy as legacy
from . import tests_identity_recovery as acceptance
from .identity_recovery import recover_invoice_identity
from .models import LeadIdentityChange


def contend(*commands):
    """Start both commands together on separate real database connections."""
    barrier = Barrier(len(commands))

    def execute(command):
        connections.close_all()
        try:
            barrier.wait(timeout=15)
            try:
                result = command()
            except ValidationError:
                return {"status": "rejected"}
            return {"status": "accepted", "id": result.pk}
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=len(commands)) as executor:
        futures = [executor.submit(execute, command) for command in commands]
        return [future.result(timeout=30) for future in futures]


def pay_source_and_targets(fixture, source, *targets):
    """Preservation in races includes real bank postings/allocations/journals."""
    for index, invoice in enumerate((source, *targets)):
        amount = invoice.total if index == 0 else invoice.total / 2
        movement = fixture.movement(str(amount), reference=f"RACE-PAY-{invoice.pk}")
        finance.reconcile_receipt(
            organization=fixture.org,
            actor=fixture.owner,
            invoice=invoice,
            transaction=movement,
            amount=str(amount),
        )


def financial_core_rows(fixture):
    documents = fixture.preserved_documents()
    return {
        "audits": [
            row
            for row in documents["original_document_audits"]
            if row["action"].startswith(("finance.", "taxes."))
        ],
        "domains": [
            row
            for row in documents["core.DomainEvent"]
            if not row["aggregate_type"].startswith("operations.")
        ],
    }


@skipUnless(
    connection.vendor == "postgresql",
    "Real identity races require PostgreSQL row locks",
)
@override_settings(
    MARKETING_AI_PRIVATE_AGGREGATES_ENABLED=False, DIRECTOR_AI_ENDPOINT_APPROVED=False
)
class InvoiceIdentityPostgresRaceTests(TransactionTestCase):
    # Reuse only fixture helpers, never inherit/discover the service suite again.
    setUp = acceptance.InvoiceIdentityRecoveryAcceptanceTests.setUp
    invoice = acceptance.InvoiceIdentityRecoveryAcceptanceTests.invoice
    movement = acceptance.InvoiceIdentityRecoveryAcceptanceTests.movement
    lead = acceptance.InvoiceIdentityRecoveryAcceptanceTests.lead
    issued = acceptance.InvoiceIdentityRecoveryAcceptanceTests.issued
    inferred = acceptance.InvoiceIdentityRecoveryAcceptanceTests.inferred
    target = acceptance.InvoiceIdentityRecoveryAcceptanceTests.target
    context = acceptance.InvoiceIdentityRecoveryAcceptanceTests.context
    proposal = acceptance.InvoiceIdentityRecoveryAcceptanceTests.proposal
    mapping = acceptance.InvoiceIdentityRecoveryAcceptanceTests.mapping
    metadata = acceptance.InvoiceIdentityRecoveryAcceptanceTests.metadata
    scoped_rows = acceptance.InvoiceIdentityRecoveryAcceptanceTests.scoped_rows
    preserved_documents = (
        acceptance.InvoiceIdentityRecoveryAcceptanceTests.preserved_documents
    )

    def assert_chain(self, lead, source, length=2):
        rows = list(
            LeadIdentityChange.objects.filter(
                organization=self.org, lead=lead
            ).order_by("pk")
        )
        self.assertEqual(len(rows), length)
        self.assertIsNone(rows[0].previous_id)
        for previous, row in pairwise(rows):
            self.assertEqual(row.previous_id, previous.pk)
            self.assertEqual(
                (row.before_party_id, row.before_product_id, row.before_invoice_id),
                (
                    previous.after_party_id,
                    previous.after_product_id,
                    previous.after_invoice_id,
                ),
            )
        lead.refresh_from_db()
        self.assertEqual(
            (lead.party_id, lead.product_id, lead.invoice_id, lead.version),
            (
                rows[-1].after_party_id,
                rows[-1].after_product_id,
                rows[-1].after_invoice_id,
                rows[-1].after_version,
            ),
        )
        context = self.context(lead)
        self.assertFalse(context["blockers"])
        for field in ("party", "product"):
            self.assertEqual(context["origins"][field]["source_invoice_id"], source.pk)
        return rows

    def test_01_two_replacements_have_one_persisted_winner(self):
        lead, source = self.inferred()
        left, right = self.target(number="RACE-LEFT"), self.target(number="RACE-RIGHT")
        pay_source_and_targets(self, source, left, right)
        first = self.proposal(lead, left, request_key="RACE-LEFT")
        second = self.proposal(lead, right, request_key="RACE-RIGHT")
        documents, metadata = self.preserved_documents(), self.metadata(lead)
        outcomes = contend(
            lambda: recover_invoice_identity(**first),
            lambda: recover_invoice_identity(**second),
        )
        self.assertCountEqual(
            [row["status"] for row in outcomes], ["accepted", "rejected"]
        )
        rows = self.assert_chain(lead, source)
        self.assertIn(lead.invoice_id, (left.pk, right.pk))
        self.assertEqual(rows[-1].kind, "replace")
        self.assertEqual(self.preserved_documents(), documents)
        self.assertEqual(self.metadata(lead), metadata)

    def test_02_reset_and_replacement_cannot_both_consume_the_same_view(self):
        lead, source = self.inferred()
        target = self.target()
        pay_source_and_targets(self, source, target)
        replacement = self.proposal(lead, target, request_key="RACE-REPLACE")
        reset = self.proposal(lead, request_key="RACE-RESET")
        documents = self.preserved_documents()
        outcomes = contend(
            lambda: recover_invoice_identity(**replacement),
            lambda: recover_invoice_identity(**reset),
        )
        self.assertCountEqual(
            [row["status"] for row in outcomes], ["accepted", "rejected"]
        )
        rows = self.assert_chain(lead, source)
        self.assertIn(rows[-1].kind, ("replace", "reset"))
        self.assertEqual(
            (lead.party_id, lead.product_id, lead.invoice_id),
            (target.party_id, target.product_id, target.pk)
            if rows[-1].kind == "replace"
            else (None, None, None),
        )
        self.assertEqual(self.preserved_documents(), documents)

    def test_03_exact_simultaneous_retry_returns_one_decision(self):
        lead, source = self.inferred()
        target = self.target()
        pay_source_and_targets(self, source, target)
        payload = self.proposal(lead, target, request_key="RACE-EXACT-RETRY")
        documents = self.preserved_documents()
        outcomes = contend(
            lambda: recover_invoice_identity(**payload),
            lambda: recover_invoice_identity(**payload),
        )
        self.assertEqual([row["status"] for row in outcomes], ["accepted", "accepted"])
        self.assertEqual(outcomes[0]["id"], outcomes[1]["id"])
        rows = self.assert_chain(lead, source)
        self.assertEqual(rows[-1].pk, outcomes[0]["id"])
        self.assertEqual(self.preserved_documents(), documents)

    def test_04_old_attribution_correction_and_recovery_are_serialized(self):
        lead, source = self.inferred()
        target = self.target()
        pay_source_and_targets(self, source, target)
        payload = self.proposal(lead, target, request_key="RACE-ATTRIBUTION-RECOVERY")
        documents = self.preserved_documents()
        outcomes = contend(
            lambda: recover_invoice_identity(**payload),
            lambda: correct_lead_invoice(
                organization=self.org,
                actor=self.finance,
                lead=lead,
                invoice=None,
                expected_version=2,
                reason="Synthetic explicit attribution detachment",
            ),
        )
        self.assertCountEqual(
            [row["status"] for row in outcomes], ["accepted", "rejected"]
        )
        rows = self.assert_chain(lead, source)
        self.assertIn(rows[-1].kind, ("replace", "attribution"))
        self.assertEqual(
            (lead.party_id, lead.product_id, lead.invoice_id),
            (target.party_id, target.product_id, target.pk)
            if rows[-1].kind == "replace"
            else (source.party_id, source.product_id, None),
        )
        self.assertEqual(self.preserved_documents(), documents)

    def test_05_order_assignment_and_different_identity_recovery_have_one_consistent_winner(
        self,
    ):
        lead, source = self.inferred()
        target = self.target()
        pay_source_and_targets(self, source, target)
        order = operations.create_order(
            organization=self.org,
            actor=self.owner,
            reference="RACE-ORDER",
            buyer=self.party,
            date=self.day,
            channel="meta_ads",
        )
        line = operations.add_order_line(
            organization=self.org,
            actor=self.owner,
            order=order,
            product=self.product,
            quantity=source.quantity,
            reference="Synthetic race line",
        )
        line = operations.link_order_invoice(
            organization=self.org,
            actor=self.owner,
            line=line,
            invoice=source,
            reason="Synthetic original invoice reviewed",
        )
        order.refresh_from_db()
        payload = self.proposal(lead, target, request_key="RACE-ORDER-RECOVERY")
        finance_rows = self.scoped_rows(("finance", "taxes"))
        core_rows = financial_core_rows(self)
        source_line = (line.order_id, line.product_id, line.invoice_id, line.quantity)
        outcomes = contend(
            lambda: recover_invoice_identity(**payload),
            lambda: operations.link_order_lead(
                organization=self.org,
                actor=self.owner,
                order=order,
                lead=lead,
                expected_version=order.version,
                reason="Synthetic original buyer confirmed",
                confirm_identity=True,
            ),
        )
        self.assertCountEqual(
            [row["status"] for row in outcomes], ["accepted", "rejected"]
        )
        lead.refresh_from_db()
        order = Order.objects.get(pk=order.pk)
        line.refresh_from_db()
        self.assertEqual(
            (line.order_id, line.product_id, line.invoice_id, line.quantity),
            source_line,
        )
        self.assertEqual(self.scoped_rows(("finance", "taxes")), finance_rows)
        self.assertEqual(financial_core_rows(self), core_rows)
        if outcomes[0]["status"] == "accepted":
            self.assertIsNone(order.lead_id)
            self.assert_chain(lead, source)
            self.assertEqual(lead.invoice_id, target.pk)
        else:
            self.assertEqual(order.lead_id, lead.pk)
            self.assertEqual(
                (lead.party_id, lead.product_id, lead.invoice_id, lead.version),
                (source.party_id, source.product_id, source.pk, 2),
            )
            self.assertEqual(
                LeadIdentityChange.objects.filter(
                    organization=self.org, lead=lead
                ).count(),
                1,
            )
            self.assertTrue(self.context(lead)["order_dependencies"])


@skipUnless(
    connection.vendor == "postgresql",
    "Real legacy adoption races require PostgreSQL row locks",
)
@override_settings(
    MARKETING_AI_PRIVATE_AGGREGATES_ENABLED=False, DIRECTOR_AI_ENDPOINT_APPROVED=False
)
class LegacyIdentityPostgresRaceTests(TransactionTestCase):
    previous_marketing = legacy.LegacyInvoiceIdentityAcceptanceTests.previous_marketing
    setUp = legacy.LegacyInvoiceIdentityAcceptanceTests.setUp
    restore_schema = legacy.LegacyInvoiceIdentityAcceptanceTests.restore_schema
    invoice = legacy.LegacyInvoiceIdentityAcceptanceTests.invoice
    movement = legacy.LegacyInvoiceIdentityAcceptanceTests.movement
    issued = legacy.LegacyInvoiceIdentityAcceptanceTests.issued
    legacy_link = legacy.LegacyInvoiceIdentityAcceptanceTests.legacy_link
    fixture = legacy.LegacyInvoiceIdentityAcceptanceTests.fixture
    context = legacy.LegacyInvoiceIdentityAcceptanceTests.context
    proposal = legacy.LegacyInvoiceIdentityAcceptanceTests.proposal
    scoped_rows = acceptance.InvoiceIdentityRecoveryAcceptanceTests.scoped_rows
    preserved_documents = (
        acceptance.InvoiceIdentityRecoveryAcceptanceTests.preserved_documents
    )

    def test_06_legacy_review_and_replacement_are_one_atomic_winner(self):
        self.fixture()
        target2 = self.issued(
            number="LEGACY-RACE-SECOND", party=self.party_b, product=self.product_b
        )
        pay_source_and_targets(self, self.source, self.target, target2)
        first = self.proposal(request_key="LEGACY-RACE-ONE")
        second = self.proposal(target_invoice=target2, request_key="LEGACY-RACE-TWO")
        original_audit = list(
            acceptance.AuditEvent.objects.filter(
                organization=self.org,
                action__in=("marketing.lead.created", "marketing.lead.invoice_linked"),
            )
            .order_by("pk")
            .values()
        )
        documents = self.preserved_documents()
        outcomes = contend(
            lambda: recover_invoice_identity(**first),
            lambda: recover_invoice_identity(**second),
        )
        self.assertCountEqual(
            [row["status"] for row in outcomes], ["accepted", "rejected"]
        )
        rows = list(
            LeadIdentityChange.objects.filter(
                organization=self.org, lead=self.lead
            ).order_by("pk")
        )
        self.assertEqual([row.kind for row in rows], ["legacy_adoption", "replace"])
        self.assertEqual(rows[1].previous_id, rows[0].pk)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.version, 3)
        self.assertIn(self.lead.invoice_id, (self.target.pk, target2.pk))
        self.assertFalse(self.context()["legacy_review_required"])
        self.assertEqual(
            list(
                acceptance.AuditEvent.objects.filter(
                    organization=self.org,
                    action__in=(
                        "marketing.lead.created",
                        "marketing.lead.invoice_linked",
                    ),
                )
                .order_by("pk")
                .values()
            ),
            original_audit,
        )
        self.assertEqual(self.preserved_documents(), documents)
