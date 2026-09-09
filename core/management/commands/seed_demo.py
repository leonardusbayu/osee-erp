"""Create a separate synthetic workspace without touching live-company records."""
import calendar
from datetime import date, timedelta
from decimal import Decimal
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from core.models import Organization, Membership
from finance.models import Party, Product, PriceVersion, Bill, BankAccount, BankTransaction, Invoice
from finance import services
from taxes.models import TaxProfile
from taxes.services import seed_tax_sources

class Command(BaseCommand):
    help = "Isi workspace DEMO dengan transaksi sintetis, terpisah dari perusahaan aktif."

    @transaction.atomic
    def handle(self, *args, **options):
        from core.workspaces import real_import_exists
        if real_import_exists():
            self.stdout.write("Data perusahaan tersedia. Pengisian demo dilewati.")
            return
        if not settings.DEBUG or not settings.DEMO_MODE:
            raise CommandError("Demo seeding hanya tersedia pada mode lokal.")
        existing = Membership.objects.filter(user__username="osee_demo", organization__is_demo=True).first()
        seed_tax_sources()
        if existing:
            self.stdout.write("Workspace demo sudah tersedia; data tidak diulang atau ditimpa.")
            return
        if User.objects.filter(username="osee_demo").exists():
            raise CommandError("Nama akun demo sudah dipakai di luar workspace contoh.")
        user = User(username="osee_demo", first_name="Finance", last_name="OSEE")
        user.set_unusable_password()
        user.save()
        org = Organization.objects.create(name="PT Langkah Pintar Nusantara", brand_name="OSEE", is_demo=True)
        Membership.objects.create(user=user, organization=org, role="owner")
        TaxProfile.objects.create(organization=org)
        today = timezone.localdate()
        month = today.replace(day=1)
        start_ordinal = month.year * 12 + month.month - 1 - 5
        start_year, start_month = divmod(start_ordinal, 12)
        price_start = date(start_year, start_month + 1, 1)
        itp = Product.objects.create(organization=org, code="ITP", name="TOEFL ITP Official", kind="itp", default_price=Decimal("650000"))
        Product.objects.create(organization=org, code="IBT", name="TOEFL iBT Official", kind="ibt", default_price=Decimal("0"))
        Product.objects.create(organization=org, code="COURSE", name="English Course", kind="course", default_price=Decimal("0"))
        supplier = Party.objects.create(organization=org, name="IIEF · data contoh", kind="supplier")
        office = Party.objects.create(organization=org, name="Office supplies · data contoh", kind="supplier")
        direct = Party.objects.create(organization=org, name="Peserta langsung · data contoh", kind="customer")
        partners = []
        for name, price in [("Mitra Nusantara", "500000"), ("Bright English", "520000"), ("Lingua Academy", "530000"), ("Global Learning", "510000"), ("English Corner", "500000"), ("Cakrawala Edu", "530000")]:
            party = Party.objects.create(organization=org, name=name + " · contoh", kind="reseller")
            partners.append(party)
            PriceVersion.objects.create(organization=org, party=party, product=itp, kind="selling", amount=Decimal(price), effective_from=price_start)
        PriceVersion.objects.create(organization=org, party=supplier, product=itp, kind="supplier", amount=Decimal("450000"), effective_from=price_start)
        account = BankAccount.objects.create(organization=org, name="BNI Operasional · DEMO", account_number="DEMO-0001")
        for idx, quantity in enumerate([42, 56, 72, 61, 90, 12]):
            ordinal = start_ordinal + idx
            yr, mo = divmod(ordinal, 12)
            day = date(yr, mo + 1, min(2, today.day) if idx == 5 else 2)
            delivered = min(day + timedelta(days=2), today)
            inv = services.create_invoice(organization=org, party=partners[idx], product=itp, quantity=quantity, date=day, due_date=day, service_date=delivered, number=f"DEMO-INV-{yr}{mo+1:02d}-001", cost_estimate=Decimal(quantity) * 450000)
            services.issue_invoice(organization=org, invoice=inv)
            bank_line = BankTransaction.objects.create(organization=org, account=account, reference=f"DEMO-IN-{idx}", date=day, description=f"Contoh pelunasan {inv.number}", amount=inv.total)
            services.reconcile_receipt(organization=org, invoice=inv, transaction=bank_line, amount=inv.total)
            services.record_delivery(organization=org, invoice=inv, date=delivered)
            cost = Decimal(quantity) * 450000
            bill = Bill(organization=org, number=f"DEMO-COST-{idx}", supplier=supplier, amount=cost, date=delivered, service_date=delivered, category="provider", tax_status="reviewed", tax_amount=Decimal("0"))
            # Synthetic demonstration fixture only; real bills use documented tax review.
            bill._tax_service_transition = True
            bill.save()
            services.approve_bill(organization=org, bill=bill)
            debit = BankTransaction.objects.create(organization=org, account=account, reference=f"DEMO-OUT-{idx}", date=delivered, description=f"Contoh pembayaran supplier {bill.number}", amount=-cost)
            services.reconcile_bill_payment(organization=org, bill=bill, transaction=debit, amount=cost)
        # Upcoming orders remain service liabilities/receivables, not earned revenue.
        for idx, quantity in enumerate([18, 24, 8, 6]):
            inv = services.create_invoice(organization=org, party=partners[idx], product=itp, quantity=quantity, date=today, due_date=today + timedelta(days=2), service_date=today + timedelta(days=5 + idx), number=f"DEMO-OPEN-{idx + 1:03d}")
            if idx != 3:
                services.issue_invoice(organization=org, invoice=inv)
            if idx == 0:
                part = inv.total / 2
                line = BankTransaction.objects.create(organization=org, account=account, reference="DEMO-PARTIAL", date=today, description="Contoh pembayaran sebagian mitra", amount=part)
                services.reconcile_receipt(organization=org, invoice=inv, transaction=line, amount=part)
        BankTransaction.objects.create(organization=org, account=account, reference="DEMO-UNMATCHED-01", date=today, description="Contoh transfer masuk — perlu identifikasi", amount=Decimal("2650000"))
        BankTransaction.objects.create(organization=org, account=account, reference="DEMO-UNMATCHED-02", date=today, description="Contoh transfer masuk — referensi belum lengkap", amount=Decimal("1500000"))
        for idx, value in enumerate(["13500000", "650000", "2100000"]):
            Bill.objects.create(organization=org, number=f"DEMO-REVIEW-{idx+1:03d}", supplier=supplier if idx == 0 else office, amount=Decimal(value), date=today, service_date=today, category="provider" if idx == 0 else "expense")
        self.stdout.write(self.style.SUCCESS("Workspace DEMO siap. Semua transaksi contoh sintetis; tarif nol contoh bukan penetapan pajak OSEE. Tidak ada koneksi bank atau pelaporan pajak dijalankan."))
