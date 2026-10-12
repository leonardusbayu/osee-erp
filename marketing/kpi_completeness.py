"""Human source inventory plus live Finance evidence gates business KPI approval."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .kpi_models import KpiSourcePeriod, KpiTarget
from .services import _audit, _fields, _org, _save, _scoped, _snapshot, _version

CASH_METRICS = {
    "cash_in",
    "cash_to_spend",
    "cohort_paid_leads",
    "cohort_paid_conversion",
    "cohort_fully_paid_leads",
    "cohort_fully_paid_conversion",
    "fully_paid_invoice_units",
    "paying_partners",
}
FOLLOWUP_METRICS = {"followup_on_time_percent"}
COUNT_FIELDS = (
    "expected_count",
    "imported_count",
    "duplicate_count",
    "excluded_count",
    "unresolved_count",
)


def _sources(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 50:
        raise ValidationError(
            "Daftar 1–50 sumber yang mencakup lingkup target wajib diisi."
        )
    result, identities = [], set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {
            "kind",
            "name",
            "reference",
            "evidence",
            *COUNT_FIELDS,
        }:
            raise ValidationError(
                "Setiap sumber membutuhkan jenis, nama, referensi, bukti, dan seluruh jumlah kontrol."
            )
        row = {}
        for key in ("kind", "name", "reference", "evidence"):
            text = value[key]
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > (4000 if key == "evidence" else 180)
                or "\x00" in text
            ):
                raise ValidationError("Identitas dan bukti sumber wajib valid.")
            row[key] = text.strip()
        if (
            row["kind"] not in {"leads", "bank", "followups"}
            or (row["kind"], row["reference"].casefold()) in identities
        ):
            raise ValidationError(
                "Jenis sumber tidak dikenal atau referensi sumber dicatat dua kali."
            )
        identities.add((row["kind"], row["reference"].casefold()))
        for key in COUNT_FIELDS:
            if type(value[key]) is not int or not 0 <= value[key] <= 2147483647:
                raise ValidationError("Jumlah kontrol harus bilangan bulat nonnegatif.")
            row[key] = value[key]
        result.append(row)
    return result


def source_period_state(target, provenance, *, spend_complete=False):
    if target.metric_key == "spend":
        return {
            "state": "complete" if spend_complete else "incomplete",
            "complete": bool(spend_complete),
            "label": "Cakupan biaya kampanye sudah diperiksa"
            if spend_complete
            else "Cakupan biaya kampanye perlu diperiksa ulang atau dilengkapi",
            "period_id": None,
        }
    periods = target.source_periods.filter(status="approved").order_by(
        "-approved_at", "-pk"
    )
    period = periods.filter(data_fingerprint=provenance["digest"]).first()
    if period is None:
        stale = periods.exists()
        return {
            "state": "stale" if stale else "missing",
            "complete": False,
            "period_id": None,
            "label": "Sumber berubah; periode perlu diperiksa ulang"
            if stale
            else "Kelengkapan seluruh sumber belum diperiksa",
        }
    result = {
        "state": "complete",
        "complete": True,
        "label": "Daftar seluruh sumber dan jumlah kontrol diperiksa",
        "period_id": period.pk,
        "approved_at": period.approved_at.isoformat(),
        "approved_by_id": period.approved_by_id,
    }
    if target.metric_key in CASH_METRICS:
        from finance.statement_services import statement_coverage

        coverage = statement_coverage(
            organization=target.organization,
            start=target.period_start,
            end=target.period_end,
        )
        result["bank_fingerprint"] = coverage["fingerprint"]
        if not coverage["verified"]:
            result.update(
                state="bank_incomplete",
                complete=False,
                label="Finance belum mengesahkan kelengkapan mutasi rekening periode ini",
            )
        elif period.bank_fingerprint != coverage["fingerprint"]:
            result.update(
                state="stale",
                complete=False,
                label="Dasar pemeriksaan bank berubah; sahkan kembali daftar sumber KPI",
            )
    if target.metric_key == "cash_to_spend":
        result["ad_source_complete"] = bool(spend_complete)
        if not spend_complete:
            result.update(
                state="ad_incomplete",
                complete=False,
                label="Cakupan biaya kampanye perlu diperiksa; daftar prospek dan bank tidak membuktikan kelengkapan biaya",
            )
    return result


@transaction.atomic
def create_source_period(
    *,
    organization,
    actor,
    target,
    reference,
    sources,
    inventory_note,
    confirm_all_sources=False,
):
    from .kpi import _text, evaluate_target

    org = _org(organization, actor)
    obj = _scoped(KpiTarget, target, org)
    if confirm_all_sources is not True:
        raise ValidationError(
            "Konfirmasikan bahwa daftar memuat seluruh sumber yang relevan, bukan hanya berkas yang sudah diimpor."
        )
    fields = _fields({"reference": reference}, {"reference"}, org)
    fields["inventory_note"] = _text(
        inventory_note, label="Daftar seluruh sumber", maximum=4000
    )
    if not fields["reference"] or not fields["inventory_note"]:
        raise ValidationError(
            "Referensi pemeriksaan dan uraian seluruh sumber wajib diisi."
        )
    rows = _sources(sources)
    evaluation = evaluate_target(obj)
    bank_fingerprint = ""
    if obj.metric_key in CASH_METRICS:
        from finance.statement_services import statement_coverage

        bank_fingerprint = statement_coverage(
            organization=org, start=obj.period_start, end=obj.period_end
        )["fingerprint"]
    period = _save(
        KpiSourcePeriod(
            organization=org,
            created_by=actor,
            target=obj,
            sources=rows,
            data_fingerprint=evaluation["basis"]["provenance"]["digest"],
            bank_fingerprint=bank_fingerprint,
            **fields,
        )
    )
    _audit(
        org,
        actor,
        "kpi.sources_declared",
        period,
        target_id=obj.pk,
        source_count=len(rows),
        fingerprint=period.data_fingerprint,
    )
    return period


@transaction.atomic
def approve_source_period(
    *, organization, actor, period, expected_version, expected_fingerprint, reason
):
    from .kpi import _text, evaluate_target

    org = _org(organization, actor, {"owner", "director"})
    period = _scoped(KpiSourcePeriod, period, org)
    _version(period, expected_version)
    reason = _text(reason, label="Catatan pemeriksaan", maximum=4000)
    if period.status != "draft" or period.target.period_end >= timezone.localdate():
        raise ValidationError(
            "Pemeriksaan final memerlukan draf dan periode yang telah berakhir."
        )
    evaluation = evaluate_target(period.target)
    provenance = evaluation["basis"]["provenance"]
    if (
        expected_fingerprint != period.data_fingerprint
        or provenance["digest"] != expected_fingerprint
    ):
        raise ValidationError(
            "Data berubah sejak sumber didaftarkan. Buat pemeriksaan pengganti dengan data terkini."
        )
    required = (
        {"leads"}
        | ({"bank"} if period.target.metric_key in CASH_METRICS else set())
        | ({"followups"} if period.target.metric_key in FOLLOWUP_METRICS else set())
    )
    kinds = {row["kind"] for row in period.sources}
    if not required.issubset(kinds):
        raise ValidationError(
            "Sumber prospek, janji follow-up, dan untuk metrik pembayaran mutasi bank harus diperiksa."
        )
    for row in period.sources:
        if (
            row["unresolved_count"]
            or row["expected_count"]
            != row["imported_count"] + row["duplicate_count"] + row["excluded_count"]
        ):
            raise ValidationError(
                "Jumlah sumber belum cocok atau masih ada pengecualian terbuka."
            )
    provenance_sources = provenance["sources"]
    lead_manifest = provenance_sources.get("leads", {})
    actual_count = lead_manifest.get("count", 0)
    if (
        sum(row["imported_count"] for row in period.sources if row["kind"] == "leads")
        != actual_count
    ):
        raise ValidationError(
            "Jumlah prospek yang dinyatakan masuk harus cocok dengan prospek pada lingkup KPI."
        )
    if period.target.metric_key in FOLLOWUP_METRICS:
        followup_manifest = provenance_sources.get("followups", {})
        promise_count = followup_manifest.get("count", 0)
        if (
            sum(
                row["imported_count"]
                for row in period.sources
                if row["kind"] == "followups"
            )
            != promise_count
        ):
            raise ValidationError(
                "Jumlah janji follow-up yang dinyatakan masuk harus cocok dengan seluruh janji jatuh tempo pada lingkup KPI."
            )
    if period.target.metric_key in CASH_METRICS:
        from finance.models import BankTransaction
        from finance.statement_services import statement_coverage

        bank_coverage = statement_coverage(
            organization=org,
            start=period.target.period_start,
            end=period.target.period_end,
        )
        if not bank_coverage["verified"]:
            raise ValidationError(
                "Selesaikan pemeriksaan cakupan mutasi melalui Finance sebelum mengesahkan KPI pembayaran."
            )
        if period.bank_fingerprint != bank_coverage["fingerprint"]:
            raise ValidationError(
                "Dasar mutasi bank berubah sejak daftar sumber dibuat. Buat daftar sumber pengganti setelah memeriksa perubahan Finance."
            )
        bank_count = BankTransaction.objects.filter(
            organization=org,
            date__range=(period.target.period_start, period.target.period_end),
        ).count()
        if (
            sum(
                row["imported_count"] for row in period.sources if row["kind"] == "bank"
            )
            != bank_count
        ):
            raise ValidationError(
                "Jumlah mutasi dinyatakan masuk harus cocok dengan seluruh rekening pada periode ini."
            )
        period.bank_fingerprint = bank_coverage["fingerprint"]
    period.status, period.approved_by, period.approved_at = (
        "approved",
        actor,
        timezone.now(),
    )
    period.review_notes, period.version = reason, period.version + 1
    _save(period)
    _audit(
        org,
        actor,
        "kpi.sources_approved",
        period,
        target_id=period.target_id,
        fingerprint=period.data_fingerprint,
        reason=reason,
    )
    return period


@transaction.atomic
def withdraw_source_period(*, organization, actor, period, expected_version, reason):
    from .kpi import _text

    org = _org(organization, actor, {"owner", "director"})
    period = _scoped(KpiSourcePeriod, period, org)
    _version(period, expected_version)
    reason = _text(reason, label="Alasan penarikan", maximum=4000)
    if period.status == "withdrawn":
        raise ValidationError("Pemeriksaan ini sudah ditarik.")
    before = _snapshot(period, {"status", "review_notes", "version"})
    period.status, period.review_notes, period.version = (
        "withdrawn",
        reason,
        period.version + 1,
    )
    _save(period)
    _audit(
        org,
        actor,
        "kpi.sources_withdrawn",
        period,
        target_id=period.target_id,
        before=before,
        reason=reason,
    )
    return period
