"""Friendly, tenant-scoped forms for team and sales work."""

from django import forms
from django.contrib.auth import get_user_model

from core.models import Membership

from .models import TeamMember
from .workflow_models import (
    CONTACT_METHODS,
    CONTACT_OUTCOMES,
    LOSS_REASONS,
    LeadContact,
)


class WorkflowForm(forms.Form):
    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        for name, field in self.fields.items():
            if name in {"account", "delegate"}:
                ids = Membership.objects.filter(
                    organization=organization,
                    user__is_active=True,
                    role__in={"owner", "director", "marketing", "finance"},
                ).values("user_id")
                field.queryset = (
                    get_user_model().objects.filter(pk__in=ids).order_by("username")
                )
            elif isinstance(field, forms.ModelChoiceField):
                field.queryset = field.queryset.filter(organization=organization)
                if name == "member":
                    field.queryset = field.queryset.filter(active=True)
            if isinstance(field, forms.DateTimeField):
                field.widget = forms.DateTimeInput(
                    format="%Y-%m-%dT%H:%M:%S",
                    attrs={"type": "datetime-local", "step": "1"},
                )
                field.help_text = (
                    field.help_text + " Waktu Indonesia Barat (WIB)."
                ).strip()
            elif isinstance(field, forms.DateField):
                field.widget = forms.DateInput(
                    format="%Y-%m-%d", attrs={"type": "date"}
                )


