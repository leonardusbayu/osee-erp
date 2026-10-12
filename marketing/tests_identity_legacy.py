"""Strict recovery of independently specified pre-feature identity history.

The fixture crosses the historical migration seam, as reviewed by root. It
never clears/rewrites the guarded new history model. Lead creation and Finance
issuance use genuine public commands, including original creation/issue audit,
posted Journal and DomainEvent. Only the old marketing link/detach/relink is
specified using the pre-feature Lead model and the exact legacy JSON contract.
Malformed/tampered AuditEvent writes below are deliberate adversarial fixtures;
AuditEvent is the mutable historical source this feature must review carefully.

The copied digest binds the reviewed snapshot. It does not prove legacy audit
content was immutable before adoption. PostgreSQL races and HTTP/browser/staff
acceptance remain separate root-owned checks.
"""

from copy import deepcopy
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import JSONField, Value
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, DomainEvent, Membership, Organization
from finance import services as finance
from finance import tests_remediation as finance_fixtures
from finance.models import Invoice, Journal, Party, Product
from marketing import services
from marketing.identity_recovery import (
    identity_recovery_context,
    recover_invoice_identity,
)
from marketing.models import Lead, LeadIdentityChange
from operations import services as operations


@override_settings(
    MARKETING_AI_PRIVATE_AGGREGATES_ENABLED=False, DIRECTOR_AI_ENDPOINT_APPROVED=False
)
class LegacyInvoiceIdentityAcceptanceTests(TransactionTestCase):
    invoice = finance_fixtures.FinanceRemediationTests.invoice
    movement = finance_fixtures.FinanceRemediationTests.movement
    previous_marketing = "0009_ad_observation_recovery"

    def setUp(self):
        executor = MigrationExecutor(connection)
        self.restore_targets = executor.loader.graph.leaf_nodes()
        marketing_leaf_targets = [
            target for target in self.restore_targets if target[0] == "marketing"
        ]
        self.assertTrue(
            marketing_leaf_targets,
            "the current migration graph must include a Marketing leaf",
        )
        self.old_targets = [
            (app, self.previous_marketing if app == "marketing" else name)
            for app, name in self.restore_targets
        ]
        self.addCleanup(self.restore_schema)
        executor.migrate(self.old_targets)
        self.old_apps = executor.loader.project_state(self.old_targets).apps
        with self.assertRaises(LookupError):
            self.old_apps.get_model("marketing", "LeadIdentityChange")
        finance_fixtures.FinanceRemediationTests.setUp(self)
        self.owner = self.actor
        self.finance = get_user_model().objects.create_user(
            "identity-legacy-current-finance"
        )
        self.historical_writer = get_user_model().objects.create_user(
            "identity-legacy-original-finance"
        )
        for user in (self.finance, self.historical_writer):
            Membership.objects.create(organization=self.org, user=user, role="finance")
        self.other = Organization.objects.create(
            name="Synthetic unrelated legacy company"
        )
        self.party_b = Party.objects.create(
            organization=self.org,
            name="Synthetic legacy correct buyer",
            kind="reseller",
        )
        self.product_b = Product.objects.create(
            organization=self.org,
            code="LEGACY-CORRECT",
            name="Synthetic legacy correct product",
            kind="itp",
            default_price="500000",
        )
        self.received = timezone.make_aware(
            datetime.combine(self.day - timedelta(days=1), time(10))
        )

    def restore_schema(self):
        MigrationExecutor(connection).migrate(self.restore_targets)

    def issued(self, **changes):
        invoice = self.invoice(**changes)
        return finance.issue_invoice(
            organization=invoice.organization, actor=self.owner, invoice=invoice
        )

    def legacy_link(self, old_lead, invoice, *, confirmed):
        before = {"party_id": old_lead.party_id, "product_id": old_lead.product_id}
        if old_lead.party_id is None:
            old_lead.party_id = invoice.party_id
        if old_lead.product_id is None:
            old_lead.product_id = invoice.product_id
        old_lead.invoice_id = invoice.pk
        old_lead.version += 1
        old_lead.save(update_fields=["party", "product", "invoice", "version"])
        return AuditEvent.objects.create(
            organization=self.org,
            actor=self.historical_writer,
            action="marketing.lead.invoice_linked",
            object_type="Lead",
            object_id=str(old_lead.pk),
            detail={
                "invoice_id": invoice.pk,
                "version": old_lead.version,
                "basis": "finance_invoice_reference",
                "before_mapping": before,
                "after_mapping": {
                    "party_id": old_lead.party_id,
                    "product_id": old_lead.product_id,
                },
                "identity_confirmed": confirmed,
            },
        )

    def fixture(
        self,
        *,
        manual_party=False,
        manual_product=False,
        campaign_product=False,
        detached=False,
        relink=False,
        issued_source=True,
        cancel_draft=False,
        unrecorded_mapping=False,
        repeated_inference=False,
    ):
        """Build once per test while the immutable table is absent, then migrate."""
        campaign = None
        if campaign_product:
            campaign = services.create_campaign(
                organization=self.org,
                actor=self.owner,
                name="Synthetic historical campaign",
                channel="meta_ads",
                product=self.product,
            )
        self.lead = services.create_lead(
            organization=self.org,
            actor=self.owner,
            reference="LEGACY-IDENTITY",
            name="Synthetic historical opportunity",
            received_at=self.received,
            campaign=campaign,
            channel="meta_ads",
            party=self.party if manual_party else None,
            product=self.product if manual_product else None,
            segment="reseller",
            region="Jakarta",
            notes="Original historical staff notes",
        )
        self.created_event = AuditEvent.objects.get(
            organization=self.org,
            action="marketing.lead.created",
            object_id=str(self.lead.pk),
        )
        source_draft = self.invoice(number="LEGACY-SOURCE")
        self.source = (
            finance.issue_invoice(
                organization=self.org, actor=self.owner, invoice=source_draft
            )
            if issued_source
            else source_draft
        )
        if cancel_draft:
            self.source = finance.cancel_invoice(
                organization=self.org,
                actor=self.owner,
                invoice=self.source,
                date=self.day,
                reason="Synthetic draft cancelled before impossible legacy link",
            )
        old_lead = self.old_apps.get_model("marketing", "Lead").objects.get(
            pk=self.lead.pk
        )
        self.link_event = self.legacy_link(old_lead, self.source, confirmed=True)
        self.correction_event = None
        if detached or relink:
            old_lead.invoice_id = None
            old_lead.version += 1
            old_lead.save(update_fields=["invoice", "version"])
            self.correction_event = AuditEvent.objects.create(
                organization=self.org,
                actor=self.historical_writer,
                action="marketing.lead.invoice_corrected",
                object_type="Lead",
                object_id=str(old_lead.pk),
                detail={
                    "before_invoice_id": self.source.pk,
                    "after_invoice_id": None,
                    "reason": "Synthetic old attribution detached",
                    "version": old_lead.version,
                },
            )
        self.relinked = None
        if relink:
            self.relinked = self.issued(number="LEGACY-KNOWN-RELINK")
            self.legacy_link(old_lead, self.relinked, confirmed=False)
        if repeated_inference:
            # Deliberate impossible pre-feature transition: no old command could
            # clear known identity. Both issue histories remain real and typed.
            second = self.issued(
                number="LEGACY-SECOND-INFERENCE",
                party=self.party_b,
                product=self.product_b,
            )
            old_lead.party_id = old_lead.product_id = old_lead.invoice_id = None
            old_lead.save(update_fields=["party", "product", "invoice"])
            self.legacy_link(old_lead, second, confirmed=True)
        if unrecorded_mapping:
            old_lead.party_id = self.party_b.pk
            old_lead.save(update_fields=["party"])
        self.target = self.issued(
            number="LEGACY-TARGET",
            party=self.party_b,
            product=self.product if campaign_product else self.product_b,
        )
        self.restore_schema()
        self.lead = Lead.objects.get(pk=old_lead.pk)
        self.assertFalse(
            LeadIdentityChange.objects.filter(
                organization=self.org, lead=self.lead
            ).exists()
        )

    def context(self):
        return identity_recovery_context(organization=self.org, lead=self.lead)

    def proposal(self, **changes):
        self.lead.refresh_from_db()
        return {
            "organization": self.org,
            "actor": self.finance,
            "lead": self.lead,
            "action": "replace",
            "target_invoice": self.target,
            "expected_version": self.lead.version,
            "expected_fingerprint": self.context()["fingerprint"],
            "request_key": "LEGACY-REVIEW-1",
            "confirm_identity": True,
            "confirm_legacy_source": True,
            "reason": "Synthetic explicit original-source review",
            "evidence": "Synthetic original archive/link-evidence.json",
            **changes,
        }

    def state(self):
        result = {}
        for label in ("core", "marketing", "finance", "operations", "taxes"):
            for model in apps.get_app_config(label).get_models():
                names = [field.attname for field in model._meta.concrete_fields]
                if "organization_id" in names:
                    result[model._meta.label] = list(
                        model.objects.filter(organization=self.org)
                        .order_by("pk")
                        .values(*names)
                    )
        return result

    def documents(self):
        return {
            name: rows
            for name, rows in self.state().items()
            if name.split(".", 1)[0] in {"finance", "operations", "taxes"}
        }

    def rewrite_detail(self, detail, event=None):
        # Value(JSONField) deliberately stores JSON null instead of SQL NULL.
        AuditEvent.objects.filter(pk=(event or self.link_event).pk).update(
            detail=Value(deepcopy(detail), output_field=JSONField())
        )

    def assert_blocked_without_writes(self):
        before = self.state()
        context = self.context()
        self.assertTrue(context["blockers"])
        self.assertEqual(self.state(), before)
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**self.proposal())
        self.assertEqual(self.state(), before)
        self.assertFalse(
            LeadIdentityChange.objects.filter(
                organization=self.org, lead=self.lead
            ).exists()
        )

    def test_migration_and_read_context_never_adopt_plain_legacy_audits_automatically(
        self,
    ):
        self.fixture()
        self.assertTrue(
            Journal.objects.filter(
                organization=self.org, source_key=f"invoice:{self.source.pk}:issue"
            ).exists()
        )
        issued = AuditEvent.objects.get(
            organization=self.org,
            action="finance.invoice.issued",
            object_id=str(self.source.pk),
        )
        self.assertLessEqual(self.created_event.created_at, self.link_event.created_at)
        self.assertLessEqual(issued.created_at, self.link_event.created_at)
        before = self.state()
        context = self.context()
        self.assertEqual(context, self.context())
        self.assertEqual(self.state(), before)
        self.assertEqual(set(context["eligible_fields"]), {"party", "product"})
        self.assertTrue(context["legacy_review_required"])
        self.assertEqual(context["history"], [])
        for field in ("party", "product"):
            self.assertEqual(
                context["origins"][field]["source_invoice_id"], self.source.pk
            )

    def test_finance_explicit_source_review_copies_evidence_and_changes_only_inferred_identity(
        self,
    ):
        self.fixture()
        audits = list(
            AuditEvent.objects.filter(organization=self.org).order_by("pk").values()
        )
        documents = self.documents()
        payload = self.proposal()
        decision = recover_invoice_identity(**payload)
        self.lead.refresh_from_db()
        self.assertEqual(
            (
                self.lead.party_id,
                self.lead.product_id,
                self.lead.invoice_id,
                self.lead.version,
            ),
            (self.target.party_id, self.target.product_id, self.target.pk, 3),
        )
        self.assertEqual(self.documents(), documents)
        self.assertEqual(
            list(
                AuditEvent.objects.filter(pk__in=[row["id"] for row in audits])
                .order_by("pk")
                .values()
            ),
            audits,
        )
        context = self.context()
        self.assertFalse(context["legacy_review_required"])
        self.assertEqual(
            [row.kind for row in context["history"]], ["legacy_adoption", "replace"]
        )
        adoption, recovered = context["history"]
        self.assertEqual(recovered.pk, decision.pk)
        self.assertEqual(recovered.previous_id, adoption.pk)
        self.assertEqual((adoption.before_version, adoption.after_version), (2, 2))
        self.assertEqual(
            (adoption.before_party_id, adoption.after_party_id),
            (self.source.party_id, self.source.party_id),
        )
        self.assertEqual(
            (
                adoption.created_by_id,
                adoption.reason,
                adoption.evidence,
                adoption.viewed_fingerprint,
            ),
            (
                self.finance.pk,
                payload["reason"],
                payload["evidence"],
                payload["expected_fingerprint"],
            ),
        )
        self.assertRegex(adoption.snapshot_digest, r"^[a-f0-9]{64}$")
        self.assertIn("legacy_basis", adoption.origin_snapshot)
        self.assertIn("legacy_evidence", adoption.origin_snapshot["legacy_basis"])
        expected_evidence = [
            {
                "id": event.pk,
                "organization_id": event.organization_id,
                "actor_id": event.actor_id,
                "action": event.action,
                "object_type": event.object_type,
                "object_id": event.object_id,
                "detail": deepcopy(event.detail),
                "created_at": str(event.created_at),
            }
            for event in (self.created_event, self.link_event)
        ]
        self.assertEqual(
            adoption.origin_snapshot["legacy_basis"]["legacy_evidence"],
            expected_evidence,
        )
        source_proof = adoption.origin_snapshot["legacy_basis"]["source_evidence"][0]
        self.assertEqual(source_proof["invoice"]["id"], self.source.pk)
        self.assertEqual(
            source_proof["issue_audit"]["action"], "finance.invoice.issued"
        )
        self.assertEqual(source_proof["issue_domain"]["topic"], "invoice.issued")
        self.assertTrue(adoption.source_snapshot)
        stored = deepcopy(adoption.origin_snapshot)
        # A mutable old mirror may change later; the accepted immutable evidence
        # remains the reviewed snapshot and original source remains visible.
        self.rewrite_detail({"tampered_after_adoption": True})
        adoption.refresh_from_db()
        self.assertEqual(adoption.origin_snapshot, stored)
        self.assertEqual(
            self.context()["origins"]["party"]["source_invoice_id"], self.source.pk
        )

    def test_owner_legacy_reset_preserves_manual_buyer_and_records_visible_review(self):
        self.fixture(manual_party=True)
        context = self.context()
        self.assertEqual(set(context["eligible_fields"]), {"product"})
        documents = self.documents()
        recover_invoice_identity(
            **self.proposal(actor=self.owner, action="reset", target_invoice=None)
        )
        self.lead.refresh_from_db()
        self.assertEqual(
            (self.lead.party_id, self.lead.product_id, self.lead.invoice_id),
            (self.party.pk, None, None),
        )
        self.assertEqual(self.documents(), documents)
        history = self.context()["history"]
        self.assertEqual([row.kind for row in history], ["legacy_adoption", "reset"])
        self.assertEqual(history[0].created_by_id, self.owner.pk)
        self.assertEqual(self.context()["origins"]["party"]["kind"], "protected")

    def test_legacy_campaign_product_stays_protected_after_reviewed_replacement(self):
        self.fixture(campaign_product=True)
        self.assertEqual(set(self.context()["eligible_fields"]), {"party"})
        campaign_id = self.lead.campaign_id
        recover_invoice_identity(**self.proposal())
        self.lead.refresh_from_db()
        self.assertEqual(
            (self.lead.party_id, self.lead.product_id, self.lead.campaign_id),
            (self.party_b.pk, self.product.pk, campaign_id),
        )
        self.assertEqual(self.context()["origins"]["product"]["kind"], "protected")

    def test_legacy_source_confirmation_is_explicit_typed_and_failed_attempts_do_not_consume_key(
        self,
    ):
        self.fixture()
        for confirmation in (False, None, "true", 1):
            with self.subTest(confirmation=confirmation):
                before = self.state()
                with self.assertRaises(ValidationError):
                    recover_invoice_identity(
                        **self.proposal(confirm_legacy_source=confirmation)
                    )
                self.assertEqual(self.state(), before)
        payload = self.proposal()
        payload.pop("confirm_legacy_source")
        before = self.state()
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**payload)
        self.assertEqual(self.state(), before)
        accepted = recover_invoice_identity(**self.proposal())
        self.assertEqual(accepted.request_key, "LEGACY-REVIEW-1")

    def test_nonobject_legacy_detail_is_blocked_without_coercion_or_raw_exception(self):
        self.fixture()
        for detail in (None, [], "{}", 1, True):
            with self.subTest(detail=detail):
                self.rewrite_detail(detail)
                self.assert_blocked_without_writes()

    def test_mapping_keys_nulls_and_schema_are_exact_not_falsy_or_permissively_trimmed(
        self,
    ):
        self.fixture()
        original = deepcopy(self.link_event.detail)
        malformed = []
        for name in ("before_mapping", "after_mapping"):
            for bad in (
                None,
                [],
                "{}",
                {"party_id": None},
                {"party_id": None, "product_id": None, "extra": None},
            ):
                malformed.append({**original, name: bad})
        for bad_null in ("", 0, False, [], {}):
            detail = deepcopy(original)
            detail["before_mapping"]["party_id"] = bad_null
            malformed.append(detail)
        for missing in original:
            detail = deepcopy(original)
            detail.pop(missing)
            malformed.append(detail)
        malformed.append({**original, "unrecognized_schema_key": "future"})
        for detail in malformed:
            with self.subTest(detail=detail):
                self.rewrite_detail(detail)
                self.assert_blocked_without_writes()

    def test_ids_and_versions_reject_bool_float_string_nonpositive_and_container_values(
        self,
    ):
        self.fixture()
        original = deepcopy(self.link_event.detail)
        for field in ("invoice_id", "version", "party_id", "product_id"):
            for value in (True, False, 0, -1, 1.0, "1", "", [], {}, None):
                detail = deepcopy(original)
                if field in {"party_id", "product_id"}:
                    detail["after_mapping"][field] = value
                else:
                    detail[field] = value
                with self.subTest(field=field, value=value):
                    self.rewrite_detail(detail)
                    self.assert_blocked_without_writes()

    def test_confirmation_and_basis_must_match_the_actual_legacy_inference_schema(self):
        self.fixture()
        original = deepcopy(self.link_event.detail)
        for field, values in (
            ("identity_confirmed", (False, None, "true", 1)),
            ("basis", (None, "", "order_reference", "finance_invoice_reference ")),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.rewrite_detail({**original, field: value})
                    self.assert_blocked_without_writes()

    def test_source_scope_object_type_and_canonical_object_id_cannot_be_rebound(self):
        self.fixture()
        original = {
            "organization_id": self.link_event.organization_id,
            "object_type": self.link_event.object_type,
            "object_id": self.link_event.object_id,
            "action": self.link_event.action,
        }
        variants = (
            {"organization_id": self.other.pk},
            {"object_type": "lead"},
            {"object_type": "Order"},
            {"object_id": f"0{self.lead.pk}"},
            {"object_id": f"{self.lead.pk}.0"},
            {"object_id": ""},
            {"action": "marketing.lead.invoice_linked.unrecognized"},
        )
        for changes in variants:
            with self.subTest(changes=changes):
                AuditEvent.objects.filter(pk=self.link_event.pk).update(**original)
                AuditEvent.objects.filter(pk=self.link_event.pk).update(**changes)
                self.assert_blocked_without_writes()
        AuditEvent.objects.filter(pk=self.link_event.pk).update(**original)
        foreign_party = Party.objects.create(
            organization=self.other, name="Synthetic foreign old buyer", kind="reseller"
        )
        foreign_product = Product.objects.create(
            organization=self.other,
            code="FOREIGN",
            name="Synthetic foreign old product",
            kind="itp",
            default_price="500000",
        )
        foreign_owner = get_user_model().objects.create_user(
            "identity-legacy-foreign-owner"
        )
        Membership.objects.create(
            organization=self.other, user=foreign_owner, role="owner"
        )
        foreign = finance.issue_invoice(
            organization=self.other,
            actor=foreign_owner,
            invoice=finance.create_invoice(
                organization=self.other,
                actor=foreign_owner,
                party=foreign_party,
                product=foreign_product,
                quantity=1,
                date=self.day,
                service_date=self.day,
            ),
        )
        detail = deepcopy(self.link_event.detail)
        detail["invoice_id"] = foreign.pk
        detail["after_mapping"] = {
            "party_id": foreign.party_id,
            "product_id": foreign.product_id,
        }
        self.rewrite_detail(detail)
        self.assert_blocked_without_writes()

    def test_creation_known_identity_cannot_be_forged_into_null_before_origin(self):
        self.fixture()
        detail = deepcopy(self.created_event.detail)
        detail["values"]["party"] = self.party.pk
        self.rewrite_detail(detail, self.created_event)
        self.assert_blocked_without_writes()

    def test_creation_snapshot_types_and_missing_keys_are_review_blockers(self):
        self.fixture()
        original = deepcopy(self.created_event.detail)
        malformed = [{**original, "values": None}, {**original, "values": []}]
        for field in ("party", "product"):
            missing = deepcopy(original)
            missing["values"].pop(field)
            malformed.append(missing)
            for value in (False, True, 0, -1, "1", 1.0):
                detail = deepcopy(original)
                detail["values"][field] = value
                malformed.append(detail)
        for detail in malformed:
            with self.subTest(detail=detail):
                self.rewrite_detail(detail, self.created_event)
                self.assert_blocked_without_writes()

    def test_missing_creation_origin_or_duplicate_origin_never_creates_guessed_adoption(
        self,
    ):
        self.fixture()
        for event in (self.created_event, self.link_event):
            original_type = event.object_type
            AuditEvent.objects.filter(pk=event.pk).update(
                object_type="Hidden adversarial source"
            )
            self.assert_blocked_without_writes()
            AuditEvent.objects.filter(pk=event.pk).update(object_type=original_type)
        duplicate = AuditEvent.objects.create(
            organization=self.org,
            actor=self.historical_writer,
            action=self.link_event.action,
            object_type="Lead",
            object_id=str(self.lead.pk),
            detail=deepcopy(self.link_event.detail),
        )
        self.assert_blocked_without_writes()
        duplicate.delete()
        self.assertFalse(LeadIdentityChange.objects.filter(lead=self.lead).exists())

    def test_effective_versions_and_recorded_chronology_must_describe_one_coherent_history(
        self,
    ):
        self.fixture(detached=True)
        original = deepcopy(self.correction_event.detail)
        for changes in (
            {"version": 2},
            {"version": 4},
            {"before_invoice_id": self.target.pk},
            {"after_invoice_id": False},
            {"before_invoice_id": str(self.source.pk)},
        ):
            with self.subTest(changes=changes):
                self.rewrite_detail({**original, **changes}, self.correction_event)
                self.assert_blocked_without_writes()
        self.rewrite_detail(original, self.correction_event)
        AuditEvent.objects.filter(pk=self.link_event.pk).update(
            created_at=self.created_event.created_at - timedelta(seconds=1)
        )
        self.assert_blocked_without_writes()

    def test_current_mapping_must_equal_reconstructed_source_state(self):
        self.fixture(unrecorded_mapping=True)
        self.assertEqual(self.lead.party_id, self.party_b.pk)
        self.assert_blocked_without_writes()

    def test_a_second_legacy_null_to_id_transition_without_a_supported_reset_is_ambiguous(
        self,
    ):
        self.fixture(repeated_inference=True)
        self.assertEqual(self.lead.version, 3)
        self.assertEqual(self.lead.party_id, self.party_b.pk)
        self.assert_blocked_without_writes()

    def test_detached_and_known_relinked_legacy_source_retains_the_first_inference(
        self,
    ):
        self.fixture(relink=True)
        context = self.context()
        self.assertEqual(self.lead.invoice_id, self.relinked.pk)
        self.assertEqual(set(context["eligible_fields"]), {"party", "product"})
        for field in ("party", "product"):
            self.assertEqual(
                context["origins"][field]["source_invoice_id"], self.source.pk
            )
        recover_invoice_identity(**self.proposal())
        for field in ("party", "product"):
            self.assertEqual(
                self.context()["origins"][field]["source_invoice_id"], self.source.pk
            )

    def test_legacy_audit_drift_after_view_is_stale_without_partial_adoption_or_consumed_key(
        self,
    ):
        self.fixture()
        payload = self.proposal()
        original_mapping = (
            self.lead.party_id,
            self.lead.product_id,
            self.lead.invoice_id,
            self.lead.version,
        )
        AuditEvent.objects.filter(pk=self.link_event.pk).update(actor=self.finance)
        self.assertNotEqual(
            self.context()["fingerprint"], payload["expected_fingerprint"]
        )
        before = self.state()
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**payload)
        self.assertEqual(self.state(), before)
        self.lead.refresh_from_db()
        self.assertEqual(
            (
                self.lead.party_id,
                self.lead.product_id,
                self.lead.invoice_id,
                self.lead.version,
            ),
            original_mapping,
        )
        accepted = recover_invoice_identity(**self.proposal())
        self.assertEqual(accepted.request_key, payload["request_key"])

    def test_order_dependency_after_legacy_view_blocks_save_without_partial_source_adoption(
        self,
    ):
        self.fixture()
        payload = self.proposal()
        order = operations.create_order(
            organization=self.org,
            actor=self.owner,
            reference="LEGACY-DEPENDENCY",
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
            invoice=self.source,
            reason="Synthetic actual legacy invoice checked",
        )
        order = operations.link_order_lead(
            organization=self.org,
            actor=self.owner,
            order=order,
            lead=self.lead,
            expected_version=1,
            reason="Synthetic actual Order attribution checked",
        )
        self.assertTrue(self.context()["blockers"])
        before = self.state()
        for values in (payload, self.proposal()):
            with self.assertRaises(ValidationError):
                recover_invoice_identity(**values)
            self.assertEqual(self.state(), before)
        self.assertFalse(LeadIdentityChange.objects.filter(lead=self.lead).exists())
        order.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(
            (order.lead_id, line.invoice_id, line.product_id),
            (self.lead.pk, self.source.pk, self.product.pk),
        )

    def test_adoption_and_recovery_are_one_transaction_when_history_or_audit_storage_fails(
        self,
    ):
        self.fixture()
        payload = self.proposal()
        for model in (LeadIdentityChange, AuditEvent):
            before = self.state()
            with (
                self.subTest(failed=model.__name__),
                patch.object(
                    model,
                    "save",
                    side_effect=ValidationError("Synthetic legacy storage unavailable"),
                ),
                self.assertRaises(ValidationError),
            ):
                recover_invoice_identity(**payload)
            self.assertEqual(self.state(), before)
            self.assertFalse(
                LeadIdentityChange.objects.filter(
                    organization=self.org, lead=self.lead
                ).exists()
            )
        accepted = recover_invoice_identity(**payload)
        self.assertEqual(accepted.request_key, payload["request_key"])

    def test_deleted_historical_actor_does_not_authorize_a_revoked_current_writer(self):
        self.fixture()
        self.historical_writer.delete()
        self.link_event.refresh_from_db()
        self.assertIsNone(self.link_event.actor_id)
        self.assertEqual(set(self.context()["eligible_fields"]), {"party", "product"})
        payload = self.proposal()
        Membership.all_objects.filter(organization=self.org, user=self.finance).update(
            is_active=False
        )
        before = self.state()
        with self.assertRaises(PermissionDenied):
            recover_invoice_identity(**payload)
        self.assertEqual(self.state(), before)
        decision = recover_invoice_identity(**self.proposal(actor=self.owner))
        self.assertEqual(decision.created_by_id, self.owner.pk)

    def test_cancelled_issued_legacy_source_retains_origin_and_requires_fresh_review(
        self,
    ):
        self.fixture()
        viewed = self.proposal()
        finance.cancel_invoice(
            organization=self.org,
            actor=self.owner,
            invoice=self.source,
            date=self.day,
            reason="Synthetic historically issued source later cancelled",
        )
        self.assertEqual(set(self.context()["eligible_fields"]), {"party", "product"})
        before = self.state()
        with self.assertRaises(ValidationError):
            recover_invoice_identity(**viewed)
        self.assertEqual(self.state(), before)
        recover_invoice_identity(**self.proposal())
        self.assertEqual(
            self.context()["origins"]["party"]["source_invoice_id"], self.source.pk
        )

    def test_cancelled_from_draft_has_no_historical_issue_proof_and_cannot_be_adopted(
        self,
    ):
        # Deliberately impossible old-link fixture: actual draft cancellation,
        # no fake issued status, no invented Journal or issue AuditEvent.
        self.fixture(issued_source=False, cancel_draft=True)
        self.assertEqual(Invoice.objects.get(pk=self.source.pk).status, "cancelled")
        self.assertFalse(
            Journal.objects.filter(
                organization=self.org, source_key=f"invoice:{self.source.pk}:issue"
            ).exists()
        )
        self.assertFalse(
            AuditEvent.objects.filter(
                organization=self.org,
                action="finance.invoice.issued",
                object_id=str(self.source.pk),
            ).exists()
        )
        self.assert_blocked_without_writes()

    def test_protected_only_legacy_attribution_review_detach_then_ordinary_relink_preserves_manual_values(
        self,
    ):
        self.fixture(manual_party=True, manual_product=True)
        self.client.force_login(self.finance)
        self.assertEqual(self.context()["eligible_fields"], [])
        self.assertTrue(self.context()["legacy_review_required"])
        url = reverse("operations:attribution_correction", args=[self.lead.pk])
        before = self.state()
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "confirm_legacy_source")
        self.assertContains(page, self.source.number)
        self.assertEqual(self.state(), before)
        form = page.context["form"]
        payload = {
            "invoice": "",
            "expected_version": str(form["expected_version"].value()),
            "expected_fingerprint": form["expected_fingerprint"].value(),
            "reason": "Synthetic protected old attribution explicitly reviewed",
            "evidence": "Synthetic protected creation and invoice archive",
        }
        rejected = self.client.post(url, payload)
        self.assertEqual(rejected.status_code, 200)
        self.assertTrue(rejected.context["form"].errors)
        self.assertEqual(self.state(), before)
        self.assertFalse(LeadIdentityChange.objects.filter(lead=self.lead).exists())
        documents = self.documents()
        accepted = self.client.post(url, {**payload, "confirm_legacy_source": "on"})
        self.assertEqual(accepted.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(
            (
                self.lead.party_id,
                self.lead.product_id,
                self.lead.invoice_id,
                self.lead.version,
            ),
            (self.party.pk, self.product.pk, None, 3),
        )
        self.assertEqual(self.documents(), documents)
        self.assertEqual(
            [row.kind for row in self.context()["history"]],
            ["legacy_adoption", "attribution"],
        )
        lead_url = reverse("marketing:lead_detail", args=[self.lead.pk])
        before = self.state()
        page = self.client.get(lead_url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(self.state(), before)
        form = page.context["invoice_form"]
        relink = {
            "operation": "link_invoice",
            "invoice": str(self.source.pk),
            "expected_version": str(self.lead.version),
            "expected_fingerprint": form["expected_fingerprint"].value(),
            "confirm_identity": "on",
            "reason": "Synthetic protected known mapping relinked",
            "evidence": "Synthetic matching actual issued invoice",
        }
        self.assertEqual(self.client.post(lead_url, relink).status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(
            (
                self.lead.party_id,
                self.lead.product_id,
                self.lead.invoice_id,
                self.lead.version,
            ),
            (self.party.pk, self.product.pk, self.source.pk, 4),
        )
        self.assertEqual(self.documents(), documents)
        self.assertEqual(self.context()["eligible_fields"], [])
        self.assertEqual(
            [row.kind for row in self.context()["history"]],
            ["legacy_adoption", "attribution", "invoice_link"],
        )

    def test_malformed_legacy_recovery_get_displays_source_review_blocker_without_write_or_500(
        self,
    ):
        self.fixture()
        self.rewrite_detail({"invoice_id": self.source.pk, "before_mapping": None})
        self.client.force_login(self.finance)
        before = self.state()
        page = self.client.get(
            reverse("marketing:invoice_identity_correction", args=[self.lead.pk])
        )
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Periksa sumber pelanggan/produk")
        self.assertEqual(self.state(), before)
        self.assertFalse(LeadIdentityChange.objects.filter(lead=self.lead).exists())

    def test_26_foreign_only_historical_audit_actors_block_source_review_without_get_writes(
        self,
    ):
        self.fixture(detached=True)
        finance.cancel_invoice(
            organization=self.org,
            actor=self.owner,
            invoice=self.source,
            date=self.day,
            reason="Synthetic genuine historical source cancellation",
        )
        foreign_actor = get_user_model().objects.create_user(
            "identity-legacy-foreign-history"
        )
        Membership.all_objects.create(
            organization=self.other, user=foreign_actor, role="finance"
        )
        self.assertFalse(
            Membership.all_objects.filter(
                organization=self.org, user=foreign_actor
            ).exists()
        )
        self.assertEqual(self.context()["blockers"], [])
        issue = AuditEvent.objects.get(
            organization=self.org,
            action="finance.invoice.issued",
            object_id=str(self.source.pk),
        )
        cancellation = AuditEvent.objects.get(
            organization=self.org,
            action="finance.invoice.cancelled",
            object_id=str(self.source.pk),
        )
        self.client.force_login(self.finance)
        url = reverse("marketing:invoice_identity_correction", args=[self.lead.pk])
        for event in (
            self.created_event,
            self.link_event,
            self.correction_event,
            issue,
            cancellation,
        ):
            original_actor_id = event.actor_id
            with self.subTest(action=event.action):
                AuditEvent.objects.filter(pk=event.pk).update(actor_id=foreign_actor.pk)
                try:
                    self.assert_blocked_without_writes()
                    before = self.state()
                    page = self.client.get(url)
                    self.assertEqual(page.status_code, 200)
                    self.assertContains(page, "Periksa sumber pelanggan/produk")
                    self.assertEqual(self.state(), before)
                    self.assertFalse(
                        LeadIdentityChange.objects.filter(lead=self.lead).exists()
                    )
                finally:
                    AuditEvent.objects.filter(pk=event.pk).update(
                        actor_id=original_actor_id
                    )
        self.assertEqual(self.context()["blockers"], [])

    def test_27_revoked_historical_membership_remains_evidence_for_fresh_authorized_adoption(
        self,
    ):
        self.fixture()
        Membership.all_objects.filter(
            organization=self.org, user=self.historical_writer
        ).update(is_active=False)
        self.assertTrue(
            get_user_model().objects.filter(pk=self.historical_writer.pk).exists()
        )
        self.assertTrue(
            Membership.all_objects.filter(
                organization=self.org, user=self.historical_writer, is_active=False
            ).exists()
        )
        context = self.context()
        self.assertEqual(context["blockers"], [])
        self.assertTrue(context["legacy_review_required"])
        self.assertEqual(set(context["eligible_fields"]), {"party", "product"})
        original_audits = list(
            AuditEvent.objects.filter(organization=self.org).order_by("pk").values()
        )
        documents = self.documents()
        accepted = recover_invoice_identity(**self.proposal())
        self.assertEqual(accepted.created_by_id, self.finance.pk)
        self.lead.refresh_from_db()
        self.assertEqual(
            (self.lead.party_id, self.lead.product_id, self.lead.invoice_id),
            (self.target.party_id, self.target.product_id, self.target.pk),
        )
        self.assertEqual(self.documents(), documents)
        self.assertEqual(
            list(
                AuditEvent.objects.filter(pk__in=[row["id"] for row in original_audits])
                .order_by("pk")
                .values()
            ),
            original_audits,
        )
        adoption = self.context()["history"][0]
        self.assertEqual(adoption.kind, "legacy_adoption")
        copied_link = next(
            row
            for row in adoption.origin_snapshot["legacy_basis"]["legacy_evidence"]
            if row["id"] == self.link_event.pk
        )
        self.assertEqual(copied_link["actor_id"], self.historical_writer.pk)

    def test_28_issue_domain_version_and_idempotency_key_must_match_the_supported_source_schema(
        self,
    ):
        self.fixture()
        issue = DomainEvent.objects.get(
            organization=self.org,
            topic="invoice.issued",
            aggregate_type="finance.invoice",
            aggregate_id=str(self.source.pk),
        )
        self.assertEqual(issue.version, 1)
        self.assertEqual(issue.idempotency_key, f"invoice.issued:{self.source.pk}")
        original = {"version": issue.version, "idempotency_key": issue.idempotency_key}
        for changes in (
            {"version": 2},
            {"idempotency_key": f"invoice.issued:unsupported-{self.source.pk}"},
        ):
            with self.subTest(changes=changes):
                DomainEvent.objects.filter(pk=issue.pk).update(**changes)
                try:
                    self.assert_blocked_without_writes()
                finally:
                    DomainEvent.objects.filter(pk=issue.pk).update(**original)
        self.assertEqual(self.context()["blockers"], [])
