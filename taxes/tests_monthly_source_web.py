"""Public monthly source-package workflow with synthetic company facts only."""

import json
from datetime import date

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Organization


@override_settings(OPENROUTER_API_KEY="")
class MonthlySourceHTTPTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic monthly source UI")
        self.users = {}
        for role in ("owner", "finance", "marketing", "auditor", "reviewer"):
            user = get_user_model().objects.create_user(
                username="monthly-source-" + role
            )
            Membership.objects.create(organization=self.org, user=user, role=role)
            self.users[role] = user
        self.client.force_login(self.users["finance"])

    def test_finance_starts_with_a_month_and_no_retyped_tax_total(self):
        response = self.client.get(
            reverse("taxes:monthly_sources"), {"period": "2026-09"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Siapkan data bulan ini")
        self.assertContains(response, 'type="month"')
        self.assertNotContains(response, 'name="base"')
        self.assertContains(response, "belum menjadi dasar pajak")

    def test_reader_cannot_prepare_marketing_denied_and_csrf_required(self):
        url = reverse("taxes:monthly_sources")
        for role in ("auditor", "reviewer"):
            self.client.force_login(self.users[role])
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(
                self.client.post(url, {"period": "2026-09"}).status_code, 403
            )
        self.client.force_login(self.users["marketing"])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"period": "2026-09"}).status_code, 403)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.users["finance"])
        self.assertEqual(csrf.post(url, {"period": "2026-09"}).status_code, 403)

    def test_empty_month_prepares_a_private_version_without_claiming_zero_tax(self):
        response = self.client.post(
            reverse("taxes:monthly_sources"), {"period": "2026-09"}
        )
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(response.url)
        self.assertContains(detail, "Pendapatan dalam buku")
        self.assertContains(detail, "Dasar pajak yang sudah diperiksa")
        self.assertContains(detail, "Belum ditentukan")
        self.assertNotContains(detail, "Buat draf pajak</button>")
        self.assertContains(detail, "Unduh sumber CSV")

    def test_only_owner_can_record_a_professional_monthly_report(self):
        from taxes.monthly_source_workflow import prepare_monthly_package

        package = prepare_monthly_package(
            organization=self.org, actor=self.users["finance"], period="2026-09"
        )
        url = reverse("taxes:monthly_source_review", args=[package.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {}).status_code, 403)
        self.client.force_login(self.users["owner"])
        self.assertContains(self.client.get(url), "Catat laporan pemeriksa")

    def sale(self):
        from finance import services as finance
        from finance.models import BankAccount, BankTransaction, Party, Product

        self._sale_counter = getattr(self, "_sale_counter", 0) + 1
        suffix = self._sale_counter
        party = Party.objects.create(
            organization=self.org, name=f"Synthetic mitra {suffix}", kind="reseller"
        )
        product = Product.objects.create(
            organization=self.org,
            code=f"ITP-{suffix}",
            name="Synthetic ITP",
            kind="itp",
            default_price="500000",
        )
        invoice = finance.create_invoice(
            organization=self.org,
            actor=self.users["owner"],
            party=party,
            product=product,
            quantity=1,
            date=date(2026, 9, 10),
            service_date=date(2026, 9, 10),
        )
        invoice = finance.issue_invoice(
            organization=self.org, actor=self.users["owner"], invoice=invoice
        )
        account = BankAccount.objects.create(
            organization=self.org,
            name=f"Synthetic bank {suffix}",
            account_number=f"MONTHLY-UI-{suffix}",
        )
        finance.import_bank_csv(
            organization=self.org,
            actor=self.users["owner"],
            account=account,
            content="date,reference,description,amount\n2026-09-10,UI-RECEIPT,Synthetic,500000\n",
        )
        bank = BankTransaction.objects.get(organization=self.org, account=account)
        finance.reconcile_receipt(
            organization=self.org,
            actor=self.users["owner"],
            invoice=invoice,
            transaction=bank,
            amount="500000",
        )
        finance.record_delivery(
            organization=self.org,
            actor=self.users["owner"],
            invoice=invoice,
            date=date(2026, 9, 10),
            evidence="Synthetic test attendance",
        )
        return invoice

    def test_nonempty_source_cards_link_invoice_and_keep_cash_separate(self):
        invoice = self.sale()
        response = self.client.post(
            reverse("taxes:monthly_sources"), {"period": "2026-09"}
        )
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(response.url)
        self.assertContains(detail, reverse("sale_detail", args=[invoice.pk]))
        self.assertContains(detail, "Uang bank:")
        self.assertContains(detail, "Informasi pendukung")

    def test_review_error_preserves_compact_row_choices_report_and_actual_adjustment(
        self,
    ):
        from taxes.models import ExternalTaxReview
        from taxes.monthly_source_workflow import prepare_monthly_package

        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.users["finance"], period="2026-09"
        )
        self.client.force_login(self.users["owner"])
        choices = [
            {
                "decision": "include" if row.get("proposed_inclusion") else "exclude",
                "reason": "Synthetic documented choice",
            }
            for row in package.snapshot["rows"]
        ]
        response = self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]),
            {
                "expected_fingerprint": package.package_hash,
                "expected_version": package.revision,
                "row_choices": json.dumps(choices),
                "inventory_note": "Synthetic all channels reviewed",
                "completeness_confirmed": "on",
                "professional_name": "Synthetic preserved professional",
                "qualification_reference": "Synthetic preserved engagement",
                "reviewed_on": "2026-10-09",
                "report_confirmed": "on",
                "adjustments-TOTAL_FORMS": "1",
                "adjustments-INITIAL_FORMS": "0",
                "adjustments-0-kind": "gross_basis",
                "adjustments-0-amount": "40.25",
                "adjustments-0-reason": "Synthetic preserved gross difference",
                "adjustments-0-source_row_ids": package.snapshot["rows"][0]["row_id"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synthetic preserved professional")
        self.assertContains(response, "Synthetic preserved gross difference")
        self.assertContains(response, 'value="40.25"')
        for index, choice in enumerate(choices):
            self.assertEqual(
                response.context["form"][f"source_{index}_decision"].value(),
                choice["decision"],
            )
            self.assertEqual(
                response.context["form"][f"source_{index}_reason"].value(),
                choice["reason"],
            )
        self.assertFalse(ExternalTaxReview.objects.exists())

    def test_source_routes_are_tenant_scoped_and_revoked_access_is_denied(self):
        from taxes.monthly_source_workflow import prepare_monthly_package

        other = Organization.objects.create(name="Other synthetic monthly company")
        Membership.objects.create(
            organization=other, user=self.users["owner"], role="owner"
        )
        foreign = prepare_monthly_package(
            organization=other, actor=self.users["owner"], period="2026-09"
        )
        self.client.force_login(self.users["owner"])
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()
        for name in (
            "monthly_source",
            "monthly_source_review",
            "monthly_source_export",
        ):
            self.assertEqual(
                self.client.get(
                    reverse("taxes:" + name, args=[foreign.pk])
                ).status_code,
                404,
            )
        self.assertEqual(
            self.client.post(
                reverse("taxes:monthly_source_generate", args=[foreign.pk]), {}
            ).status_code,
            404,
        )
        Membership.objects.filter(
            organization=self.org, user=self.users["owner"]
        ).update(is_active=False)
        self.assertEqual(
            self.client.get(reverse("taxes:monthly_sources")).status_code, 403
        )

    def documented(self, regime="final"):
        from taxes.tests_monthly_packages import MonthlySourcePackageTests

        helper = MonthlySourcePackageTests()
        helper.org, helper.owner = self.org, self.users["owner"]
        helper.profile(regime)
        return helper

    def review_data(self, package, helper, **changes):
        data = {
            "expected_fingerprint": package.package_hash,
            "expected_version": package.revision,
            "row_choices": json.dumps(
                [
                    {
                        "decision": "include"
                        if row.get("proposed_inclusion")
                        else "exclude",
                        "reason": ""
                        if row.get("proposed_inclusion")
                        else "Synthetic supporting source already counted in book income",
                    }
                    for row in package.snapshot["rows"]
                ]
            ),
            "positive_reason": "Synthetic common income treatment from the professional report",
            "proposed_confirmed": "on",
            "inventory_note": "Synthetic all products channels branches and off-ERP sources reviewed",
            "completeness_confirmed": "on",
            "evidence": helper.evidence().pk,
            "professional_name": "Synthetic web professional",
            "qualification_reference": "Synthetic verified engagement reference",
            "reviewed_on": "2026-10-09",
            "statement": "Synthetic report approves exact source decisions and supported monthly basis",
            "report_confirmed": "on",
            "self_payment_only_confirmed": "on",
            "object_code": "411128-420",
            "rule_reference": "Synthetic applicable fiscal rules",
            "adjustments-TOTAL_FORMS": "0",
            "adjustments-INITIAL_FORMS": "0",
        }
        data.update(changes)
        return data

    def test_documented_final_draft_uses_approved_sources_and_actual_delta_without_retyped_total(
        self,
    ):
        from decimal import Decimal

        from taxes.models import MonthlyTaxSourcePackage, TaxObligation

        helper = self.documented()
        self.sale()
        response = self.client.post(
            reverse("taxes:monthly_sources"), {"period": "2026-09"}
        )
        self.assertEqual(response.status_code, 302)
        package = MonthlyTaxSourcePackage.objects.get(organization=self.org)
        self.client.force_login(self.users["owner"])
        candidate = next(
            row for row in package.snapshot["rows"] if row.get("proposed_inclusion")
        )
        data = self.review_data(
            package,
            helper,
            **{
                "adjustments-TOTAL_FORMS": "1",
                "adjustments-0-kind": "gross_basis",
                "adjustments-0-amount": "40000",
                "adjustments-0-reason": "Synthetic documented gross contract difference",
                "adjustments-0-evidence": helper.evidence().pk,
                "adjustments-0-source_row_ids": candidate["row_id"],
            },
        )
        approved = self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]), data
        )
        self.assertEqual(
            approved.status_code,
            302,
            (approved.context["form"].errors, approved.context["adjustments"].errors)
            if approved.context
            else approved.content.decode()[:500],
        )
        detail = self.client.get(approved.url)
        self.assertContains(detail, "Buat draf pajak")
        self.assertContains(detail, "Pendapatan yang dimasukkan")
        self.assertContains(detail, "Perbedaan nilai kontrak / bruto")
        self.assertContains(detail, "Synthetic documented gross contract difference")
        self.assertEqual(
            str(detail.context["approved_bridge"]["book_amount"]), "500000.00"
        )
        self.assertEqual(
            detail.context["approved_bridge"]["adjustments"][0]["amount"], "40000"
        )
        self.assertEqual(detail.context["approved_bridge"]["amount"], "540000.00")
        self.assertContains(detail, "Lihat perlakuan setiap sumber")
        self.assertNotContains(detail, "transaksi di luar aplikasi belum diperiksa")
        self.assertEqual(detail.context["pending_source_count"], 0)
        generated = self.client.post(
            reverse("taxes:monthly_source_generate", args=[package.pk]),
            {
                "expected_fingerprint": package.package_hash,
                "expected_version": package.revision,
            },
        )
        self.assertEqual(generated.status_code, 302, generated.content.decode()[:500])
        obligation = TaxObligation.objects.get(
            organization=self.org, source_package=package
        )
        self.assertEqual(
            (obligation.base, obligation.amount), (Decimal(540000), Decimal(2700))
        )
        self.assertEqual(obligation.source_evidence_id, int(data["evidence"]))
        again = self.client.post(
            reverse("taxes:monthly_source_generate", args=[package.pk]),
            {
                "expected_fingerprint": package.package_hash,
                "expected_version": package.revision,
            },
        )
        self.assertEqual(again.url, generated.url)
        self.assertEqual(
            TaxObligation.objects.filter(
                organization=self.org, source_package=package
            ).count(),
            1,
        )

    def test_normal_nil_requires_documented_confirmation_and_never_offers_final_generation(
        self,
    ):
        from taxes.models import ExternalTaxReview, MonthlyTaxSourcePackage

        helper = self.documented("normal")
        self.client.post(reverse("taxes:monthly_sources"), {"period": "2026-09"})
        package = MonthlyTaxSourcePackage.objects.get(organization=self.org)
        self.client.force_login(self.users["owner"])
        data = self.review_data(package, helper)
        reviews_before = ExternalTaxReview.objects.count()
        rejected = self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]), data
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertFalse(hasattr(package, "approval"))
        self.assertEqual(ExternalTaxReview.objects.count(), reviews_before)
        data["nil_confirmed"] = "on"
        approved = self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]), data
        )
        self.assertEqual(approved.status_code, 302, approved.content.decode()[:500])
        detail = self.client.get(approved.url)
        self.assertContains(detail, "Tidak berlaku")
        self.assertNotContains(detail, "Buat draf pajak</button>")
        self.assertEqual(
            self.client.post(
                reverse("taxes:monthly_source_generate", args=[package.pk]),
                {
                    "expected_fingerprint": package.package_hash,
                    "expected_version": package.revision,
                },
            ).status_code,
            200,
        )
        self.assertFalse(package.obligations.exists())

    def test_stale_review_keeps_values_and_history_csv_while_new_version_is_prepared(
        self,
    ):
        from finance import services as finance
        from taxes.models import MonthlyTaxSourcePackage
        from taxes.monthly_source_workflow import prepare_monthly_package

        helper = self.documented()
        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.users["finance"], period="2026-09"
        )
        self.client.force_login(self.users["owner"])
        data = self.review_data(package, helper)
        finance.post_journal(
            organization=self.org,
            actor=self.users["owner"],
            date=date(2026, 9, 20),
            source_key="synthetic-later-source",
            description="Synthetic backdated source",
            entries=[("BANK", "25", "0"), ("REVENUE", "0", "25")],
        )
        response = self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]), data
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Siapkan versi terbaru")
        self.assertContains(response, "Synthetic web professional")
        self.assertFalse(hasattr(package, "approval"))
        old_csv = self.client.get(
            reverse("taxes:monthly_source_export", args=[package.pk])
        )
        self.assertEqual(old_csv.status_code, 200)
        self.assertIn(package.source_hash, old_csv.content.decode("utf-8"))
        self.assertIn("Data berubah", old_csv.content.decode("utf-8"))
        latest = self.client.post(
            reverse("taxes:monthly_sources"), {"period": "2026-09"}
        )
        self.assertEqual(latest.status_code, 302)
        second = MonthlyTaxSourcePackage.objects.get(organization=self.org, revision=2)
        self.assertEqual(second.previous_id, package.pk)
        self.assertContains(
            self.client.get(latest.url),
            reverse("taxes:monthly_source", args=[package.pk]),
        )

    def test_customer_final_withholding_cannot_create_duplicate_self_payment_draft(
        self,
    ):
        from taxes.models import ExternalTaxReview, MonthlyTaxSourcePackage

        helper = self.documented()
        self.sale()
        self.client.post(reverse("taxes:monthly_sources"), {"period": "2026-09"})
        package = MonthlyTaxSourcePackage.objects.get(organization=self.org)
        self.client.force_login(self.users["owner"])
        data = self.review_data(package, helper, customer_final_withholding="on")
        count = ExternalTaxReview.objects.count()
        response = self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]), data
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "pemotongan final pelanggan memerlukan alur tersendiri"
        )
        self.assertTrue(response.context["form"]["customer_final_withholding"].value())
        self.assertEqual(ExternalTaxReview.objects.count(), count)
        self.assertFalse(hasattr(package, "approval"))
        self.assertFalse(package.obligations.exists())

    def test_wrong_package_version_and_foreign_report_document_preserve_choices_without_approval(
        self,
    ):
        from taxes.models import ExternalTaxReview
        from taxes.monthly_source_workflow import prepare_monthly_package

        helper = self.documented()
        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.users["finance"], period="2026-09"
        )
        self.client.force_login(self.users["owner"])
        data = self.review_data(package, helper, expected_version=package.revision + 1)
        reviews_before = ExternalTaxReview.objects.count()
        response = self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]), data
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"]["expected_version"].value(),
            str(package.revision + 1),
        )
        self.assertEqual(ExternalTaxReview.objects.count(), reviews_before)
        self.assertFalse(hasattr(package, "approval"))
        other = Organization.objects.create(
            name="Synthetic foreign professional report"
        )
        Membership.objects.create(
            organization=other, user=self.users["owner"], role="owner"
        )
        helper.org = other
        data["expected_version"], data["evidence"] = (
            package.revision,
            helper.evidence().pk,
        )
        response = self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]), data
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"]["evidence"].errors)
        self.assertContains(response, "Synthetic web professional")
        self.assertEqual(ExternalTaxReview.objects.count(), reviews_before)

    def test_malformed_compact_choice_transport_fails_closed_without_losing_regular_report_fields(
        self,
    ):
        from taxes.models import ExternalTaxReview
        from taxes.monthly_source_workflow import prepare_monthly_package

        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.users["finance"], period="2026-09"
        )
        self.client.force_login(self.users["owner"])
        response = self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]),
            {
                "row_choices": '{"untrusted":"not a source list"}',
                "professional_name": "Synthetic retained name",
                "expected_fingerprint": package.package_hash,
                "expected_version": package.revision,
                "adjustments-TOTAL_FORMS": "0",
                "adjustments-INITIAL_FORMS": "0",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pilihan transaksi tidak lengkap")
        self.assertContains(response, "Synthetic retained name")
        self.assertFalse(ExternalTaxReview.objects.exists())

    def test_compact_adjustments_support_more_than_twenty_distinct_invoice_decisions(
        self,
    ):
        from decimal import Decimal

        from taxes.monthly_source_workflow import prepare_monthly_package

        helper = self.documented()
        for number in range(21):
            self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.users["finance"], period="2026-09"
        )
        self.client.force_login(self.users["owner"])
        proof = helper.evidence()
        actual = [
            {
                "kind": "gross_basis",
                "amount": "1",
                "reason": "Synthetic actual contract gross delta",
                "evidence": str(proof.pk),
                "source_row_ids": [row["row_id"]],
            }
            for row in package.snapshot["rows"]
            if row.get("proposed_inclusion")
        ]
        self.assertEqual(len(actual), 21)
        data = self.review_data(package, helper, adjustment_inputs=json.dumps(actual))
        response = self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]), data
        )
        self.assertEqual(
            response.status_code,
            302,
            (response.context["form"].errors, response.context["adjustments"].errors)
            if response.context
            else "",
        )
        detail = self.client.get(response.url)
        self.assertEqual(Decimal(detail.context["taxable_turnover"]), Decimal(10500021))
        self.assertEqual(len(detail.context["approval"].decision["adjustments"]), 21)

    def test_owner_can_correct_a_review_on_unchanged_sources_with_reason_and_history(
        self,
    ):
        from taxes.models import MonthlyTaxSourcePackage

        helper = self.documented()
        self.sale()
        self.client.post(reverse("taxes:monthly_sources"), {"period": "2026-09"})
        first = MonthlyTaxSourcePackage.objects.get(organization=self.org)
        self.client.force_login(self.users["owner"])
        approved = self.client.post(
            reverse("taxes:monthly_source_review", args=[first.pk]),
            self.review_data(first, helper),
        )
        self.assertEqual(approved.status_code, 302)
        url = reverse("taxes:monthly_source_correction", args=[first.pk])
        self.assertContains(self.client.get(approved.url), "Perbaiki hasil pemeriksaan")
        rejected = self.client.post(
            url,
            {
                "expected_fingerprint": first.package_hash,
                "expected_version": first.revision,
                "reason": "Synthetic corrected report classification",
            },
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(
            rejected.context["form"]["reason"].value(),
            "Synthetic corrected report classification",
        )
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(organization=self.org).count(), 1
        )
        revised = self.client.post(
            url,
            {
                "expected_fingerprint": first.package_hash,
                "expected_version": first.revision,
                "reason": "Synthetic corrected report classification",
                "confirmed": "on",
            },
        )
        self.assertEqual(revised.status_code, 302, revised.content.decode()[:500])
        second = MonthlyTaxSourcePackage.objects.get(organization=self.org, revision=2)
        self.assertEqual(second.previous_id, first.pk)
        self.assertEqual(second.source_hash, first.source_hash)
        self.assertEqual(second.snapshot, first.snapshot)
        self.assertEqual(
            revised.url, reverse("taxes:monthly_source_review", args=[second.pk])
        )
        self.assertContains(
            self.client.get(reverse("taxes:monthly_source", args=[first.pk])),
            "Versi ini sudah digantikan",
        )
        self.assertNotContains(
            self.client.get(reverse("taxes:monthly_source", args=[first.pk])),
            "Buat draf pajak</button>",
        )
        old_csv = self.client.get(
            reverse("taxes:monthly_source_export", args=[first.pk])
        )
        self.assertIn(first.package_hash, old_csv.content.decode())
        self.assertIn("Synthetic web professional", old_csv.content.decode())
        self.assertContains(
            self.client.get(reverse("taxes:monthly_source", args=[second.pk])),
            "Synthetic corrected report classification",
        )

    def test_review_correction_is_owner_only_scoped_and_csrf_protected(self):
        from taxes.monthly_source_workflow import prepare_monthly_package

        helper = self.documented()
        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.users["finance"], period="2026-09"
        )
        self.client.force_login(self.users["owner"])
        self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]),
            self.review_data(package, helper),
        )
        url = reverse("taxes:monthly_source_correction", args=[package.pk])
        for role in ("finance", "auditor", "reviewer", "marketing"):
            self.client.force_login(self.users[role])
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {}).status_code, 403)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.users["owner"])
        self.assertEqual(csrf.post(url, {}).status_code, 403)
        self.client.force_login(self.users["owner"])
        other = Organization.objects.create(
            name="Synthetic correction outside active company"
        )
        Membership.objects.create(
            organization=other, user=self.users["owner"], role="owner"
        )
        foreign = prepare_monthly_package(
            organization=other, actor=self.users["owner"], period="2026-09"
        )
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()
        foreign_url = reverse("taxes:monthly_source_correction", args=[foreign.pk])
        self.assertEqual(self.client.get(foreign_url).status_code, 404)
        self.assertEqual(self.client.post(foreign_url, {}).status_code, 404)

    def test_review_correction_preserves_reason_on_stale_identity_and_protects_approved_tax(
        self,
    ):
        from taxes.models import MonthlyTaxSourcePackage, TaxObligation
        from taxes.monthly_source_workflow import prepare_monthly_package

        helper = self.documented()
        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.users["finance"], period="2026-09"
        )
        self.client.force_login(self.users["owner"])
        data = self.review_data(package, helper)
        self.client.post(
            reverse("taxes:monthly_source_review", args=[package.pk]), data
        )
        url = reverse("taxes:monthly_source_correction", args=[package.pk])
        correction = {
            "expected_fingerprint": package.package_hash,
            "expected_version": package.revision + 1,
            "reason": "Synthetic retained correction for mistaken classification",
            "confirmed": "on",
        }
        rejected = self.client.post(url, correction)
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(
            rejected.context["form"]["reason"].value(), correction["reason"]
        )
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(organization=self.org).count(), 1
        )
        self.client.post(
            reverse("taxes:monthly_source_generate", args=[package.pk]),
            {
                "expected_fingerprint": package.package_hash,
                "expected_version": package.revision,
            },
        )
        obligation = TaxObligation.objects.get(source_package=package)
        approved = self.client.post(
            reverse("taxes:obligation_review", args=[obligation.pk]), data
        )
        self.assertEqual(approved.status_code, 302, approved.content.decode()[:500])
        correction["expected_version"] = package.revision
        rejected = self.client.post(url, correction)
        self.assertEqual(rejected.status_code, 200)
        self.assertFalse(rejected.context["can_review_revision"])
        self.assertEqual(
            rejected.context["form"]["reason"].value(), correction["reason"]
        )
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(organization=self.org).count(), 1
        )
        obligation.refresh_from_db()
        self.assertEqual(obligation.status, "approved")

    def test_corrected_review_requires_explicit_draft_withdrawal_then_generates_only_replacement(
        self,
    ):
        from decimal import Decimal

        from taxes.models import MonthlyTaxSourcePackage, TaxObligation
        from taxes.monthly_source_workflow import prepare_monthly_package

        helper = self.documented()
        self.sale()
        first = prepare_monthly_package(
            organization=self.org, actor=self.users["finance"], period="2026-09"
        )
        self.client.force_login(self.users["owner"])
        self.client.post(
            reverse("taxes:monthly_source_review", args=[first.pk]),
            self.review_data(first, helper),
        )
        self.client.post(
            reverse("taxes:monthly_source_generate", args=[first.pk]),
            {
                "expected_fingerprint": first.package_hash,
                "expected_version": first.revision,
            },
        )
        old = TaxObligation.objects.get(source_package=first)
        url = reverse("taxes:monthly_source_correction", args=[first.pk])
        values = {
            "expected_fingerprint": first.package_hash,
            "expected_version": first.revision,
            "reason": "Synthetic corrected report documents contract gross difference",
            "confirmed": "on",
        }
        blocked = self.client.post(url, values)
        self.assertEqual(blocked.status_code, 200)
        self.assertContains(
            blocked, reverse("taxes:obligation_withdraw", args=[old.pk])
        )
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(organization=self.org).count(), 1
        )
        withdrawn = self.client.post(
            reverse("taxes:obligation_withdraw", args=[old.pk]),
            {
                "expected_version": old.updated_at.isoformat(),
                "reason": values["reason"],
                "confirmed": "on",
            },
        )
        self.assertEqual(withdrawn.status_code, 302)
        revised = self.client.post(url, values)
        self.assertEqual(revised.status_code, 302, revised.content.decode()[:500])
        self.assertEqual(self.client.post(url, values).url, revised.url)
        second = MonthlyTaxSourcePackage.objects.get(organization=self.org, revision=2)
        candidate = next(
            row for row in second.snapshot["rows"] if row.get("proposed_inclusion")
        )
        approved = self.client.post(
            revised.url,
            self.review_data(
                second,
                helper,
                adjustment_inputs=json.dumps(
                    [
                        {
                            "kind": "gross_basis",
                            "amount": "40000",
                            "reason": "Synthetic corrected supported gross contract delta",
                            "evidence": str(helper.evidence().pk),
                            "source_row_ids": [candidate["row_id"]],
                        }
                    ]
                ),
            ),
        )
        self.assertEqual(approved.status_code, 302, approved.content.decode()[:500])
        generated = self.client.post(
            reverse("taxes:monthly_source_generate", args=[second.pk]),
            {
                "expected_fingerprint": second.package_hash,
                "expected_version": second.revision,
            },
        )
        self.assertEqual(generated.status_code, 302, generated.content.decode()[:500])
        replacement = TaxObligation.objects.get(source_package=second)
        self.assertEqual(
            (replacement.base, replacement.amount), (Decimal(540000), Decimal(2700))
        )
        old.refresh_from_db()
        self.assertEqual(old.status, "withdrawn")
        self.assertEqual(
            TaxObligation.objects.filter(organization=self.org)
            .exclude(status="withdrawn")
            .count(),
            1,
        )
        self.assertEqual(
            self.client.post(
                reverse("taxes:monthly_source_generate", args=[first.pk]),
                {
                    "expected_fingerprint": first.package_hash,
                    "expected_version": first.revision,
                },
            ).status_code,
            200,
        )
        self.assertEqual(first.approval.result["taxable_turnover"], "500000")
