"""Versioned marketing commitments and immutable, source-derived reviews."""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from director.models import CHANNEL_CHOICES, PlanningRecord

KPI_METRICS = {
    "leads": {
        "label": "Prospek masuk tercatat",
        "unit": "prospek",
        "direction": "higher",
        "description": "Jumlah prospek yang diterima dalam periode dan lingkup target.",
    },
    "cash_in": {
        "label": "Kas masuk bruto terhubung",
        "unit": "IDR",
        "direction": "higher",
        "description": "Kas bank teralokasi ke invoice prospek, menurut tanggal mutasi. Kredit pajak bukan kas. KPI ini tetap bruto; refund invoice aktual tersedia terpisah dan tidak mengubah target bruto ini.",
    },
    "cohort_paid_leads": {
        "label": "Prospek dengan pembayaran (termasuk sebagian)",
        "unit": "prospek",
        "direction": "higher",
        "description": "Prospek masuk dalam periode yang memiliki pembayaran diterapkan ke invoice hingga akhir pengamatan. Pembayaran sebagian tetap dihitung; bukan jumlah lunas.",
    },
    "cohort_paid_conversion": {
        "label": "Konversi ke pembayaran (termasuk sebagian)",
        "unit": "%",
        "direction": "higher",
        "description": "Prospek dengan pembayaran sebagian atau penuh dibagi seluruh prospek masuk dalam periode, kali 100.",
    },
    "cohort_fully_paid_leads": {
        "label": "Prospek dengan pesanan lunas penuh",
        "unit": "prospek",
        "direction": "higher",
        "description": "Prospek periode dengan seluruh invoice aktif bernilai neto telah dilunasi sampai akhir pengamatan. Pembayaran sebagian tidak dihitung lunas; invoice yang dibatalkan atau bernilai neto nol tidak dihitung.",
    },
    "cohort_fully_paid_conversion": {
        "label": "Konversi ke pesanan lunas penuh",
        "unit": "%",
        "direction": "higher",
        "description": "Prospek dengan seluruh invoice aktif bernilai neto telah dilunasi dibagi seluruh prospek masuk dalam periode, kali 100. Pembayaran sebagian tidak dihitung lunas.",
    },
    "followup_on_time_percent": {
        "label": "Janji follow-up tepat waktu",
        "unit": "%",
        "direction": "higher",
        "description": "Janji yang jatuh tempo dalam periode dan memiliki bukti penyelesaian yang dicatat serta terjadi selambatnya pada tenggat, dibagi seluruh janji jatuh tempo. Koreksi setelah tenggat tidak dapat mengubah hasil menjadi tepat waktu.",
    },
    "response_coverage_percent": {
        "label": "Kelengkapan respons pertama",
        "unit": "%",
        "direction": "higher",
        "description": "Prospek masuk dalam periode dengan waktu respons pertama tercatat hingga akhir pengamatan dibagi seluruh prospek periode, kali 100.",
    },
    "median_response_minutes": {
        "label": "Batas median respons pertama",
        "unit": "menit",
        "direction": "lower",
        "description": "Median menit dari prospek diterima sampai respons pertama. Skor hanya tersedia bila semua prospek periode mempunyai respons tercatat.",
    },
    "spend": {
        "label": "Batas belanja iklan terkonfirmasi",
        "unit": "IDR",
        "direction": "lower",
        "description": "Belanja iklan periode dari sumber kampanye yang telah dikonfirmasi lengkap. Belum mencakup seluruh biaya pemasaran perusahaan.",
    },
    "cash_to_spend": {
        "label": "Rasio kas bruto terhadap belanja iklan",
        "unit": "kali",
        "direction": "higher",
        "description": "Kas bruto terhubung dibagi belanja iklan terkonfirmasi dalam periode yang sama. Bukan laba, ROAS kausal, atau pengembalian cohort.",
    },
}


