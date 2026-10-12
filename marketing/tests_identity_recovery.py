"""MKT-02 contract acceptance through the invoice-identity recovery seam.

All fixture data is synthetic. The unchanged public RED probe is separate.

Confirmed seams: marketing.services.link_invoice, public Finance/Operations
commands, identity_recovery_context, recover_invoice_identity, persisted history.
The database snapshots are deliberate preservation/atomicity oracles, not a
substitute for asserting the public service result and refreshed Lead mapping.

Agreed public contracts:
* Context has version, fingerprint, eligible_fields (party/product), origins,
  ordered history of LeadIdentityChange objects, legacy_review_required, blockers.
* origins[field] has kind (invoice_inferred/protected/unknown) and original
  first source_invoice_id. Reset may expose kind=unknown while retaining source.
* LeadIdentityChange is exported from marketing.models. Typed columns are named
  before_party/after_party, before_product/after_product, before_invoice/
  after_invoice, previous, before_version/after_version, kind, reason, evidence,
  request_key, viewed_fingerprint, created_by.
* recover_invoice_identity returns the persisted decision; decision.lead may be
  cached, so assertions reload Lead independently. No tuple return is assumed.
* New source history is independent of mutable legacy audit mirrors. It requires
  no legacy review. Legacy-only rows require an explicit review/adoption decision.
* ValidationError is the user-facing invalid/stale/replay-conflict exception;
  PermissionDenied is the authorization exception. Exact message text is unset.
* Historical-source status changes stale a viewed fingerprint; cancellation
  retains origin eligibility and a freshly reviewed replacement can proceed.
* ORM save/create/update/bulk_update/delete of history reject ValidationError.
  Narrow Lead privileges expire on both success and exception, including flags
  used by normal link and old attribution correction.

Separate legacy-adoption/concurrency/HTTP acceptance requirements:
1. Pre-migration genuine created/link/correction audit history requires explicit
   confirm_legacy_source plus visible immutable adoption; GET writes nothing.
2. Exact type matrix: null/list/string detail; missing/extra mapping keys;
   bool/float/string/nonpositive IDs or versions; absent/nonbool confirmation;
   wrong basis/object_type/noncanonical object_id/org/source scope all block.
3. Known creation value plus forged null-before, conflicting duplicate versions,
   multiple legacy inferences, impossible predecessor, deleted/missing creation
   or origin, source mismatch, and event version beyond Lead all block atomically.
4. Legacy audit changed after viewed fingerprint rejects even if Lead version is
   unchanged; changed content before first review must not be called immutable.
5. Deleted historical actor and revoked historical membership remain readable;
   cancelled-from-draft source without issue-before-link proof blocks adoption.
6. Failed legacy adoption never consumes request key or stores an adoption row.
7. PostgreSQL races: replacement/replacement, replacement/reset, same-key retry,
   attribution correction/recovery, Order reassignment/recovery; persisted final
   chain and every financial/Operations row must be checked in root's native lane.
8. HTTP CSRF, stale form input retention, obsolete recommendation token rejection,
   desktop/mobile flow, source-bound unchanged public RED replay and staff UAT.
"""

import json
from copy import deepcopy
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import AuditEvent, Membership, Organization
from finance import services as finance
from finance.costing import allocate_cost
from finance.fulfillment import credit_units, deliver_units, refund_credit
from finance.models import Party, Product
from finance.settlement import applied_payment_amounts
from finance.tests_tax_money import TaxMoneyFixtures
from marketing import services, workflow
from marketing.ai import _outbound_body, advice_identity, build_recommendations
from marketing.analytics import marketing_snapshot
from marketing.identity_recovery import (
    identity_recovery_context,
    recover_invoice_identity,
)
from marketing.management import management_snapshot
from marketing.models import Lead, LeadIdentityChange
from operations import services as operations
from operations.attribution import correct_lead_invoice, invoice_attribution


