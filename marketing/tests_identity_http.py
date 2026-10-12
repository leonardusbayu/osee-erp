"""Public HTTP invoice-identity correction, freshness and authorization contracts.

Fixture helpers are borrowed explicitly through a module alias. This class does
not inherit the service suite or rediscover its 18 tests. All invoices/journals,
bank receipts and Operations dependencies come from genuine public commands.
"""

from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlencode

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from finance import services as finance
from finance.models import Party, Product
from finance.tests_tax_money import TaxMoneyFixtures
from marketing import services
from marketing import tests_identity_recovery as identity_fixtures
from marketing.activity_models import WorkActivity
from marketing.identity_recovery import (
    identity_recovery_context,
    recover_invoice_identity,
)
from marketing.models import LeadIdentityChange, MarketingAction
from operations import services as operations
from operations.attribution import correct_lead_invoice


@override_settings(
    MARKETING_AI_PRIVATE_AGGREGATES_ENABLED=False, DIRECTOR_AI_ENDPOINT_APPROVED=False
)
class InvoiceIdentityHttpAcceptanceTests(TaxMoneyFixtures, TestCase):
    lead = identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.lead
    issued = identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.issued
    inferred = identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.inferred
    target = identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.target
    context = identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.context
    proposal = identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.proposal
    mapping = identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.mapping
    metadata = identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.metadata
    scoped_rows = identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.scoped_rows
    preserved_documents = (
        identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.preserved_documents
    )
    entire_state = identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.entire_state
    origin_source = (
        identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.origin_source
    )
    history_ids = identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.history_ids

    def setUp(self):
        identity_fixtures.InvoiceIdentityRecoveryAcceptanceTests.setUp(self)
        self.client.force_login(self.finance)

    def url(self, lead):
        return reverse("marketing:invoice_identity_correction", args=[lead.pk])

    def page(self, lead, client=None):
        response = (client or self.client).get(self.url(lead))
        self.assertEqual(response.status_code, 200)
        return response

    def payload(self, lead, target=None, *, page=None, **changes):
        form = (page or self.page(lead)).context["form"]
        return {
            "operation": "save",
            "action": "reset" if target is None else "replace",
            "target_invoice": "" if target is None else str(target.pk),
            "expected_version": str(form["expected_version"].value()),
            "expected_fingerprint": form["expected_fingerprint"].value(),
            "request_key": form["request_key"].value(),
            "confirm_identity": "on",
            "reason": "Synthetic staff checked wrong invoice",
            "evidence": "Synthetic staff archive/source-reference.txt",
            **changes,
        }

    def assert_retained(
        self,
        response,
        payload,
        fields=("action", "target_invoice", "reason", "evidence"),
    ):
        for field in fields:
            self.assertEqual(
                str(response.context["form"][field].value()), str(payload[field]), field
            )

    def recheck(self, lead, payload):
        before = self.entire_state()
        response = self.client.post(self.url(lead), {**payload, "operation": "recheck"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.entire_state(), before)
        self.assert_retained(response, payload)
        form = response.context["form"]
        self.assertNotEqual(form["request_key"].value(), payload["request_key"])
        self.assertFalse(form["confirm_identity"].value())
        self.assertFalse(form["confirm_legacy_source"].value())
        lead.refresh_from_db()
        self.assertEqual(str(form["expected_version"].value()), str(lead.version))
        self.assertEqual(
            form["expected_fingerprint"].value(), self.context(lead)["fingerprint"]
        )
        return {
            **payload,
            "operation": "save",
            "expected_version": str(form["expected_version"].value()),
            "expected_fingerprint": form["expected_fingerprint"].value(),
            "request_key": form["request_key"].value(),
            "confirm_identity": "",
            "confirm_legacy_source": "",
        }

    def test_owner_and_finance_get_replace_and_readback_preserve_money_metadata_and_original_source(
        self,
    ):
        for actor in (self.finance, self.owner):
            with self.subTest(actor=actor.username):
                self.client.force_login(actor)
                lead, source = self.inferred()
                target = self.target()
                finance.reconcile_receipt(
                    organization=self.org,
                    actor=self.owner,
                    invoice=source,
                    transaction=self.movement(
                        "500000", reference=f"HTTP-A-{source.pk}"
                    ),
                    amount="500000",
                )
                finance.reconcile_receipt(
                    organization=self.org,
                    actor=self.owner,
                    invoice=target,
                    transaction=self.movement(
                        "100000", reference=f"HTTP-B-{target.pk}"
                    ),
                    amount="100000",
                )
                before = self.entire_state()
                page = self.page(lead)
                self.assertEqual(self.entire_state(), before)
                self.assertContains(page, source.number)
                self.assertContains(page, self.party.name)
                self.assertContains(page, self.product.name)
                metadata, documents = self.metadata(lead), self.preserved_documents()
                payload = self.payload(lead, target, page=page)
                response = self.client.post(self.url(lead), payload)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(
                    self.mapping(lead),
                    (target.party_id, target.product_id, target.pk, 3),
                )
                self.assertEqual(self.metadata(lead), metadata)
                self.assertEqual(self.preserved_documents(), documents)
                decision = LeadIdentityChange.objects.get(
                    organization=self.org, request_key=payload["request_key"]
                )
                self.assertEqual(decision.created_by_id, actor.pk)
                before_read = self.entire_state()
                readback = self.page(lead)
                self.assertEqual(self.entire_state(), before_read)
                for text in (
                    target.number,
                    self.party_b.name,
                    self.product_b.name,
                    source.number,
                    payload["reason"],
                    payload["evidence"],
                ):
                    self.assertContains(readback, text)
                self.assertEqual(
                    self.origin_source(self.context(lead), "party"), source.pk
                )

    def test_marketing_auditor_and_legacy_director_read_history_but_post_is_forbidden(
        self,
    ):
        lead, source = self.inferred()
        target = self.target()
        payload = self.payload(lead, target)
        for actor in (self.marketing, self.auditor, self.director):
            with self.subTest(actor=actor.username):
                self.client.force_login(actor)
                before = self.entire_state()
                page = self.page(lead)
                self.assertContains(page, source.number)
                self.assertNotContains(page, 'value="save"')
                self.assertEqual(self.entire_state(), before)
                response = self.client.post(self.url(lead), payload)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.entire_state(), before)

    def test_detached_lead_keeps_recovery_link_and_reset_then_relink_remains_readable(
        self,
    ):
        lead, source = self.inferred()
        lead = correct_lead_invoice(
            organization=self.org,
            actor=self.finance,
            lead=lead,
            invoice=None,
            expected_version=2,
            reason="Synthetic old mistaken invoice detached",
        )
        detail = self.client.get(reverse("marketing:lead_detail", args=[lead.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, self.url(lead))
        payload = self.payload(lead)
        documents = self.preserved_documents()
        response = self.client.post(self.url(lead), payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.mapping(lead), (None, None, None, 4))
        self.assertEqual(self.preserved_documents(), documents)
        self.assertContains(self.page(lead), source.number)
        target = self.target()
        lead = services.link_invoice(
            organization=self.org,
            actor=self.finance,
            lead=lead,
            invoice=target,
            expected_version=4,
            confirm_identity=True,
        )
        self.assertEqual(
            self.mapping(lead), (target.party_id, target.product_id, target.pk, 5)
        )
        detail = self.client.get(reverse("marketing:lead_detail", args=[lead.pk]))
        self.assertContains(detail, self.url(lead))
        page = self.page(lead)
        self.assertContains(page, target.number)
        self.assertContains(page, source.number)
        self.assertEqual(self.origin_source(self.context(lead), "party"), source.pk)

    def test_exact_http_retry_returns_saved_decision_and_changed_retry_recheck_uses_a_new_key(
        self,
    ):
        lead, source = self.inferred()
        target, another = self.target(), self.target()
        payload = self.payload(lead, target)
        self.assertEqual(self.client.post(self.url(lead), payload).status_code, 302)
        before = self.entire_state()
        self.assertEqual(self.client.post(self.url(lead), payload).status_code, 302)
        self.assertEqual(self.entire_state(), before)
        changed = {
            **payload,
            "target_invoice": str(another.pk),
            "reason": "Synthetic corrected retry reason",
        }
        failed = self.client.post(self.url(lead), changed)
        self.assertEqual(failed.status_code, 400)
        self.assert_retained(failed, changed)
        self.assertEqual(
            failed.context["form"]["request_key"].value(), payload["request_key"]
        )
        self.assertEqual(self.entire_state(), before)
        fresh = self.recheck(lead, changed)
        self.assertEqual(
            self.client.post(
                self.url(lead), {**fresh, "confirm_identity": "on"}
            ).status_code,
            302,
        )
        self.assertEqual(
            self.mapping(lead), (another.party_id, another.product_id, another.pk, 4)
        )
        self.assertEqual(len(self.history_ids(self.context(lead))), 3)
        self.assertEqual(self.origin_source(self.context(lead), "party"), source.pk)

    def test_duplicate_form_parameters_never_select_last_value_or_mutate_on_recheck(
        self,
    ):
        lead, source = self.inferred()
        target, another = self.target(), self.target()
        payload = self.payload(lead, target)
        duplicate_values = {
            "operation": "recheck",
            "action": "reset",
            "target_invoice": str(another.pk),
            "expected_version": "3",
            "expected_fingerprint": "0" * 64,
            "request_key": "DIFFERENT-REQUEST",
            "reason": "Different reason",
            "evidence": "Different evidence",
            "confirm_identity": "",
        }
        for field, value in duplicate_values.items():
            with self.subTest(field=field):
                before = self.entire_state()
                data = {**payload, field: [payload[field], value]}
                response = self.client.post(
                    self.url(lead),
                    urlencode(data, doseq=True),
                    content_type="application/x-www-form-urlencoded",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.entire_state(), before)
        self.assertEqual(
            self.mapping(lead), (source.party_id, source.product_id, source.pk, 2)
        )

    def test_csrf_is_enforced_and_recovery_get_is_read_only(self):
        lead, _source = self.inferred()
        target = self.target()
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.finance)
        before = self.entire_state()
        page = self.page(lead, client=secure)
        self.assertContains(page, "csrfmiddlewaretoken")
        self.assertEqual(self.entire_state(), before)
        payload = self.payload(lead, target, page=page)
        response = secure.post(self.url(lead), payload)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.entire_state(), before)
        payload["csrfmiddlewaretoken"] = secure.cookies["csrftoken"].value
        self.assertEqual(secure.post(self.url(lead), payload).status_code, 302)
        self.assertEqual(
            self.mapping(lead), (target.party_id, target.product_id, target.pk, 3)
        )

    def test_stale_lead_view_retains_proposal_and_recheck_clears_both_confirmations_before_save(
        self,
    ):
        lead, _source = self.inferred()
        target = self.target()
        payload = self.payload(lead, target, confirm_legacy_source="on")
        lead = services.update_lead(
            organization=self.org,
            actor=self.owner,
            lead=lead,
            expected_version=2,
            notes="Synthetic newer source note",
            responsibility_reason="Synthetic Owner help after checking the current Marketing PIC",
        )
        before = self.entire_state()
        failed = self.client.post(self.url(lead), payload)
        self.assertEqual(failed.status_code, 400)
        self.assert_retained(
            failed,
            payload,
            fields=(
                "action",
                "target_invoice",
                "reason",
                "evidence",
                "expected_version",
                "expected_fingerprint",
            ),
        )
        self.assertEqual(self.entire_state(), before)
        fresh = self.recheck(lead, payload)
        before = self.entire_state()
        missing_confirmation = self.client.post(self.url(lead), fresh)
        self.assertEqual(missing_confirmation.status_code, 400)
        self.assertEqual(self.entire_state(), before)
        self.assertEqual(
            self.client.post(
                self.url(lead), {**fresh, "confirm_identity": "on"}
            ).status_code,
            302,
        )
        self.assertEqual(
            self.mapping(lead), (target.party_id, target.product_id, target.pk, 4)
        )

    def test_target_cancelled_after_get_rejects_and_retains_staff_inputs(self):
        lead, source = self.inferred()
        target = self.target()
        payload = self.payload(lead, target)
        finance.cancel_invoice(
            organization=self.org,
            actor=self.owner,
            invoice=target,
            date=self.day,
            reason="Synthetic selected target cancelled after staff view",
        )
        before = self.entire_state()
        failed = self.client.post(self.url(lead), payload)
        self.assertEqual(failed.status_code, 400)
        self.assert_retained(failed, payload)
        self.assertEqual(self.entire_state(), before)
        self.assertEqual(
            self.mapping(lead), (source.party_id, source.product_id, source.pk, 2)
        )

    def test_source_cancelled_after_get_stales_view_and_fresh_review_still_can_replace(
        self,
    ):
        lead, source = self.inferred()
        target = self.target()
        payload = self.payload(lead, target)
        finance.cancel_invoice(
            organization=self.org,
            actor=self.owner,
            invoice=source,
            date=self.day,
            reason="Synthetic historical source cancelled after staff view",
        )
        before = self.entire_state()
        failed = self.client.post(self.url(lead), payload)
        self.assertEqual(failed.status_code, 400)
        self.assert_retained(failed, payload)
        self.assertEqual(self.entire_state(), before)
        self.assertEqual(
            set(self.context(lead)["eligible_fields"]), {"party", "product"}
        )
        fresh = self.recheck(lead, payload)
        documents = self.preserved_documents()
        self.assertEqual(
            self.client.post(
                self.url(lead), {**fresh, "confirm_identity": "on"}
            ).status_code,
            302,
        )
        self.assertEqual(self.preserved_documents(), documents)
        self.assertEqual(self.origin_source(self.context(lead), "party"), source.pk)

    def test_order_added_after_get_blocks_recovery_and_displays_existing_dependency(
        self,
    ):
        lead, source = self.inferred()
        target = self.target()
        payload = self.payload(lead, target)
        order = operations.create_order(
            organization=self.org,
            actor=self.owner,
            reference="HTTP-SOURCE-ORDER",
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
            reason="Synthetic source invoice belongs to actual Order",
        )
        order = operations.link_order_lead(
            organization=self.org,
            actor=self.owner,
            order=order,
            lead=lead,
            expected_version=1,
            reason="Synthetic Order attribution appeared after view",
        )
        before = self.entire_state()
        failed = self.client.post(self.url(lead), payload)
        self.assertEqual(failed.status_code, 400)
        self.assert_retained(failed, payload)
        self.assertContains(failed, order.reference, status_code=400)
        self.assertEqual(self.entire_state(), before)
        fresh = self.recheck(lead, payload)
        rejected = self.client.post(self.url(lead), {**fresh, "confirm_identity": "on"})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.entire_state(), before)
        order.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(
            (order.lead_id, line.invoice_id, line.product_id),
            (lead.pk, source.pk, self.product.pk),
        )

    def test_cross_organization_target_is_invalid_and_foreign_actor_cannot_read_or_write_lead(
        self,
    ):
        lead, _source = self.inferred()
        foreign_party = Party.objects.create(
            organization=self.other,
            name="Synthetic HTTP foreign buyer",
            kind="reseller",
        )
        foreign_product = Product.objects.create(
            organization=self.other,
            code="HTTP-FOREIGN",
            name="Synthetic HTTP foreign product",
            kind="itp",
            default_price="500000",
        )
        foreign = self.issued(
            organization=self.other,
            actor=self.foreign_owner,
            party=foreign_party,
            product=foreign_product,
        )
        payload = self.payload(lead, foreign)
        before, foreign_before = self.entire_state(), self.entire_state(self.other)
        failed = self.client.post(self.url(lead), payload)
        self.assertEqual(failed.status_code, 400)
        self.assert_retained(failed, payload)
        self.assertEqual(self.entire_state(), before)
        self.assertEqual(self.entire_state(self.other), foreign_before)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.url(lead)).status_code, 404)
        self.assertEqual(self.client.post(self.url(lead), payload).status_code, 404)
        self.assertEqual(self.entire_state(), before)
        self.assertEqual(self.entire_state(self.other), foreign_before)

    def test_obsolete_local_recommendation_token_rejects_work_and_retains_every_entered_field(
        self,
    ):
        self.client.force_login(self.owner)
        lead, _source = self.inferred()
        equal = self.issued(number="HTTP-EQUAL-VALUES")
        campaign = services.create_campaign(
            organization=self.org,
            actor=self.owner,
            name="Synthetic recommendation source",
            channel="meta_ads",
        )
        services.save_ad_observation(
            organization=self.org,
            actor=self.owner,
            campaign=campaign,
            date=self.day,
            spend="100",
            source="Synthetic platform statement",
            reference="HTTP-RECOMMENDATION",
        )
        with patch("marketing.ai.urlopen") as network:
            page = self.client.get(
                reverse("marketing:advisor"),
                {"start": str(self.day - timedelta(days=1)), "end": str(self.day)},
            )
        network.assert_not_called()
        self.assertEqual(page.status_code, 200)
        recommendation = next(
            item
            for item in page.context["advice"]["recommendations"]
            if item["id"] == "confirm_period_coverage"
        )
        token = recommendation["action_token"]
        payload = self.payload(lead, equal)
        self.assertEqual(self.client.post(self.url(lead), payload).status_code, 302)
        action = {
            "recommendation_token": token,
            "title": "Keep entered work title",
            "description": "Keep entered work description",
            "evidence": "Keep entered work evidence",
            "metric_key": "spend_known_rows",
            "owner": self.member.pk,
            "due_date": str(timezone.localdate() + timedelta(days=10)),
            "channel": "meta_ads",
            "kind": "content",
        }
        before = self.entire_state()
        denied = self.client.post(reverse("marketing:action_create"), action)
        self.assertEqual(denied.status_code, 400)
        self.assertContains(denied, "Data sumber berubah", status_code=400)
        self.assert_retained(
            denied,
            action,
            fields=(
                "title",
                "description",
                "evidence",
                "owner",
                "due_date",
                "channel",
                "kind",
            ),
        )
        self.assertFalse(MarketingAction.objects.filter(organization=self.org).exists())
        self.assertFalse(WorkActivity.objects.filter(organization=self.org).exists())
        self.assertEqual(self.entire_state(), before)

    def test_displayed_before_mapping_comes_from_the_same_locked_context_as_freshness_controls(
        self,
    ):
        lead, _source = self.inferred()
        target = self.target()
        accepted = self.proposal(lead, target)
        original_context = identity_recovery_context

        def recovery_between_first_fetch_and_locked_read(*, organization, lead):
            recover_invoice_identity(**accepted)
            return original_context(organization=organization, lead=lead)

        # The view's public context adapter is the seam. The injected transition
        # itself is a genuine service recovery, rather than a forged Lead object.
        with patch(
            "marketing.identity_views.identity_recovery_context",
            side_effect=recovery_between_first_fetch_and_locked_read,
        ):
            page = self.page(lead)
        displayed = page.context["lead"]
        self.assertEqual(
            (
                displayed.party_id,
                displayed.product_id,
                displayed.invoice_id,
                displayed.version,
            ),
            (target.party_id, target.product_id, target.pk, 3),
        )
        self.assertEqual(str(page.context["form"]["expected_version"].value()), "3")
        self.assertEqual(
            page.context["form"]["expected_fingerprint"].value(),
            self.context(lead)["fingerprint"],
        )
        self.assertContains(page, self.party_b.name)
        self.assertContains(page, self.product_b.name)

    def test_malformed_identity_origin_context_renders_as_unknown_without_server_error(
        self,
    ):
        lead, _source = self.inferred()
        recovery = self.context(lead)
        recovery["origins"] = None
        with patch(
            "marketing.identity_views.identity_recovery_context",
            return_value=recovery,
        ):
            response = self.client.get(self.url(lead))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [origin["kind"] for origin in response.context["identity_origins"]],
            ["unknown", "unknown"],
        )
        self.assertEqual(
            [
                origin["source_invoice_id"]
                for origin in response.context["identity_origins"]
            ],
            [None, None],
        )
