"""Shared validation and scoping for the payroll service modules."""

from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from core.models import Membership, Organization

from .models import ZERO

CENT = Decimal("0.01")
WRITERS = {"owner", "finance"}
APPROVERS = {"owner", "director"}
READERS = WRITERS | {"director"}


def _org(organization):
    try:
        return Organization.objects.select_for_update().get(
            pk=getattr(organization, "pk", organization)
        )
    except (Organization.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError("Perusahaan tidak ditemukan.") from exc


def _authorize(organization, actor, roles):
    if not getattr(actor, "is_authenticated", False):
        raise PermissionDenied("Masuk dengan akun perusahaan untuk melanjutkan.")
    memberships = Membership.objects.filter(user_id=actor.pk, user__is_active=True)
    if not memberships.filter(organization=organization).exists():
        if memberships.exists():
            raise ValidationError(
                "Akun Anda tidak memiliki akses ke perusahaan yang dipilih."
            )
        raise PermissionDenied("Akun aktif perusahaan diperlukan untuk tindakan ini.")
    if not memberships.filter(organization=organization, role__in=roles).exists():
        raise PermissionDenied(
            "Peran akun Anda tidak memiliki akses ke data SDM/penggajian."
        )


def _scoped(model, value, organization):
    try:
        return model.objects.select_for_update().get(
            organization=organization, pk=getattr(value, "pk", value)
        )
    except (model.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError("Catatan tidak tersedia untuk perusahaan ini.") from exc


def _save(obj):
    obj._service_transition = True
    obj.save()
    return obj


def _money(value, *, positive=False):
    try:
        if isinstance(value, (float, bool)):
            raise InvalidOperation
        amount = Decimal(value)
        if (
            not amount.is_finite()
            or amount != amount.quantize(CENT)
            or abs(amount) >= Decimal(1000000000000000000)
        ):
            raise InvalidOperation
        if positive and amount <= ZERO:
            raise InvalidOperation
        if amount < ZERO:
            raise InvalidOperation
        return amount.quantize(CENT)
    except (InvalidOperation, ValueError, TypeError) as exc:
        qualifier = "lebih besar dari nol" if positive else "tidak boleh negatif"
        raise ValidationError(
            f"Jumlah harus berupa angka rupiah dengan maksimal dua desimal dan {qualifier}."
        ) from exc


def _period_values(year, month):
    if (
        type(year) is not int
        or type(month) is not int
        or not 2000 <= year <= 2200
        or not 1 <= month <= 12
    ):
        raise ValidationError("Pilih periode penggajian yang valid.")
    today = timezone.localdate()
    if (year, month) > (today.year, today.month):
        raise ValidationError("Periode payroll tidak boleh di masa depan.")
    return year, month


def _version(run, expected):
    if type(expected) is not int or expected != run.version:
        raise ValidationError(
            "Data payroll berubah. Muat ulang halaman sebelum melanjutkan."
        )


def _reason(value, label, *, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValidationError(f"{label} wajib diisi, maksimal {limit} karakter.")
    return value.strip()


def _totals(run):
    rows = list(run.lines.all())
    gross = sum((row.gross_amount for row in rows), ZERO)
    tax = sum((row.pph21_amount for row in rows), ZERO)
    employee_bpjs = sum((row.bpjs_employee_amount for row in rows), ZERO)
    employer_bpjs = sum((row.bpjs_employer_amount for row in rows), ZERO)
    return {
        "people_count": len(rows),
        "gross": gross,
        "pph21": tax,
        "bpjs_employee": employee_bpjs,
        "bpjs_employer": employer_bpjs,
        "bpjs_total": employee_bpjs + employer_bpjs,
        "net_pay": gross - tax - employee_bpjs,
        "employer_cost": gross + employer_bpjs,
    }
