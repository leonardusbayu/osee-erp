"""Recipient identity staff workflows at public HTTP seams; synthetic facts only."""

import uuid
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Organization


@override_settings(OPENROUTER_API_KEY="")
class RecipientIdentityHTTPTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic recipient identity UI")
        self.users = {}
        for role in ("owner", "finance", "marketing", "auditor", "reviewer"):
            user = get_user_model().objects.create_user(username="recipient-ui-" + role)
            Membership.objects.create(organization=self.org, user=user, role=role)
            self.users[role] = user

    def helper(self):
        from taxes.tests_workflow import TaxReleaseWorkflowTests

        helper = TaxReleaseWorkflowTests()
        helper.org, helper.owner = self.org, self.users["owner"]
        return helper

    def invoice(self, *, amount="500000"):
        from finance import services
        from finance.models import Party, Product

        customer = Party.objects.create(
            organization=self.org,
            name="Synthetic customer withholding OSEE",
            kind="customer",
            tax_id="OLD-NOT-AN-IDENTITY-DECISION",
        )
        product = Product.objects.create(
            organization=self.org,
            code="ITP-UI",
            name="Synthetic ITP",
            kind="itp",
            default_price=Decimal(amount),
        )
        invoice = services.create_invoice(
            organization=self.org,
            actor=self.users["owner"],
            party=customer,
            product=product,
            quantity=1,
            date=date(2026, 9, 10),
        )
        return services.issue_invoice(
            organization=self.org, actor=self.users["owner"], invoice=invoice
        )

    def professional(self):
        return {
            "evidence": self.helper().evidence().pk,
            "professional_name": "Synthetic professional",
            "qualification_reference": "Synthetic documented engagement",
            "reviewed_on": "2026-10-09",
            "statement": "Synthetic received report confirms exact displayed decision",
            "report_confirmed": "on",
        }

    def legacy_obligation(self, reference):
        from django.db.models.query import QuerySet

        from taxes.models import TaxObligation

        # The importer/history seam can contain old approved records. New model
        # validation must remain strict; these private legacy fixtures bypass it.
        row = TaxObligation(
            organization=self.org,
            period=date(2024, 9, 1),
            tax_type="pph23",
            direction="receivable",
            base=Decimal(500000),
            rate=Decimal("0.02"),
            amount=Decimal(10000),
            source_reference=reference,
            rule_reference="Synthetic legacy record",
            status="approved",
        )
        QuerySet(TaxObligation).bulk_create([row])
        return row

    def publish(self):
        from taxes.knowledge import publish_catalog
        from taxes.services import SOURCE_CATALOG

        publish_catalog(
            SOURCE_CATALOG,
            date(2026, 10, 9),
            reviewed_by="Synthetic recipient source reviewer",
            review_reference="Synthetic source publication only",
        )

    def proposal(self, party=None, **changes):
        data = {
            "request_key": str(uuid.uuid4()),
            "target": "supplier" if party else "organization",
            "legal_name": party.name if party else self.org.name,
            "recipient_type": "body",
            "tax_residency": "spdn",
            "population_status": "na",
            "identity_type": "npwp16",
            "number": "2234567890123456" if party else "1234567890123456",
            "registration_status": "registered",
            "effective_from": "2025-05-22",
            "reference": "SYN-IDENTITY-UI",
            "reason": "Synthetic documented recipient identity for exact company",
            "evidence": self.helper().evidence().pk,
        }
        if party:
            data["recipient_party"] = party.pk
        data.update(changes)
        return data

    def report(self, identity, **changes):
        data = {
            "expected_fingerprint": identity.fingerprint,
            "expected_version": identity.revision,
            "treatment": "standard_npwp",
            "evidence": self.helper().evidence().pk,
            "professional_name": "Synthetic identity professional",
            "qualification_reference": "Synthetic documented engagement",
            "reviewed_on": "2026-10-09",
            "statement": "Synthetic report confirms exact identity evidence and stated treatment",
            "report_confirmed": "on",
        }
        data.update(changes)
        return data

    def prepared_and_approved(self, party=None, **changes):
        from taxes.models import RecipientTaxIdentity

        self.client.force_login(self.users["finance"])
        data = self.proposal(party, **changes)
        response = self.client.post(reverse("taxes:recipient_identity_new"), data)
        self.assertEqual(response.status_code, 302, response.content.decode()[:500])
        identity = RecipientTaxIdentity.objects.get(
            organization=self.org, request_key=data["request_key"]
        )
        self.client.force_login(self.users["owner"])
        report = self.report(
            identity,
            treatment="increased_no_npwp"
            if changes.get("identity_type") == "none"
            else "standard_npwp",
        )
        response = self.client.post(
            reverse("taxes:recipient_identity_review", args=[identity.pk]), report
        )
        self.assertEqual(response.status_code, 302, response.content.decode()[:500])
        return identity

    def test_recipient_register_reads_and_preparation_follow_company_roles_and_csrf(
        self,
    ):
        index = reverse("taxes:recipient_identities")
        prepare = reverse("taxes:recipient_identity_new")
        for role in ("owner", "finance"):
            self.client.force_login(self.users[role])
            self.assertContains(
                self.client.get(index), "Identitas penerima penghasilan"
            )
            self.assertContains(self.client.get(prepare), "Siapkan identitas penerima")
        for role in ("auditor", "reviewer"):
            self.client.force_login(self.users[role])
            self.assertEqual(self.client.get(index).status_code, 200)
            self.assertEqual(self.client.get(prepare).status_code, 403)
            self.assertEqual(self.client.post(prepare, {}).status_code, 403)
        self.client.force_login(self.users["marketing"])
        self.assertEqual(self.client.get(index).status_code, 403)
        self.assertEqual(self.client.get(prepare).status_code, 403)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.users["finance"])
        self.assertEqual(csrf.post(prepare, {}).status_code, 403)

    def test_unknown_identity_and_format_only_number_never_choose_a_tax_factor(self):
        from taxes.models import ExternalTaxReview, RecipientTaxIdentity, TaxObligation

        self.publish()
        self.client.force_login(self.users["finance"])
        data = self.proposal(
            identity_type="unknown",
            number="",
            registration_status="unknown",
            tax_residency="unknown",
        )
        response = self.client.post(reverse("taxes:recipient_identity_new"), data)
        self.assertEqual(response.status_code, 302, response.content.decode()[:500])
        identity = RecipientTaxIdentity.objects.get(request_key=data["request_key"])
        self.assertContains(self.client.get(response.url), "Menunggu laporan pemeriksa")
        self.assertFalse(hasattr(identity, "approval"))
        self.client.force_login(self.users["owner"])
        count = ExternalTaxReview.objects.count()
        rejected = self.client.post(
            reverse("taxes:recipient_identity_review", args=[identity.pk]),
            self.report(identity),
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(
            rejected.context["form"]["professional_name"].value(),
            "Synthetic identity professional",
        )
        self.assertEqual(ExternalTaxReview.objects.count(), count)
        unresolved = self.client.post(
            reverse("taxes:recipient_identity_review", args=[identity.pk]),
            self.report(identity, treatment="unresolved"),
        )
        self.assertEqual(unresolved.status_code, 302, unresolved.content.decode()[:500])
        self.assertContains(
            self.client.get(unresolved.url), "Belum dapat digunakan untuk PPh 23"
        )
        self.assertFalse(TaxObligation.objects.exists())

    def test_finance_prepares_owner_approves_and_lists_mask_private_identity(self):
        from taxes.models import RecipientTaxIdentity

        self.publish()
        self.client.force_login(self.users["finance"])
        data = self.proposal()
        response = self.client.post(reverse("taxes:recipient_identity_new"), data)
        self.assertEqual(response.status_code, 302)
        identity = RecipientTaxIdentity.objects.get(request_key=data["request_key"])
        review = reverse("taxes:recipient_identity_review", args=[identity.pk])
        self.assertEqual(self.client.get(review).status_code, 403)
        self.assertEqual(
            self.client.post(review, self.report(identity)).status_code, 403
        )
        for role in ("reviewer", "auditor"):
            self.client.force_login(self.users[role])
            self.assertEqual(self.client.get(response.url).status_code, 200)
            self.assertNotContains(self.client.get(response.url), data["number"])
            self.assertEqual(self.client.get(review).status_code, 403)
        self.client.force_login(self.users["owner"])
        approved = self.client.post(review, self.report(identity))
        self.assertEqual(approved.status_code, 302, approved.content.decode()[:500])
        self.assertContains(
            self.client.get(approved.url), "Laporan pemeriksa yang menjadi dasar"
        )
        index = self.client.get(reverse("taxes:recipient_identities"))
        self.assertNotContains(index, data["number"])
        self.assertContains(index, "3456")
        self.client.force_login(self.users["marketing"])
        self.assertEqual(self.client.get(approved.url).status_code, 403)
        self.assertEqual(
            self.client.get(
                reverse("taxes:evidence_download", args=[identity.evidence_id])
            ).status_code,
            403,
        )

    def test_identity_errors_and_wrong_version_preserve_inputs_without_report_or_company_leak(
        self,
    ):
        from taxes.models import ExternalTaxReview, RecipientTaxIdentity

        self.publish()
        self.client.force_login(self.users["finance"])
        data = self.proposal(effective_from="2025-05-21")
        rejected = self.client.post(reverse("taxes:recipient_identity_new"), data)
        self.assertEqual(rejected.status_code, 200)
        self.assertContains(rejected, "22 Mei 2025")
        self.assertEqual(rejected.context["form"]["number"].value(), data["number"])
        self.assertEqual(
            rejected.context["form"]["request_key"].value(), data["request_key"]
        )
        self.assertFalse(RecipientTaxIdentity.objects.exists())
        data["effective_from"] = "2025-05-22"
        prepared = self.client.post(reverse("taxes:recipient_identity_new"), data)
        self.assertEqual(prepared.status_code, 302)
        identity = RecipientTaxIdentity.objects.get(request_key=data["request_key"])
        self.client.force_login(self.users["owner"])
        count = ExternalTaxReview.objects.count()
        rejected = self.client.post(
            reverse("taxes:recipient_identity_review", args=[identity.pk]),
            self.report(identity, expected_version=2),
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.context["form"]["treatment"].value(), "standard_npwp")
        self.assertEqual(ExternalTaxReview.objects.count(), count)
        other = Organization.objects.create(name="Synthetic foreign recipient")
        Membership.objects.create(
            organization=other, user=self.users["owner"], role="owner"
        )
        session = self.client.session
        session["organization_id"] = other.pk
        session.save()
        for route in ("recipient_identity", "recipient_identity_review"):
            self.assertEqual(
                self.client.get(
                    reverse("taxes:" + route, args=[identity.pk])
                ).status_code,
                404,
            )
        self.assertEqual(
            self.client.get(
                reverse("taxes:evidence_download", args=[identity.evidence_id])
            ).status_code,
            404,
        )

    def test_manual_pph23_binds_supplier_and_date_and_computes_factor_two(self):
        from finance.models import Party
        from taxes.models import TaxObligation

        self.publish()
        own = self.prepared_and_approved()
        supplier = Party.objects.create(
            organization=self.org,
            name="Synthetic supplier without NPWP",
            kind="supplier",
            tax_id="FREE-TEXT-NOT-PROOF",
        )
        identity = self.prepared_and_approved(
            supplier, identity_type="none", number="", registration_status="no_npwp"
        )
        self.client.force_login(self.users["finance"])
        get = self.client.get(reverse("taxes:obligation_new"))
        self.assertNotContains(get, supplier.tax_id)
        self.assertNotContains(get, 'name="tax_point_date" value=')
        data = {
            "period": "2026-09",
            "rule_code": "pph23_service_2",
            "base": "500000",
            "direction": "payable",
            "source_reference": "SYN-SUPPLIER-INV",
            "rule_reference": "Synthetic reviewed PPh23 object",
            "object_code": "24-104-99",
            "evidence": self.helper().evidence().pk,
            "note": "Synthetic source price before withholding",
            "recipient_party": supplier.pk,
            "recipient_identity": identity.pk,
            "tax_point_date": "2026-09-10",
            "tax_point_basis": "due",
            "withholder_reference": own.number,
            "withholder_name": own.legal_name,
            "object_reason": "Synthetic professional object and exclusion review",
            "final_exempt_excluded": "on",
        }
        rejected = self.client.post(
            reverse("taxes:obligation_new"), {**data, "tax_point_date": "2026-08-10"}
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(
            rejected.context["form"]["recipient_identity"].value(), str(identity.pk)
        )
        self.assertFalse(TaxObligation.objects.exists())
        prepared = self.client.post(reverse("taxes:obligation_new"), data)
        self.assertEqual(prepared.status_code, 302, prepared.content.decode()[:500])
        row = TaxObligation.objects.get(organization=self.org)
        self.assertEqual((row.amount, row.rate), (Decimal(20000), Decimal("0.04")))
        self.assertEqual(row.recipient_identity_id, identity.pk)
        detail = self.client.get(prepared.url)
        self.assertContains(detail, "Identitas dan tanggal yang terikat")
        self.assertContains(detail, supplier.name)
        self.assertNotContains(detail, own.number)
        self.client.force_login(self.users["reviewer"])
        self.assertNotContains(
            self.client.get(prepared.url), "Catat laporan &amp; persetujuan"
        )
        for route in ("obligation_review", "obligation_verify"):
            self.assertEqual(
                self.client.get(reverse("taxes:" + route, args=[row.pk])).status_code,
                403,
            )

    def test_direct_pdf_retry_reuses_exact_document_and_changed_request_is_rejected(
        self,
    ):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from taxes.models import RecipientTaxIdentity, TaxEvidence

        self.publish()
        self.client.force_login(self.users["finance"])
        data = self.proposal()
        source = TaxEvidence.objects.get(pk=data.pop("evidence"))
        data["file"] = SimpleUploadedFile(
            "synthetic-ui-direct.pdf",
            bytes(source.content),
            content_type="application/pdf",
        )
        first = self.client.post(reverse("taxes:recipient_identity_new"), data)
        self.assertEqual(first.status_code, 302, first.content.decode()[:500])
        count = TaxEvidence.objects.count()
        data["file"] = SimpleUploadedFile(
            "synthetic-ui-direct.pdf",
            bytes(source.content),
            content_type="application/pdf",
        )
        again = self.client.post(reverse("taxes:recipient_identity_new"), data)
        self.assertEqual(again.status_code, 302, again.content.decode()[:500])
        self.assertEqual(again.url, first.url)
        self.assertEqual(TaxEvidence.objects.count(), count)
        data["reason"] = "Synthetic changed fact using existing request key"
        data["file"] = SimpleUploadedFile(
            "synthetic-ui-direct.pdf",
            bytes(source.content),
            content_type="application/pdf",
        )
        rejected = self.client.post(reverse("taxes:recipient_identity_new"), data)
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.context["form"]["reason"].value(), data["reason"])
        self.assertEqual(RecipientTaxIdentity.objects.count(), 1)
        self.assertEqual(TaxEvidence.objects.count(), count)

    def test_customer_invoice_and_certificate_match_both_bound_parties_before_money_action(
        self,
    ):
        from taxes.models import TaxObligation, TaxVerification

        self.publish()
        own = self.prepared_and_approved()
        invoice = self.invoice()
        self.client.force_login(self.users["finance"])
        data = {
            "period": "2026-09",
            "rule_code": "pph23_service_2",
            "base": "500000",
            "direction": "receivable",
            "source_reference": invoice.number,
            "rule_reference": "Synthetic reviewed service",
            "object_code": "24-104-99",
            "source_invoice": invoice.pk,
            "evidence": self.helper().evidence().pk,
            "note": "Synthetic exact invoice context",
            "recipient_identity": own.pk,
            "tax_point_date": "2026-09-10",
            "tax_point_basis": "due",
            "withholder_reference": "3234567890123456",
            "withholder_name": invoice.party.name,
            "object_reason": "Synthetic professional object and exclusion review",
            "final_exempt_excluded": "on",
        }
        missing = self.client.post(
            reverse("taxes:obligation_new"), {**data, "source_invoice": ""}
        )
        self.assertEqual(missing.status_code, 200)
        self.assertContains(missing, "Pilih invoice pelanggan")
        response = self.client.post(reverse("taxes:obligation_new"), data)
        self.assertEqual(response.status_code, 302, response.content.decode()[:500])
        row = TaxObligation.objects.get()
        self.assertEqual(
            (row.amount, row.rate, row.source_invoice_id),
            (Decimal(10000), Decimal("0.02"), invoice.pk),
        )
        self.client.force_login(self.users["owner"])
        self.assertEqual(
            self.client.post(
                reverse("taxes:obligation_review", args=[row.pk]), self.professional()
            ).status_code,
            302,
        )
        detail = self.client.get(response.url)
        self.assertContains(detail, "Owner/Direktur perlu mencocokkan")
        self.assertNotContains(detail, reverse("customer_tax_receipt", args=[row.pk]))
        certificate = {
            "evidence": self.helper().evidence().pk,
            "kind": "credit",
            "reference": "SYN-CERT-UI-2026",
            "taxpayer_reference": own.number,
            "period": "2026-09",
            "amount": "10000",
            "note": "Synthetic exact certificate comparison",
            "matches_confirmed": "on",
            **self.helper().certificate(row),
        }
        for key, value in (
            ("withholder_reference", "4234567890123456"),
            ("recipient_name", "Synthetic wrong company"),
            ("base", "400000"),
            ("tax_point_date", "2026-09-11"),
            ("certificate_status", ""),
        ):
            rejected = self.client.post(
                reverse("taxes:obligation_verify", args=[row.pk]),
                {**certificate, key: value},
            )
            self.assertEqual(rejected.status_code, 200, key)
            self.assertEqual(str(rejected.context["form"][key].value()), value)
            self.assertFalse(TaxVerification.objects.exists())
        verified = self.client.post(
            reverse("taxes:obligation_verify", args=[row.pk]), certificate
        )
        self.assertEqual(verified.status_code, 302, verified.content.decode()[:500])
        proof = TaxVerification.objects.get()
        self.assertEqual(
            proof.certificate_snapshot["withholder_reference"], "3234567890123456"
        )
        self.client.force_login(self.users["finance"])
        completed = self.client.get(response.url)
        self.assertContains(completed, reverse("customer_tax_receipt", args=[row.pk]))
        self.assertContains(completed, "Bukti potong sudah dicocokkan")
        self.assertContains(completed, "Bukti potong bukan bukti penerimaan SPT")
        self.client.force_login(self.users["owner"])
        self.assertNotContains(
            self.client.get(response.url),
            reverse("taxes:obligation_verify", args=[row.pk]),
        )
        self.client.force_login(self.users["finance"])
        self.assertEqual(
            self.client.post(
                reverse("taxes:obligation_verify", args=[row.pk]), certificate
            ).status_code,
            403,
        )

    def test_bill_decision_requires_date_and_supplier_identity_but_no_withholding_does_not(
        self,
    ):
        from finance import services
        from finance.models import Party
        from taxes.models import BillTaxDecision, ExternalTaxReview

        self.publish()
        own = self.prepared_and_approved()
        supplier = Party.objects.create(
            organization=self.org, name="Synthetic bill supplier", kind="supplier"
        )
        identity = self.prepared_and_approved(supplier)
        bill = services.create_bill(
            organization=self.org,
            actor=self.users["owner"],
            supplier=supplier,
            amount="500000",
            date=date(2026, 9, 10),
            service_date=date(2026, 9, 10),
            supplier_reference="SYN-BILL-IDENTITY",
        )
        services.approve_bill(
            organization=self.org, actor=self.users["owner"], bill=bill
        )
        self.client.force_login(self.users["owner"])
        route = reverse("taxes:bill_review", args=[bill.pk])
        fields = {
            **self.professional(),
            "rule_code": "pph23_service_2",
            "base": "500000",
            "period": "2026-09",
            "object_code": "24-104-99",
            "reason": "Synthetic professional object, exclusions and exact tax point",
            "recipient_identity": identity.pk,
            "tax_point_date": "2026-09-10",
            "tax_point_basis": "due",
            "withholder_reference": own.number,
            "withholder_name": own.legal_name,
            "final_exempt_excluded": "on",
        }
        count = ExternalTaxReview.objects.count()
        rejected = self.client.post(route, {**fields, "recipient_identity": own.pk})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.context["form"]["reason"].value(), fields["reason"])
        self.assertEqual(ExternalTaxReview.objects.count(), count)
        response = self.client.post(route, fields)
        self.assertEqual(response.status_code, 302, response.content.decode()[:500])
        decision = BillTaxDecision.objects.get(bill=bill)
        self.assertEqual(
            (decision.amount, decision.obligation.recipient_party_id),
            (Decimal(10000), supplier.pk),
        )
        self.assertEqual(
            decision.external_review.decision["recipient_identity_id"], identity.pk
        )
        untouched = Party.objects.create(
            organization=self.org, name="Synthetic bill no withholding", kind="supplier"
        )
        other = services.create_bill(
            organization=self.org,
            actor=self.users["owner"],
            supplier=untouched,
            amount="250000",
            date=date(2026, 9, 10),
            service_date=date(2026, 9, 10),
            supplier_reference="SYN-BILL-NIL",
        )
        services.approve_bill(
            organization=self.org, actor=self.users["owner"], bill=other
        )
        response = self.client.post(
            reverse("taxes:bill_review", args=[other.pk]),
            {
                **self.professional(),
                "rule_code": "no_withholding",
                "base": "250000",
                "period": "2026-09",
                "reason": "Synthetic professionally excluded withholding object",
            },
        )
        self.assertEqual(response.status_code, 302, response.content.decode()[:500])
        self.assertEqual(BillTaxDecision.objects.get(bill=other).amount, Decimal(0))

    def test_legacy_unbound_history_stays_readable_without_action_buttons(self):
        row = self.legacy_obligation("SYN-LEGACY-READ-ONLY")
        for role in ("owner", "finance", "reviewer", "auditor"):
            self.client.force_login(self.users[role])
            detail = self.client.get(reverse("taxes:obligation", args=[row.pk]))
            self.assertEqual(detail.status_code, 200)
            self.assertContains(detail, "SYN-LEGACY-READ-ONLY")
            self.assertContains(detail, "Angka dan dokumen historis tetap dapat dibaca")
            self.assertNotContains(
                detail, reverse("taxes:obligation_verify", args=[row.pk])
            )
            self.assertNotContains(
                detail, reverse("customer_tax_receipt", args=[row.pk])
            )

    def test_unpublished_references_preserve_inputs_and_show_owner_next_step(self):
        from taxes.models import RecipientTaxIdentity

        self.client.force_login(self.users["finance"])
        data = self.proposal()
        response = self.client.post(reverse("taxes:recipient_identity_new"), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["number"].value(), data["number"])
        self.assertContains(response, "Minta Owner/Direktur")
        self.assertNotContains(response, "pmk81-identity-2024")
        self.assertFalse(RecipientTaxIdentity.objects.exists())

    def test_unresolved_recovery_keeps_actual_date_and_original_report_history(self):
        from finance.models import Party
        from taxes.models import RecipientTaxIdentity
        from taxes.recipient_identity import resolve_recipient_identity

        self.publish()
        party = Party.objects.create(
            organization=self.org,
            name="Synthetic unresolved recovery supplier",
            kind="supplier",
        )
        data = self.proposal(
            party,
            identity_type="unknown",
            number="",
            registration_status="unknown",
            tax_residency="unknown",
        )
        self.client.force_login(self.users["finance"])
        self.assertEqual(
            self.client.post(reverse("taxes:recipient_identity_new"), data).status_code,
            302,
        )
        previous = RecipientTaxIdentity.objects.get(request_key=data["request_key"])
        self.client.force_login(self.users["owner"])
        self.assertEqual(
            self.client.post(
                reverse("taxes:recipient_identity_review", args=[previous.pk]),
                self.report(previous, treatment="unresolved"),
            ).status_code,
            302,
        )
        original_report = previous.approval.external_review_id
        self.client.force_login(self.users["finance"])
        recovery = self.client.get(
            reverse("taxes:recipient_identity_new") + f"?previous={previous.pk}"
        )
        self.assertEqual(
            recovery.context["form"]["effective_from"].value(), previous.effective_from
        )
        self.assertContains(recovery, 'value="2025-05-22"')
        corrected_data = self.proposal(
            party,
            previous=previous.pk,
            reference="SYN-IDENTITY-RESOLVED",
            reason="Synthetic new document resolves facts at the same actual identity date",
        )
        response = self.client.post(
            reverse("taxes:recipient_identity_new"), corrected_data
        )
        self.assertEqual(response.status_code, 302, response.content.decode()[:500])
        corrected = RecipientTaxIdentity.objects.get(
            request_key=corrected_data["request_key"]
        )
        self.client.force_login(self.users["owner"])
        response = self.client.post(
            reverse("taxes:recipient_identity_review", args=[corrected.pk]),
            self.report(corrected),
        )
        self.assertEqual(response.status_code, 302, response.content.decode()[:500])
        self.assertEqual(corrected.effective_from, previous.effective_from)
        self.assertEqual(previous.approval.external_review_id, original_report)
        self.assertEqual(
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=party,
                tax_point_date=date(2026, 9, 10),
            ).pk,
            corrected.pk,
        )
        self.assertContains(
            self.client.get(reverse("taxes:recipient_identity", args=[previous.pk])),
            "Versi sebelumnya",
        )

    def test_wrong_unapproved_draft_correction_uses_no_predecessor_and_shows_obsolete_note(
        self,
    ):
        from taxes.models import RecipientTaxIdentity

        self.publish()
        self.client.force_login(self.users["finance"])
        data = self.proposal(registration_status="unknown")
        response = self.client.post(reverse("taxes:recipient_identity_new"), data)
        self.assertEqual(response.status_code, 302)
        previous = RecipientTaxIdentity.objects.get(request_key=data["request_key"])
        self.assertContains(
            self.client.get(response.url), "Perbaiki data untuk pemeriksaan"
        )
        corrected_form = self.client.get(
            reverse("taxes:recipient_identity_new") + f"?previous={previous.pk}"
        )
        self.assertIsNone(corrected_form.context["form"]["previous"].value())
        self.assertEqual(
            corrected_form.context["form"]["effective_from"].value(),
            previous.effective_from,
        )
        corrected = self.prepared_and_approved()
        self.assertIsNone(corrected.previous_id)
        old_detail = self.client.get(
            reverse("taxes:recipient_identity", args=[previous.pk])
        )
        self.assertContains(
            old_detail,
            "Draf ini tetap tersimpan. Penerima sudah mempunyai identitas yang disetujui",
        )
        self.assertNotContains(old_detail, "Catat laporan pemeriksa")
        self.assertNotContains(old_detail, "Perbaiki data untuk pemeriksaan")

    def test_malformed_historical_snapshot_is_readable_and_blocks_actions(self):
        from django.db.models.query import QuerySet

        from taxes.models import TaxObligation

        row = self.legacy_obligation("SYN-MALFORMED-HISTORY")
        self.client.force_login(self.users["owner"])
        for snapshot in (
            ["malformed"],
            {"recipient": ["wrong"], "withholder": {"reference": 123, "name": False}},
        ):
            QuerySet(TaxObligation).filter(pk=row.pk).update(identity_snapshot=snapshot)
            response = self.client.get(reverse("taxes:obligation", args=[row.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "SYN-MALFORMED-HISTORY")
            self.assertContains(
                response, "Angka dan dokumen historis tetap dapat dibaca"
            )
            self.assertNotContains(
                response, reverse("taxes:obligation_verify", args=[row.pk])
            )