class KpiTarget(PlanningRecord):
    title = models.CharField(max_length=200)
    metric_key = models.CharField(
        max_length=60,
        choices=[(key, spec["label"]) for key, spec in KPI_METRICS.items()],
    )
    target = models.DecimalField(
        max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal(0))]
    )
    period_start = models.DateField()
    period_end = models.DateField()
    member = models.ForeignKey(
        "marketing.TeamMember",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="kpi_targets",
    )
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, blank=True)
    campaign = models.ForeignKey(
        "marketing.Campaign",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="kpi_targets",
    )
    rationale = models.TextField(max_length=4000)
    status = models.CharField(
        max_length=20,
        choices=[("draft", "Draf"), ("active", "Aktif"), ("retired", "Dihentikan")],
        default="draft",
    )
    version = models.PositiveIntegerField(default=1)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    scoped_references = ("member", "campaign")

    class Meta:
        ordering = ["-period_start", "-pk"]

    @property
    def metric_label(self):
        metric = KPI_METRICS.get(self.metric_key)
        return metric["label"] if metric else self.metric_key

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_kpi_transition", None)
            self.__dict__.pop("_service_write", None)

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(
            old,
            (
                "title",
                "metric_key",
                "target",
                "period_start",
                "period_end",
                "member_id",
                "channel",
                "campaign_id",
                "rationale",
            ),
        )
        if old and not getattr(self, "_kpi_transition", False):
            self._unchanged(old, ("status", "version", "approved_by_id", "approved_at"))
        if not old and (
            self.status != "draft"
            or self.approved_by_id
            or self.approved_at
            or self.version != 1
        ):
            raise ValidationError(
                "Target baru harus berupa draf; gunakan persetujuan direktur untuk mengaktifkan."
            )
        if old and old.approved_at:
            self._unchanged(old, ("approved_by_id", "approved_at"))
        if old and old.status == "retired":
            self._unchanged(old, ("status", "version"))
        if not self.rationale.strip():
            raise ValidationError({"rationale": "Tuliskan dasar dan tujuan target."})
        if (
            self.period_start
            and self.period_end
            and (
                not 2000 <= self.period_start.year <= self.period_end.year <= 2200
                or not 0 <= (self.period_end - self.period_start).days <= 365
            )
        ):
            raise ValidationError(
                "Periode target harus berurutan, maksimal 366 hari, tahun 2000–2200."
            )
        if self.target is not None:
            if self.target < 0 or (self.target == 0 and self.metric_key != "spend"):
                raise ValidationError(
                    {"target": "Target harus positif; hanya batas belanja dapat nol."}
                )
            if (
                self.metric_key
                in {"leads", "cohort_paid_leads", "cohort_fully_paid_leads"}
                and self.target != self.target.to_integral_value()
            ):
                raise ValidationError(
                    {"target": "Target jumlah prospek harus bilangan bulat."}
                )
            if (
                self.metric_key
                in {
                    "cohort_paid_conversion",
                    "cohort_fully_paid_conversion",
                    "followup_on_time_percent",
                    "response_coverage_percent",
                }
                and self.target > 100
            ):
                raise ValidationError({"target": "Target persentase maksimal 100."})
        if self.member_id and self.metric_key in {"spend", "cash_to_spend"}:
            raise ValidationError(
                {
                    "member": "Belanja iklan belum dapat dipetakan ke penanggung jawab prospek."
                }
            )
        if self.campaign_id and self.channel != self.campaign.channel:
            raise ValidationError({"channel": "Kanal target harus sesuai kampanye."})
        if self.status == "active" and self.period_start and self.period_end:
            duplicate = (
                type(self)
                .objects.filter(
                    organization_id=self.organization_id,
                    status="active",
                    metric_key=self.metric_key,
                    member_id=self.member_id,
                    channel=self.channel,
                    campaign_id=self.campaign_id,
                    period_start__lte=self.period_end,
                    period_end__gte=self.period_start,
                )
                .exclude(pk=self.pk)
            )
            if duplicate.exists():
                raise ValidationError(
                    "Sudah ada target aktif untuk metrik dan lingkup yang sama pada periode yang tumpang tindih."
                )


class KpiReview(PlanningRecord):
    target = models.ForeignKey(
        KpiTarget, on_delete=models.PROTECT, related_name="reviews"
    )
    actual = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    snapshot = models.JSONField()
    fingerprint = models.CharField(max_length=64)
    conclusion = models.TextField(max_length=4000)
    scoped_references = ("target",)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def save(self, *args, **kwargs):
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_service_write", None)

    def clean(self):
        super().clean()
        self._unchanged(
            self._old(),
            ("target_id", "actual", "snapshot", "fingerprint", "conclusion"),
        )
        if not self.conclusion.strip():
            raise ValidationError(
                {"conclusion": "Catat kesimpulan dan tindak lanjut evaluasi."}
            )


class KpiSourcePeriod(PlanningRecord):
    target = models.ForeignKey(
        KpiTarget, on_delete=models.PROTECT, related_name="source_periods"
    )
    reference = models.CharField(max_length=180)
    sources = models.JSONField()
    inventory_note = models.TextField(max_length=4000)
    data_fingerprint = models.CharField(max_length=64)
    bank_fingerprint = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ("draft", "Draf"),
            ("approved", "Diperiksa"),
            ("withdrawn", "Ditarik"),
        ],
        default="draft",
    )
    version = models.PositiveIntegerField(default=1)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(max_length=4000, blank=True)
    scoped_references = ("target",)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"],
                name="marketing_kpi_period_reference",
            )
        ]

    def save(self, *args, **kwargs):
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_service_write", None)

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(
            old,
            ("target_id", "reference", "sources", "inventory_note", "data_fingerprint"),
        )
        if old and old.status != "draft":
            self._unchanged(old, ("bank_fingerprint", "approved_by_id", "approved_at"))
        if old and old.status == "withdrawn":
            self._unchanged(
                old,
                ("status", "version", "approved_by_id", "approved_at", "review_notes"),
            )