@override_settings(
    MARKETING_AI_PRIVATE_AGGREGATES_ENABLED=False, DIRECTOR_AI_ENDPOINT_APPROVED=False
)
class InvoiceIdentityRecoveryAcceptanceTests(TaxMoneyFixtures, TestCase):
    """18 acceptance cases through public commands and preserved persisted facts."""

    def setUp(self):
        TaxMoneyFixtures.setUp(self)
        self.other = Organization.objects.create(
            name="Synthetic foreign identity company"
        )
        for role in ("finance", "marketing", "director", "auditor"):
            user = get_user_model().objects.create_user(
                username=f"identity-draft-{role}"
            )
            Membership.objects.create(organization=self.org, user=user, role=role)
            setattr(self, role, user)
        self.foreign_owner = get_user_model().objects.create_user(
            "identity-draft-foreign"
        )
        Membership.objects.create(
            organization=self.other, user=self.foreign_owner, role="owner"
        )
        self.party_b = Party.objects.create(
            organization=self.org, name="Synthetic correct buyer", kind="reseller"
        )
        self.product_b = Product.objects.create(
            organization=self.org,
            code="CORRECT",
            name="Synthetic correct product",
            kind="itp",
            default_price="500000",
        )
        self.received = timezone.make_aware(
            datetime.combine(self.day - timedelta(days=1), time(10))
        )
        self.member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=services.create_member(
                organization=self.org,
                actor=self.marketing,
                name="Synthetic original PIC",
                discipline="sales",
            ),
            expected_version=1,
            account=self.marketing,
            active=True,
            reason="Synthetic PIC account reviewed",
        )
        self.serial = 0

    def lead(self, **changes):
        self.serial += 1
        return services.create_lead(
            organization=self.org,
            actor=self.marketing,
            **{
                "reference": f"IDENTITY-{self.serial}",
                "name": "Synthetic opportunity",
                "received_at": self.received,
                "channel": "meta_ads",
                "owner": self.member,
                "party": None,
                "product": None,
                "segment": "reseller",
                "region": "Jakarta",
                "notes": "Original staff notes",
                **changes,
            },
        )

    def issued(self, **changes):
        organization = changes.get("organization", self.org)
        actor = changes.get("actor", self.owner)
        return finance.issue_invoice(
            organization=organization, actor=actor, invoice=self.invoice(**changes)
        )

    def inferred(self, **changes):
        lead = self.lead(**changes)
        invoice = self.issued()
        lead = services.link_invoice(
            organization=self.org,
            actor=self.finance,
            lead=lead,
            invoice=invoice,
            expected_version=lead.version,
            confirm_identity=True,
        )
        return lead, invoice

    def target(self, **changes):
        return self.issued(
            **{"party": self.party_b, "product": self.product_b, **changes}
        )

    def context(self, lead):
        return identity_recovery_context(organization=self.org, lead=lead)

    def proposal(self, lead, target=None, **changes):
        lead.refresh_from_db()
        return {
            "organization": self.org,
            "actor": self.finance,
            "lead": lead,
            "action": "reset" if target is None else "replace",
            "target_invoice": target,
            "expected_version": lead.version,
            "expected_fingerprint": self.context(lead)["fingerprint"],
            "request_key": f"IDENTITY-RECOVERY-{lead.pk}",
            "confirm_identity": True,
            "reason": "Synthetic wrong source checked",
            "evidence": "Synthetic archive/reference.txt",
            "confirm_legacy_source": False,
            **changes,
        }

    def mapping(self, lead):
        lead.refresh_from_db()
        return (lead.party_id, lead.product_id, lead.invoice_id, lead.version)

    def metadata(self, lead):
        lead.refresh_from_db()
        names = (
            "organization_id",
            "reference",
            "name",
            "received_at",
            "campaign_id",
            "channel",
            "owner_id",
            "current_owner_id",
            "segment",
            "region",
            "stage",
            "first_response_at",
            "next_follow_up",
            "next_follow_up_at",
            "notes",
            "created_by_id",
            "created_at",
        )
        return {name: deepcopy(getattr(lead, name)) for name in names}

    def scoped_rows(self, labels, organization=None):
        """Snapshot all concrete persisted fields, including metadata and JSON."""
        organization = organization or self.org
        result = {}
        for label in labels:
            for model in apps.get_app_config(label).get_models():
                names = [field.attname for field in model._meta.concrete_fields]
                if "organization_id" in names:
                    result[model._meta.label] = list(
                        model.objects.filter(organization=organization)
                        .order_by("pk")
                        .values(*names)
                    )
        return result

    def preserved_documents(self):
        result = self.scoped_rows(("finance", "operations", "taxes"))
        result["original_document_audits"] = list(
            AuditEvent.objects.filter(
                organization=self.org, action__regex=r"^(finance|operations|taxes)\."
            )
            .order_by("pk")
            .values()
        )
        # Financial domain events are facts too; only organizations with this
        # concrete model/field participate, keeping this oracle schema-neutral.
        for model in apps.get_app_config("core").get_models():
            names = [field.attname for field in model._meta.concrete_fields]
            if model.__name__ == "DomainEvent" and "organization_id" in names:
                document_types = [
                    row.__name__
                    for label in ("finance", "operations", "taxes")
                    for row in apps.get_app_config(label).get_models()
                ]
                result[model._meta.label] = list(
                    model.objects.filter(organization=self.org)
                    .filter(
                        Q(aggregate_type__in=document_types)
                        | Q(aggregate_type__regex=r"^(finance|operations|taxes)\.")
                    )
                    .order_by("pk")
                    .values(*names)
                )
        return result

    def entire_state(self, organization=None):
        organization = organization or self.org
        return self.scoped_rows(
            ("core", "marketing", "finance", "operations", "taxes"), organization
        )

    def origin_source(self, context, field):
        return context["origins"][field]["source_invoice_id"]

    def history_ids(self, context):
        return [change.pk for change in context["history"]]

    def assert_no_privileges(self, obj):
        for flag in (
            "_invoice_link",
            "_service_write",
            "_workflow_write",
            "_identity_recovery",
            "_identity_recovery_write",
            "_identity_write",
            "_service_transition",
        ):
            self.assertFalse(getattr(obj, flag, False), flag)

    def rich_preservation_facts(self, source, target):
        """Real money, tax proof, fulfillment, credit/refund, costing, participants."""
        finance.reconcile_receipt(
            organization=self.org,
            actor=self.owner,
            invoice=source,
            transaction=self.movement("500000", reference="SOURCE-A-PAID"),
            amount="500000",
        )
        finance.reconcile_receipt(
            organization=self.org,
            actor=self.owner,
            invoice=target,
            transaction=self.movement("100000", reference="TARGET-B-PARTIAL"),
            amount="100000",
        )
        self.profile()
        tax_invoice = self.issued(number="PRESERVE-TAX")
        obligation = self.approved(invoice=tax_invoice)
        self.credit(obligation)
        self.net_receipt(obligation, reference="PRESERVE-NET-TAX")
        sentinel = self.issued(number="PRESERVE-UNITS", quantity=2)
        finance.reconcile_receipt(
            organization=self.org,
            actor=self.owner,
            invoice=sentinel,
            transaction=self.movement("1000000", reference="PRESERVE-UNIT-PAID"),
            amount="1000000",
        )
        deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=sentinel,
            quantity=1,
            date=self.day,
            reference="PRESERVE-DELIVERY",
            evidence="Synthetic signed attendance",
        )
        credit = credit_units(
            organization=self.org,
            actor=self.owner,
            invoice=sentinel,
            quantity=1,
            kind="unfulfilled",
            date=self.day,
            reference="PRESERVE-CREDIT",
            evidence="Synthetic approved cancellation",
        )
        refund_credit(
            organization=self.org,
            actor=self.owner,
            credit=credit,
            transaction=self.movement("-500000", reference="PRESERVE-REFUND-BANK"),
            amount="500000",
            reference="PRESERVE-REFUND",
            evidence="Synthetic transfer evidence",
        )
        bill = finance.approve_bill(
            organization=self.org,
            actor=self.owner,
            bill=finance.create_bill(
                organization=self.org,
                actor=self.owner,
                supplier=self.supplier,
                amount="250000",
                date=self.day,
                service_date=self.day,
                category="provider",
                number="PRESERVE-BILL",
            ),
        )
        allocate_cost(
            organization=self.org,
            actor=self.owner,
            invoice=sentinel,
            bill=bill,
            quantity=1,
            amount="250000",
            date=self.day,
            reference="PRESERVE-COST",
            reason="Synthetic provider unit",
            evidence="Synthetic confirmed cost evidence",
        )
        order = operations.create_order(
            organization=self.org,
            actor=self.owner,
            reference="PRESERVE-ORDER",
            buyer=self.party,
            date=self.day,
            channel="mitra",
        )
        line = operations.add_order_line(
            organization=self.org,
            actor=self.owner,
            order=order,
            product=self.product,
            quantity=1,
        )
        operations.register_participant(
            organization=self.org,
            actor=self.owner,
            line=line,
            name="Synthetic participant",
            source="Synthetic intake",
            external_id="PRESERVE-PERSON",
            email="synthetic@example.invalid",
            phone="0800000000",
            planned_date=self.day,
        )
        operations.invoice_line(
            organization=self.org, actor=self.owner, line=line, date=self.day
        )

    def test_01_new_inference_records_typed_history_and_context_read_is_pure(self):
        lead, source = self.inferred()
        before = self.entire_state()
        context = self.context(lead)
        self.assertEqual(context, self.context(lead))
        self.assertEqual(self.entire_state(), before)
        self.assertEqual(set(context["eligible_fields"]), {"party", "product"})
        self.assertFalse(context["legacy_review_required"])
        self.assertEqual(context["blockers"], [])
        self.assertRegex(context["fingerprint"], r"^[a-f0-9]{64}$")
        self.assertEqual(context["version"], 2)
        self.assertEqual(len(context["history"]), 1)
        origin = context["history"][0]
        self.assertIsInstance(origin, LeadIdentityChange)
        self.assertEqual(
            (origin.organization_id, origin.lead_id, origin.kind),
            (self.org.pk, lead.pk, "invoice_link"),
        )
        self.assertEqual(
            (
                origin.before_party_id,
                origin.before_product_id,
                origin.before_invoice_id,
            ),
            (None, None, None),
        )
        self.assertEqual(
            (origin.after_party_id, origin.after_product_id, origin.after_invoice_id),
            (source.party_id, source.product_id, source.pk),
        )
        for field in ("party", "product"):
            self.assertEqual(context["origins"][field]["kind"], "invoice_inferred")
            self.assertEqual(self.origin_source(context, field), source.pk)

    def test_02_replace_both_inferred_fields_preserves_all_finance_operations_and_metadata(
        self,
    ):
        lead, source = self.inferred()
        target = self.target()
        self.rich_preservation_facts(source, target)
        metadata, documents = self.metadata(lead), self.preserved_documents()
        original_audit = AuditEvent.objects.get(
            action="marketing.lead.invoice_linked", object_id=str(lead.pk)
        )
        original_audit_values = (
            AuditEvent.objects.filter(pk=original_audit.pk).values().get()
        )
        history = self.history_ids(self.context(lead))
        payload = self.proposal(lead, target)
        decision = recover_invoice_identity(**payload)
        self.assertIsInstance(decision, LeadIdentityChange)
        self.assertEqual(
            self.mapping(lead), (target.party_id, target.product_id, target.pk, 3)
        )
        self.assertEqual(self.metadata(lead), metadata)
        self.assertEqual(self.preserved_documents(), documents)
        self.assertEqual(
            AuditEvent.objects.filter(pk=original_audit.pk).values().get(),
            original_audit_values,
        )
        self.assertEqual(
            (
                decision.before_party_id,
                decision.before_product_id,
                decision.before_invoice_id,
            ),
            (source.party_id, source.product_id, source.pk),
        )
        self.assertEqual(
            (
                decision.after_party_id,
                decision.after_product_id,
                decision.after_invoice_id,
            ),
            (target.party_id, target.product_id, target.pk),
        )
        self.assertEqual(
            (
                decision.created_by_id,
                decision.request_key,
                decision.reason,
                decision.evidence,
            ),
            (
                self.finance.pk,
                payload["request_key"],
                payload["reason"],
                payload["evidence"],
            ),
        )
        self.assertEqual(decision.viewed_fingerprint, payload["expected_fingerprint"])
        now = self.context(lead)
        self.assertEqual(self.history_ids(now), history + [decision.pk])
        for field in ("party", "product"):
            self.assertEqual(self.origin_source(now, field), source.pk)
        paid = applied_payment_amounts(
            organization=self.org, invoice_ids=[source.pk, target.pk]
        )
        self.assertEqual(paid, {source.pk: Decimal(500000), target.pk: Decimal(100000)})
        mapping = invoice_attribution(organization=self.org)
        self.assertEqual(mapping["by_lead"][lead.pk], [target.pk])
        self.assertNotIn(source.pk, mapping["by_invoice"])
        self.assert_no_privileges(decision.lead)

    def test_03_reset_only_inferred_identity_without_moving_money_or_making_fake_identity(
        self,
    ):
        lead, source = self.inferred()
        target = self.target()
        self.rich_preservation_facts(source, target)
        metadata, documents = self.metadata(lead), self.preserved_documents()
        party_ids = list(
            Party.objects.filter(organization=self.org).values_list("pk", flat=True)
        )
        product_ids = list(
            Product.objects.filter(organization=self.org).values_list("pk", flat=True)
        )
        decision = recover_invoice_identity(**self.proposal(lead))
        self.assertEqual(self.mapping(lead), (None, None, None, 3))
        self.assertEqual(
            (
                decision.after_party_id,
                decision.after_product_id,
                decision.after_invoice_id,
            ),
            (None, None, None),
        )
        self.assertEqual(self.metadata(lead), metadata)
        self.assertEqual(self.preserved_documents(), documents)
        self.assertEqual(
            list(
                Party.objects.filter(organization=self.org).values_list("pk", flat=True)
            ),
            party_ids,
        )
        self.assertEqual(
            list(
                Product.objects.filter(organization=self.org).values_list(
                    "pk", flat=True
                )
            ),
            product_ids,
        )
        context = self.context(lead)
        self.assertIn(decision.pk, self.history_ids(context))
        for field in ("party", "product"):
            self.assertEqual(self.origin_source(context, field), source.pk)
        self.assertNotIn(lead.pk, invoice_attribution(organization=self.org)["by_lead"])
        self.assertEqual(
            applied_payment_amounts(organization=self.org, invoice_ids=[source.pk])[
                source.pk
            ],
            Decimal(500000),
        )

    def test_04_manual_buyer_protects_only_buyer_and_rejects_wrong_buyer_target(self):
        lead, source = self.inferred(party=self.party)
        self.assertEqual(set(self.context(lead)["eligible_fields"]), {"product"})
        wrong = self.target()
        before = self.entire_state()
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**self.proposal(lead, wrong))
        self.assertEqual(self.entire_state(), before)
        correct = self.target(party=self.party)
        recover_invoice_identity(**self.proposal(lead, correct))
        self.assertEqual(
            self.mapping(lead)[:3], (self.party.pk, self.product_b.pk, correct.pk)
        )
        recover_invoice_identity(**self.proposal(lead, request_key="RESET-MIXED"))
        self.assertEqual(self.mapping(lead)[:3], (self.party.pk, None, None))
        self.assertEqual(self.context(lead)["origins"]["party"]["kind"], "protected")
        self.assertIsNone(self.origin_source(self.context(lead), "party"))
        self.assertEqual(self.origin_source(self.context(lead), "product"), source.pk)

    def test_05_campaign_product_remains_protected_through_replace_and_reset(self):
        campaign = services.create_campaign(
            organization=self.org,
            actor=self.marketing,
            name="Synthetic original campaign",
            channel="meta_ads",
            product=self.product,
        )
        lead, source = self.inferred(campaign=campaign)
        self.assertEqual(set(self.context(lead)["eligible_fields"]), {"party"})
        wrong = self.target()
        before = self.entire_state()
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**self.proposal(lead, wrong))
        self.assertEqual(self.entire_state(), before)
        correct = self.target(product=self.product)
        recover_invoice_identity(**self.proposal(lead, correct))
        self.assertEqual(
            self.mapping(lead)[:3], (self.party_b.pk, self.product.pk, correct.pk)
        )
        recover_invoice_identity(**self.proposal(lead, request_key="RESET-CAMPAIGN"))
        self.assertEqual(self.mapping(lead)[:3], (None, self.product.pk, None))
        lead.refresh_from_db()
        self.assertEqual((lead.campaign_id, lead.channel), (campaign.pk, "meta_ads"))
        self.assertEqual(self.context(lead)["origins"]["product"]["kind"], "protected")
        self.assertEqual(self.origin_source(self.context(lead), "party"), source.pk)

    def test_06_manual_equal_values_and_same_invoice_retry_never_fabricate_an_origin(
        self,
    ):
        lead = self.lead(party=self.party, product=self.product)
        source = self.issued()
        lead = services.link_invoice(
            organization=self.org,
            actor=self.finance,
            lead=lead,
            invoice=source,
            expected_version=1,
            confirm_identity=True,
        )
        context = self.context(lead)
        self.assertEqual(context["eligible_fields"], [])
        self.assertTrue(
            all(
                context["origins"][field]["kind"] == "protected"
                for field in ("party", "product")
            )
        )
        before = self.entire_state()
        retry = services.link_invoice(
            organization=self.org,
            actor=self.finance,
            lead=lead,
            invoice=source,
            expected_version=2,
            confirm_identity=True,
        )
        self.assertEqual(retry.pk, lead.pk)
        self.assertEqual(self.entire_state(), before)
        self.assertEqual(
            self.history_ids(self.context(lead)), self.history_ids(context)
        )
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**self.proposal(lead))
        self.assertEqual(self.entire_state(), before)
        inferred, real_source = self.inferred()
        inferred_before = self.entire_state()
        services.link_invoice(
            organization=self.org,
            actor=self.finance,
            lead=inferred,
            invoice=real_source,
            expected_version=2,
            confirm_identity=True,
        )
        self.assertEqual(self.entire_state(), inferred_before)
        self.assertEqual(
            self.origin_source(self.context(inferred), "party"), real_source.pk
        )

    def test_07_order_confirmation_does_not_infer_lead_identity_or_offer_recovery(self):
        lead = self.lead()
        order = operations.create_order(
            organization=self.org,
            actor=self.owner,
            reference="ORDER-NO-INFERENCE",
            buyer=self.party,
            date=self.day,
        )
        operations.link_order_lead(
            organization=self.org,
            actor=self.owner,
            order=order,
            lead=lead,
            expected_version=1,
            reason="Synthetic buyer confirmation",
            confirm_identity=True,
        )
        self.assertEqual(self.mapping(lead), (None, None, None, 1))
        context = self.context(lead)
        self.assertEqual(context["eligible_fields"], [])
        self.assertTrue(
            all(
                context["origins"][field]["kind"] == "unknown"
                for field in ("party", "product")
            )
        )
        self.assertEqual(context["history"], [])
        target = self.target()
        before = self.entire_state()
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**self.proposal(lead, target))
        self.assertEqual(self.entire_state(), before)
        self.assertEqual(self.mapping(lead), (None, None, None, 1))
        self.assertFalse(
            LeadIdentityChange.objects.filter(organization=self.org, lead=lead).exists()
        )

    def test_08_detach_relink_pic_and_contact_preserve_first_origin_and_stale_old_view(
        self,
    ):
        lead, source = self.inferred()
        old = self.proposal(lead, self.target())
        lead = correct_lead_invoice(
            organization=self.org,
            actor=self.finance,
            lead=lead,
            invoice=None,
            expected_version=lead.version,
            reason="Synthetic wrong link detached",
        )
        detached = self.context(lead)
        self.assertEqual(set(detached["eligible_fields"]), {"party", "product"})
        self.assertEqual(self.origin_source(detached, "party"), source.pk)
        equivalent = self.issued(number="EQUIVALENT-LINK")
        lead = services.link_invoice(
            organization=self.org,
            actor=self.finance,
            lead=lead,
            invoice=equivalent,
            expected_version=lead.version,
            confirm_identity=True,
        )
        replacement_pic = services.create_member(
            organization=self.org,
            actor=self.owner,
            name="Synthetic replacement PIC",
            discipline="sales",
        )
        lead = workflow.assign_lead(
            organization=self.org,
            actor=self.owner,
            lead=lead,
            member=replacement_pic,
            expected_version=lead.version,
            reason="Synthetic reassignment",
        )
        workflow.record_contact(
            organization=self.org,
            actor=self.owner,
            lead=lead,
            expected_version=lead.version,
            occurred_at=self.received + timedelta(hours=1),
            method="whatsapp",
            outcome="no_answer",
            summary="Synthetic follow up",
            evidence="Synthetic contact reference",
            responsibility_reason="Synthetic owner checked PIC assignment",
        )
        current = self.context(lead)
        self.assertEqual(set(current["eligible_fields"]), {"party", "product"})
        for field in ("party", "product"):
            self.assertEqual(self.origin_source(current, field), source.pk)
        self.assertNotEqual(current["fingerprint"], old["expected_fingerprint"])
        before = self.entire_state()
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**old)
        self.assertEqual(self.entire_state(), before)
        metadata = self.metadata(lead)
        recover_invoice_identity(**self.proposal(lead, old["target_invoice"]))
        self.assertEqual(self.metadata(lead), metadata)
        self.assertEqual(self.origin_source(self.context(lead), "party"), source.pk)

    def test_09_reset_then_normal_link_appends_generation_without_fork_or_lost_first_source(
        self,
    ):
        lead, source = self.inferred()
        first_ids = self.history_ids(self.context(lead))
        reset = recover_invoice_identity(**self.proposal(lead))
        target = self.target()
        lead.refresh_from_db()
        lead = services.link_invoice(
            organization=self.org,
            actor=self.finance,
            lead=lead,
            invoice=target,
            expected_version=lead.version,
            confirm_identity=True,
        )
        self.assertEqual(
            self.mapping(lead), (target.party_id, target.product_id, target.pk, 4)
        )
        context = self.context(lead)
        ids = self.history_ids(context)
        self.assertEqual(ids[:2], first_ids + [reset.pk])
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        for previous, following in zip(context["history"], context["history"][1:]):
            self.assertEqual(following.previous_id, previous.pk)
        for field in ("party", "product"):
            self.assertEqual(self.origin_source(context, field), source.pk)
        recover_invoice_identity(
            **self.proposal(lead, request_key="RESET-SECOND-GENERATION")
        )
        self.assertEqual(self.mapping(lead), (None, None, None, 5))
        self.assertEqual(len(self.history_ids(self.context(lead))), 4)

    def test_10_authorization_rechecks_roles_inactive_accounts_and_revoked_membership(
        self,
    ):
        lead, source = self.inferred()
        target = self.target()
        inactive = get_user_model().objects.create_user(
            "identity-draft-inactive", is_active=False
        )
        Membership.objects.create(organization=self.org, user=inactive, role="finance")
        revoked = get_user_model().objects.create_user("identity-draft-revoked")
        Membership.all_objects.create(
            organization=self.org, user=revoked, role="owner", is_active=False
        )
        for actor in (
            None,
            self.marketing,
            self.auditor,
            self.director,
            inactive,
            revoked,
            self.foreign_owner,
        ):
            with self.subTest(actor=getattr(actor, "username", None)):
                before = self.entire_state()
                with self.assertRaises(PermissionDenied):
                    recover_invoice_identity(**self.proposal(lead, target, actor=actor))
                self.assertEqual(self.entire_state(), before)
        proposal = self.proposal(lead, target)
        Membership.all_objects.filter(organization=self.org, user=self.finance).update(
            is_active=False
        )
        before = self.entire_state()
        with self.assertRaises(PermissionDenied):
            recover_invoice_identity(**proposal)
        self.assertEqual(self.entire_state(), before)
        decision = recover_invoice_identity(
            **self.proposal(lead, target, actor=self.owner)
        )
        self.assertEqual(decision.created_by_id, self.owner.pk)
        self.assertEqual(self.origin_source(self.context(lead), "party"), source.pk)

    def test_11_cross_organization_lead_invoice_and_context_are_rejected_without_writes(
        self,
    ):
        lead, source = self.inferred()
        foreign_party = Party.objects.create(
            organization=self.other, name="Synthetic foreign buyer", kind="reseller"
        )
        foreign_product = Product.objects.create(
            organization=self.other,
            code="FOREIGN",
            name="Synthetic foreign product",
            kind="itp",
            default_price="500000",
        )
        foreign_invoice = self.issued(
            organization=self.other,
            actor=self.foreign_owner,
            party=foreign_party,
            product=foreign_product,
        )
        local_target = self.target()
        for overrides in (
            {"target_invoice": foreign_invoice},
            {"organization": self.other, "actor": self.foreign_owner},
        ):
            with self.subTest(overrides=overrides):
                before = self.entire_state()
                foreign_before = self.entire_state(self.other)
                with self.assertRaises(ValidationError):
                    recover_invoice_identity(
                        **self.proposal(lead, local_target, **overrides)
                    )
                self.assertEqual(self.entire_state(), before)
                self.assertEqual(self.entire_state(self.other), foreign_before)
                self.assertEqual(
                    self.mapping(lead),
                    (source.party_id, source.product_id, source.pk, 2),
                )
                self.assertEqual(len(self.history_ids(self.context(lead))), 1)
        before = self.entire_state()
        with self.assertRaises(ValidationError):
            identity_recovery_context(organization=self.other, lead=lead)
        self.assertEqual(self.entire_state(), before)

    def test_12_stale_version_fingerprint_and_malformed_confirmations_do_not_consume_request(
        self,
    ):
        lead, source = self.inferred()
        target = self.target()
        original = self.proposal(lead, target)
        lead = services.update_lead(
            organization=self.org,
            actor=self.owner,
            lead=lead,
            expected_version=lead.version,
            notes="Synthetic source reviewed later",
            responsibility_reason="Synthetic Owner help after checking the current Marketing PIC",
        )
        proposals = [original]
        for changes in (
            {"expected_version": True},
            {"expected_version": "3"},
            {"expected_fingerprint": "0" * 64},
            {"expected_fingerprint": None},
            {"confirm_identity": False},
            {"confirm_identity": "true"},
            {"action": "replace", "target_invoice": None},
            {"action": "reset", "target_invoice": target},
            {"action": ""},
            {"reason": ""},
            {"evidence": ""},
        ):
            proposals.append(self.proposal(lead, target, **changes))
        for payload in proposals:
            with self.subTest(payload=payload):
                before = self.entire_state()
                with self.assertRaises(ValidationError):
                    recover_invoice_identity(**payload)
                self.assertEqual(self.entire_state(), before)
        decision = recover_invoice_identity(**self.proposal(lead, target))
        self.assertEqual(decision.request_key, original["request_key"])
        self.assertEqual(self.mapping(lead)[-1], 4)

    def test_13_cancelled_historical_source_stays_origin_but_invalid_or_taken_target_rejects(
        self,
    ):
        lead, source = self.inferred()
        valid = self.target()
        reviewed = self.proposal(lead, valid)
        finance.cancel_invoice(
            organization=self.org,
            actor=self.owner,
            invoice=source,
            date=self.day,
            reason="Synthetic original invoice cancelled",
        )
        context = self.context(lead)
        self.assertEqual(set(context["eligible_fields"]), {"party", "product"})
        self.assertEqual(self.origin_source(context, "party"), source.pk)
        before = self.entire_state()
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**reviewed)
        self.assertEqual(self.entire_state(), before)
        draft = self.invoice(party=self.party_b, product=self.product_b)
        cancelled = self.target()
        after_view = self.proposal(lead, cancelled)
        finance.cancel_invoice(
            organization=self.org,
            actor=self.owner,
            invoice=cancelled,
            date=self.day,
            reason="Synthetic target cancelled after review",
        )
        prior = self.target(
            date=self.day - timedelta(days=2), service_date=self.day - timedelta(days=2)
        )
        taken = self.target()
        other_lead = self.lead(party=self.party_b, product=self.product_b)
        services.link_invoice(
            organization=self.org,
            actor=self.finance,
            lead=other_lead,
            invoice=taken,
            expected_version=1,
        )
        for payload in (
            after_view,
            self.proposal(lead, draft),
            self.proposal(lead, prior),
            self.proposal(lead, taken),
        ):
            with self.subTest(target=getattr(payload["target_invoice"], "pk", None)):
                before = self.entire_state()
                with self.assertRaises(ValidationError):
                    recover_invoice_identity(**payload)
                self.assertEqual(self.entire_state(), before)
        recover_invoice_identity(**self.proposal(lead, valid))
        self.assertEqual(
            self.mapping(lead)[:3], (valid.party_id, valid.product_id, valid.pk)
        )
        self.assertEqual(self.origin_source(self.context(lead), "party"), source.pk)

    def test_14_order_dependency_survives_direct_detach_and_requires_explicit_order_correction(
        self,
    ):
        lead, source = self.inferred()
        target = self.target()
        viewed = self.proposal(lead, target)
        order = operations.create_order(
            organization=self.org,
            actor=self.owner,
            reference="SOURCE-ORDER",
            buyer=self.party,
            date=self.day,
        )
        line = operations.add_order_line(
            organization=self.org,
            actor=self.owner,
            order=order,
            product=self.product,
            quantity=1,
        )
        operations.link_order_invoice(
            organization=self.org,
            actor=self.owner,
            line=line,
            invoice=source,
            reason="Synthetic existing issued source checked",
        )
        order = operations.link_order_lead(
            organization=self.org,
            actor=self.owner,
            order=order,
            lead=lead,
            expected_version=1,
            reason="Synthetic Order attribution checked",
        )
        before = self.entire_state()
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**viewed)
        self.assertEqual(self.entire_state(), before)
        lead = correct_lead_invoice(
            organization=self.org,
            actor=self.finance,
            lead=lead,
            invoice=None,
            expected_version=lead.version,
            reason="Synthetic direct invoice detached",
        )
        order.refresh_from_db()
        self.assertEqual(order.lead_id, lead.pk)
        self.assertEqual(
            invoice_attribution(organization=self.org)["by_lead"][lead.pk], [source.pk]
        )
        self.assertTrue(self.context(lead)["blockers"])
        documents = self.preserved_documents()
        for payload in (self.proposal(lead, target), self.proposal(lead)):
            before = self.entire_state()
            with self.assertRaises(ValidationError):
                recover_invoice_identity(**payload)
            self.assertEqual(self.entire_state(), before)
            self.assertEqual(self.preserved_documents(), documents)
        operations.link_order_lead(
            organization=self.org,
            actor=self.owner,
            order=order,
            lead=None,
            expected_version=order.version,
            reason="Synthetic explicit mistaken Order attribution corrected",
        )
        documents = self.preserved_documents()
        recover_invoice_identity(**self.proposal(lead, target))
        self.assertEqual(self.preserved_documents(), documents)
        line.refresh_from_db()
        self.assertEqual(
            (line.order_id, line.product_id, line.invoice_id),
            (order.pk, self.product.pk, source.pk),
        )

    def test_15_exact_replay_precedes_freshness_but_every_changed_payload_conflicts(
        self,
    ):
        lead, source = self.inferred()
        target, another = self.target(), self.target()
        payload = self.proposal(lead, target)
        accepted = recover_invoice_identity(**payload)
        lead = services.update_lead(
            organization=self.org,
            actor=self.owner,
            lead=lead,
            expected_version=3,
            notes="Synthetic newer staff note",
            responsibility_reason="Synthetic Owner help after checking the current Marketing PIC",
        )
        before = self.entire_state()
        replay = recover_invoice_identity(**payload)
        self.assertEqual(replay.pk, accepted.pk)
        self.assertEqual(self.entire_state(), before)
        conflicts = (
            {"actor": self.owner},
            {"target_invoice": another},
            {"action": "reset", "target_invoice": None},
            {"expected_version": 4},
            {"expected_fingerprint": self.context(lead)["fingerprint"]},
            {"reason": "Different reason"},
            {"evidence": "Different evidence"},
            {"confirm_identity": False},
            {"confirm_legacy_source": True},
        )
        for changes in conflicts:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                recover_invoice_identity(**{**payload, **changes})
            self.assertEqual(self.entire_state(), before)
        other_lead = self.lead()
        foreign_lead_payload = {**payload, "lead": other_lead}
        before = self.entire_state()
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**foreign_lead_payload)
        self.assertEqual(self.entire_state(), before)

    def test_16_origin_and_recovery_roll_back_if_history_or_audit_persistence_fails(
        self,
    ):
        # Deliberate database write-boundary fault injection. No private parser or
        # chain helper is mocked; the full public command executes up to storage.
        for model in (LeadIdentityChange, AuditEvent):
            lead, invoice = self.lead(), self.issued()
            before = self.entire_state()
            with (
                self.subTest(command="link", failed=model.__name__),
                patch.object(
                    model,
                    "save",
                    side_effect=ValidationError("Synthetic storage unavailable"),
                ),
                self.assertRaises(ValidationError),
            ):
                services.link_invoice(
                    organization=self.org,
                    actor=self.finance,
                    lead=lead,
                    invoice=invoice,
                    expected_version=1,
                    confirm_identity=True,
                )
            self.assertEqual(self.entire_state(), before)
            self.assertEqual(self.mapping(lead), (None, None, None, 1))
            self.assert_no_privileges(lead)
        lead, source = self.inferred()
        target = self.target()
        payload = self.proposal(lead, target)
        for model in (LeadIdentityChange, AuditEvent):
            before = self.entire_state()
            with (
                self.subTest(command="recovery", failed=model.__name__),
                patch.object(
                    model,
                    "save",
                    side_effect=ValidationError("Synthetic storage unavailable"),
                ),
                self.assertRaises(ValidationError),
            ):
                recover_invoice_identity(**payload)
            self.assertEqual(self.entire_state(), before)
            self.assertEqual(
                self.mapping(lead), (source.party_id, source.product_id, source.pk, 2)
            )
            self.assert_no_privileges(lead)
        accepted = recover_invoice_identity(**payload)
        self.assertEqual(accepted.request_key, payload["request_key"])

    def test_17_same_values_without_kpi_or_order_change_local_advice_identity_and_roundtrip_cannot_revive_it(
        self,
    ):
        def advice_snapshot():
            snapshot = marketing_snapshot(
                organization=self.org, start=self.day - timedelta(days=1), end=self.day
            )
            return {
                **snapshot,
                "management": management_snapshot(
                    organization=self.org,
                    start=snapshot["period"]["start"],
                    end=snapshot["period"]["end"],
                ),
            }

        lead, source = self.inferred()
        equivalent = self.issued(number="EQUAL-VALUES-REPLACEMENT")
        before = advice_snapshot()
        self.assertEqual(before["management"]["target_count"], 0)
        self.assertEqual(before["management"]["commercial"]["cohort_orders"], 0)
        original_fingerprint = self.context(lead)["fingerprint"]
        lead = correct_lead_invoice(
            organization=self.org,
            actor=self.finance,
            lead=lead,
            invoice=source,
            expected_version=2,
            reason="Synthetic same-value attribution confirmed",
        )
        same = advice_snapshot()
        self.assertEqual(same["summary"], before["summary"])
        self.assertNotEqual(advice_identity(same), advice_identity(before))
        self.assertNotEqual(
            same["management"]["fingerprint"], before["management"]["fingerprint"]
        )
        recover_invoice_identity(
            **self.proposal(lead, equivalent, request_key="EQUAL-VALUES-RECOVERY")
        )
        equal = advice_snapshot()
        self.assertEqual(equal["summary"], before["summary"])
        self.assertNotEqual(advice_identity(equal), advice_identity(same))
        self.assertNotEqual(advice_identity(equal), advice_identity(before))
        self.assertEqual(self.origin_source(self.context(lead), "party"), source.pk)
        recover_invoice_identity(**self.proposal(lead))
        lead.refresh_from_db()
        lead = services.link_invoice(
            organization=self.org,
            actor=self.finance,
            lead=lead,
            invoice=source,
            expected_version=lead.version,
            confirm_identity=True,
        )
        final = advice_snapshot()
        self.assertEqual(final["summary"], before["summary"])
        self.assertNotEqual(self.context(lead)["fingerprint"], original_fingerprint)
        self.assertNotEqual(advice_identity(final), advice_identity(before))
        self.assertNotEqual(advice_identity(final), advice_identity(same))
        self.assertNotEqual(advice_identity(final), advice_identity(equal))
        # Serialize the existing provider boundary locally; no provider/network call.
        body = _outbound_body(
            build_recommendations(final),
            final,
            {"model": "vendor/test-model", "provider": "test-provider"},
        )
        encoded = body.decode() if isinstance(body, bytes) else json.dumps(body)
        for private in (
            "Synthetic wrong source checked",
            "Synthetic archive/reference.txt",
            self.party.name,
            self.product.name,
            "source_invoice_id",
            "request_key",
            "viewed_fingerprint",
            "legacy_snapshot",
        ):
            self.assertNotIn(private, encoded)

    def test_18_history_rejects_orm_rewriting_and_every_returned_lead_loses_mutation_flags(
        self,
    ):
        lead, source = self.inferred()
        self.assert_no_privileges(lead)
        source_change = self.context(lead)["history"][0]
        decision = recover_invoice_identity(**self.proposal(lead, self.target()))
        self.assert_no_privileges(decision.lead)
        before = self.entire_state()
        for row in (source_change, decision):
            row.reason = "Forged historical reason"
            with self.assertRaises(ValidationError):
                row.save()
            with self.assertRaises(ValidationError):
                LeadIdentityChange.objects.filter(pk=row.pk).update(
                    reason="Forged bulk reason"
                )
            with self.assertRaises(ValidationError):
                LeadIdentityChange.objects.bulk_update([row], ["reason"])
            row.refresh_from_db()
        # Leaf row has no successor: these rejects must come from immutability.
        with self.assertRaises(ValidationError):
            decision.delete()
        with self.assertRaises(ValidationError):
            LeadIdentityChange.objects.filter(pk=decision.pk).delete()
        values = {
            field.attname: getattr(decision, field.attname)
            for field in decision._meta.concrete_fields
            if not field.primary_key
        }
        with self.assertRaises(ValidationError):
            LeadIdentityChange.objects.create(**values)
        with self.assertRaises(ValidationError):
            LeadIdentityChange.objects.bulk_create([LeadIdentityChange(**values)])
        self.assertEqual(self.entire_state(), before)
        lead.refresh_from_db()
        for field, value in (
            ("party", self.party),
            ("product", self.product),
            ("invoice", source),
        ):
            setattr(lead, field, value)
            with self.assertRaises(ValidationError):
                lead.save()
            self.assert_no_privileges(lead)
            lead.refresh_from_db()
        with self.assertRaises(ValidationError):
            Lead.objects.filter(pk=lead.pk).update(party=self.party)
        lead.party = self.party
        with self.assertRaises(ValidationError):
            Lead.objects.bulk_update([lead], ["party"])
        self.assertEqual(self.entire_state(), before)

    def test_19_forged_well_shaped_narrow_proof_cannot_unlock_manual_or_campaign_identity(
        self,
    ):
        campaign = services.create_campaign(
            organization=self.org,
            actor=self.marketing,
            name="Synthetic protected proof campaign",
            channel="meta_ads",
            product=self.product,
        )
        for initial in (
            {"party": self.party, "product": self.product},
            {"party": self.party},
            {"campaign": campaign},
        ):
            with self.subTest(initial=initial):
                lead, source = self.inferred(**initial)
                target = self.target()
                before_mapping = self.mapping(lead)
                before = self.entire_state()
                lead.party, lead.product, lead.invoice, lead.version = (
                    target.party,
                    target.product,
                    target,
                    lead.version + 1,
                )
                lead._service_write = lead._invoice_link = True
                lead._identity_recovery_write = {
                    "before": before_mapping,
                    "after": (
                        target.party_id,
                        target.product_id,
                        target.pk,
                        lead.version,
                    ),
                    "eligible_fields": ("party", "product"),
                }
                with self.assertRaises(ValidationError):
                    lead.save()
                self.assert_no_privileges(lead)
                self.assertEqual(self.entire_state(), before)
                self.assertEqual(self.mapping(lead), before_mapping)
