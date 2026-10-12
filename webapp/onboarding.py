"""Guided company preparation using existing records and authorized workflows."""

from datetime import timedelta

from django.db.models import Q
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.access import organization_required, require_role
from core.models import Membership
from finance.models import BankAccount, PriceVersion, Product
from finance.statement_services import statement_coverage
from imports.models import ImportBatch, ImportException
from imports.review_services import exception_states
from marketing.kpi_models import KpiTarget
from marketing.models import TeamMember
from marketing.workflow import current_work_policy
from operations.models import IntakeBatch, Registration


def preparation_steps(organization):
    """Read-only counts are preparation signals, never an overall release approval."""
    today = timezone.localdate()
    previous_end = today.replace(day=1) - timedelta(days=1)
    staff = Membership.objects.filter(organization=organization, user__is_active=True)
    finance = staff.filter(role="finance").count()
    marketers = staff.filter(role="marketing")
    marketing = marketers.count()
    profiles = TeamMember.objects.filter(organization=organization, active=True)
    mapped = (
        profiles.filter(account_id__in=marketers.values("user_id"))
        .values("account_id")
        .distinct()
        .count()
    )
    unmapped = profiles.filter(
        Q(account__isnull=True) | ~Q(account_id__in=staff.values("user_id"))
    ).count()
    policy = current_work_policy(organization)
    products = Product.objects.filter(organization=organization)
    price_ids = (
        PriceVersion.objects.filter(
            organization=organization, kind="selling", effective_from__lte=today
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
        .values("product_id")
    )
    priced = (
        products.filter(Q(default_price__gt=0) | Q(pk__in=price_ids)).distinct().count()
    )
    registrations = Registration.objects.filter(organization=organization).count()
    intake = IntakeBatch.objects.filter(organization=organization).count()
    targets = KpiTarget.objects.filter(
        organization=organization,
        status="active",
        period_start__lte=today,
        period_end__gte=today,
    )
    categories = [
        targets.filter(metric_key__in=keys).exists()
        for keys in (
            ("leads", "cash_in", "cohort_paid_leads", "cohort_paid_conversion"),
            ("spend", "cash_to_spend"),
            ("response_coverage_percent", "median_response_minutes"),
        )
    ]
    accounts = BankAccount.objects.filter(organization=organization).count()
    coverage = statement_coverage(
        organization=organization, start=previous_end.replace(day=1), end=previous_end
    )
    states = exception_states(
        organization=organization,
        exceptions=ImportException.objects.filter(organization=organization),
    )
    unresolved = sum(
        value["status"] != "resolved" or value["requires_recheck"]
        for value in states.values()
    )
    source_batches = ImportBatch.objects.filter(organization=organization).exists()

    def step(key, title, recorded, detail, description, actions):
        return {
            "key": key,
            "title": title,
            "recorded": recorded,
            "status": "Tercatat" if recorded else "Perlu dilengkapi",
            "detail": detail,
            "description": description,
            "actions": [
                {"label": label, "url": reverse(route), "owner_only": owner}
                for label, route, owner in actions
            ],
        }

    return [
        step(
            "accounts",
            "1. Akun pribadi tim",
            finance > 0 and marketing > 0,
            f"{finance} akun Finance dan {marketing} akun Marketing aktif.",
            "Owner membuat akun setiap staf, memilih peran, dan membagikan sandi melalui saluran internal yang aman.",
            [
                ("Tambah akun staf", "member_create", True),
                ("Kelola akses", "settings", True),
            ],
        ),
        step(
            "pic",
            "2. Hubungkan PIC dengan akun",
            marketing > 0 and mapped == marketing and not unmapped,
            f"{mapped} dari {marketing} akun Marketing memiliki PIC aktif; {unmapped} profil belum terhubung ke akun aktif.",
            "Buka nama anggota, lalu Owner menghubungkan akun. Alih tugas dan delegasi menggunakan riwayat tanggung jawab.",
            [("Buka anggota tim", "marketing:members", False)],
        ),
        step(
            "calendar",
            "3. Kalender kerja dan batas waktu",
            policy is not None,
            f"Kebijakan versi {policy.version}: {policy.work_start:%H:%M}–{policy.work_end:%H:%M} WIB."
            if policy
            else "Belum ada kalender kerja yang disahkan.",
            "Tetapkan hari/jam kerja, hari libur, batas respons, dan batas pemeriksaan sesuai keputusan perusahaan.",
            [("Tetapkan kalender kerja", "marketing:workflow_policy", True)],
        ),
        step(
            "prices",
            "4. Produk dan harga saat ini",
            products.exists() and priced == products.count(),
            f"{priced} dari {products.count()} produk memiliki harga jual yang tercatat untuk hari ini.",
            "Owner memeriksa harga ITP, iBT, kursus, dan perjanjian tiap mitra. Harga tercatat belum membuktikan seluruh perjanjian sudah diperiksa.",
            [
                ("Periksa produk dan harga", "prices", False),
                ("Periksa mitra", "partners", False),
            ],
        ),
        step(
            "registrations",
            "5. Pendaftaran dan pesanan",
            registrations > 0,
            f"{intake} berkas pendaftaran dan {registrations} pendaftaran peserta tercatat.",
            "Unggah CSV/Excel, cocokkan kolom, periksa duplikat, lalu pilih pemesan serta produk. Bukti pembayaran diproses terpisah oleh Finance.",
            [
                ("Impor pendaftaran", "operations:intake_upload", False),
                ("Buka pesanan", "operations:overview", False),
            ],
        ),
        step(
            "bank",
            "6. Sumber bank periode terakhir",
            coverage["verified"],
            f"{accounts} rekening tercatat. Cakupan yang diperiksa: {previous_end.replace(day=1):%d/%m/%Y}–{previous_end:%d/%m/%Y}.",
            "Finance mengimpor e-statement, mencocokkan seluruh baris, lalu mengesahkan kontrol jumlah dan saldo. Saldo buku besar tetap diperiksa pada tutup buku.",
            [("Impor dan periksa mutasi", "bank", False)],
        ),
        step(
            "exceptions",
            "7. Selesaikan pengecualian sumber",
            source_batches and unresolved == 0,
            f"{unresolved} pengecualian sumber belum diselesaikan."
            if source_batches
            else "Belum ada impor sumber keuangan yang dapat diperiksa.",
            "Tetapkan penanggung jawab dan simpan keputusan dengan bukti. Penyelesaian menjaga sumber dan riwayat sebelumnya.",
            [("Periksa pengecualian", "import_exception_inbox", False)],
        ),
        step(
            "targets",
            "8. Target marketing yang seimbang",
            all(categories),
            f"{sum(categories)} dari 3 area mempunyai target aktif periode berjalan: penjualan, biaya, dan disiplin respons.",
            "Tim mengusulkan nilai target dan Owner mengesahkan. Nilai perusahaan tidak diisi otomatis; daftar seluruh sumber harus diperiksa sebelum evaluasi resmi.",
            [
                ("Susun target", "marketing:kpi_create", False),
                ("Periksa target dan sumber", "marketing:kpis", False),
            ],
        ),
    ]


@organization_required
@require_http_methods(["GET"])
def onboarding(request):
    require_role(request, "owner", "finance")
    steps = preparation_steps(request.organization)
    is_owner = request.membership.role == "owner"
    next_step = next((step for step in steps if not step["recorded"]), None)
    next_action = (
        next(
            (
                action
                for action in next_step["actions"]
                if is_owner or not action["owner_only"]
            ),
            None,
        )
        if next_step
        else None
    )
    return render(
        request,
        "app/onboarding.html",
        {
            "steps": steps,
            "active_nav": "onboarding",
            "recorded_steps": sum(step["recorded"] for step in steps),
            "is_owner": is_owner,
            "next_step": next_step,
            "next_action": next_action,
        },
    )


urlpatterns = [path("settings/get-started/", onboarding, name="onboarding")]