class MemberAccountForm(WorkflowForm):
    account = forms.ModelChoiceField(
        label="Akun pribadi anggota",
        queryset=get_user_model().objects.none(),
        required=False,
        help_text="Satu akun untuk satu anggota pada perusahaan ini. Kosong melepas hubungan akun; histori tetap ada.",
    )
    active = forms.BooleanField(label="Anggota masih aktif", required=False)
    reason = forms.CharField(
        label="Alasan pengaturan akun / status",
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class DelegationForm(WorkflowForm):
    delegate = forms.ModelChoiceField(
        label="Akun pengganti", queryset=get_user_model().objects.none()
    )
    starts_on = forms.DateField(label="Mulai berlaku")
    ends_on = forms.DateField(label="Berlaku sampai")
    reason = forms.CharField(
        label="Alasan dan pekerjaan yang perlu dibantu",
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class AssignmentForm(WorkflowForm):
    member = forms.ModelChoiceField(
        label="PIC yang bertanggung jawab sekarang", queryset=TeamMember.objects.all()
    )
    reason = forms.CharField(
        label="Alasan penugasan / alih tugas",
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Waktu efektif dicatat saat disimpan. PIC awal untuk atribusi KPI tidak diganti.",
    )


class ContactForm(WorkflowForm):
    occurred_at = forms.DateTimeField(label="Waktu kontak benar-benar terjadi")
    method = forms.ChoiceField(label="Cara menghubungi", choices=CONTACT_METHODS)
    outcome = forms.ChoiceField(
        label="Hasil kontak",
        choices=[item for item in CONTACT_OUTCOMES if item[0] != "correction"],
    )
    summary = forms.CharField(
        label="Ringkasan pembicaraan dan langkah berikutnya",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    evidence = forms.CharField(
        label="Referensi bukti kontak",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Lokasi arsip atau ID percakapan yang dapat diperiksa. Jangan masukkan password atau informasi pribadi yang tidak diperlukan.",
    )
    loss_reason = forms.ChoiceField(
        label="Alasan tidak lanjut",
        choices=[("", "Tidak berlaku"), *LOSS_REASONS],
        required=False,
        help_text="Wajib bila Hasil kontak dipilih Tidak lanjut.",
    )
    first_response = forms.BooleanField(
        label="Ini respons pertama yang benar-benar diberikan kepada prospek",
        required=False,
    )
    followup_action = forms.ChoiceField(
        label="Janji tindak lanjut",
        choices=[
            ("keep", "Pertahankan jadwal saat ini"),
            ("set", "Tetapkan jadwal baru"),
            ("clear", "Tutup jadwal saat ini"),
        ],
        help_text="Pilih Tetapkan jadwal baru untuk mengisi tanggal dan jam; Pertahankan jadwal saat ini tidak mengubah janji yang sudah dicatat.",
    )
    next_due_at = forms.DateTimeField(
        label="Tanggal dan jam janji berikutnya",
        required=False,
        help_text="Wajib bila memilih Tetapkan jadwal baru. Kosongkan bila mempertahankan atau menutup jadwal.",
    )
    completes = forms.ModelChoiceField(
        label="Janji sebelumnya yang dipenuhi oleh kontak ini",
        queryset=LeadContact.objects.none(),
        required=False,
    )
    responsibility_reason = forms.CharField(
        label="Alasan bantuan langsung oleh direktur",
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Wajib bila direktur mencatat kontak tanpa menjadi PIC atau pengganti yang ditunjuk.",
    )

    def __init__(self, *args, lead, correction=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["completes"].queryset = LeadContact.objects.filter(
            organization=self.organization,
            lead=lead,
            next_due_at__isnull=False,
            completion_events__isnull=True,
        )
        self.fields["completes"].label_from_instance = lambda obj: (
            f"{obj.next_due_at:%d/%m/%Y %H:%M} · {obj.summary[:80]}"
        )
        if lead.first_response_at or correction:
            self.fields["first_response"].disabled = True
        if correction:
            for field in (
                "first_response",
                "followup_action",
                "next_due_at",
                "completes",
            ):
                self.fields.pop(field)
            self.fields["correction_reason"] = forms.CharField(
                label="Alasan koreksi",
                max_length=1000,
                widget=forms.Textarea(attrs={"rows": 3}),
            )

    def clean(self):
        data = super().clean()
        if data.get("outcome") == "lost" and not data.get("loss_reason"):
            self.add_error("loss_reason", "Pilih alasan prospek tidak lanjut.")
        if data.get("followup_action") == "set" and not data.get("next_due_at"):
            self.add_error("next_due_at", "Isi tanggal dan jam janji berikutnya.")
        if data.get("followup_action") in {"keep", "clear"} and data.get("next_due_at"):
            self.add_error(
                "next_due_at", "Kosongkan tanggal/jam atau pilih tetapkan jadwal baru."
            )
        return data


class ResponseCorrectionForm(WorkflowForm):
    first_response_at = forms.DateTimeField(
        label="Waktu respons pertama yang benar",
        required=False,
        help_text="Kosong hanya bila pencatatan lama memang tidak memiliki bukti respons; jelaskan alasannya.",
    )
    reason = forms.CharField(
        label="Alasan koreksi waktu respons",
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    evidence = forms.CharField(
        label="Referensi bukti pemeriksaan",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    responsibility_reason = forms.CharField(
        label="Alasan bantuan langsung oleh direktur", required=False, max_length=1000
    )


class CampaignTransitionForm(WorkflowForm):
    operation = forms.ChoiceField(
        label="Keputusan kampanye",
        choices=[
            ("pause", "Jeda kampanye"),
            ("resume", "Lanjutkan kampanye"),
            ("complete", "Selesaikan kampanye"),
            ("handover", "Alih pengelola"),
        ],
    )
    effective_on = forms.DateField(label="Tanggal efektif keputusan")
    member = forms.ModelChoiceField(
        label="Pengelola baru (khusus alih pengelola)",
        queryset=TeamMember.objects.all(),
        required=False,
    )
    reason = forms.CharField(
        label="Alasan dan hasil pemeriksaan",
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class ReasonForm(forms.Form):
    reason = forms.CharField(
        label="Alasan", max_length=1000, widget=forms.Textarea(attrs={"rows": 3})
    )


class WorkPolicyForm(forms.Form):
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    response_limit_minutes = forms.IntegerField(
        label="Batas respons pertama (menit kerja)", min_value=1, max_value=10080
    )
    review_limit_minutes = forms.IntegerField(
        label="Batas pemeriksaan bukti (menit kerja)", min_value=1, max_value=10080
    )
    workdays = forms.TypedMultipleChoiceField(
        label="Hari kerja",
        choices=list(
            enumerate(["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"])
        ),
        coerce=int,
        widget=forms.CheckboxSelectMultiple,
    )
    work_start = forms.TimeField(
        label="Mulai jam kerja WIB",
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
    )
    work_end = forms.TimeField(
        label="Akhir jam kerja WIB",
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
    )
    holidays = forms.CharField(
        label="Hari libur tambahan",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Satu tanggal YYYY-MM-DD per baris. Di luar hari kerja dan tanggal ini tidak dihitung sebagai menit kerja.",
    )
    reason = forms.CharField(
        label="Dasar pengesahan / perubahan aturan",
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def clean_holidays(self):
        return [
            line.strip()
            for line in self.cleaned_data["holidays"].splitlines()
            if line.strip()
        ]
