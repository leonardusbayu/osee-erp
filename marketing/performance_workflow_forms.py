from django import forms


class SourcePeriodForm(forms.Form):
    reference = forms.CharField(label="Referensi pemeriksaan", max_length=180)
    inventory_note = forms.CharField(
        label="Seluruh sumber dalam lingkup ini",
        max_length=4000,
        widget=forms.Textarea,
        help_text="Daftarkan seluruh formulir, percakapan, ekspor dan rekening relevan. Jelaskan pengecualian; jangan hanya memeriksa berkas yang kebetulan sudah masuk.",
    )
    confirm_all_sources = forms.BooleanField(
        label="Saya telah mencantumkan seluruh sumber relevan untuk lingkup dan periode target."
    )


class SourceCheckForm(forms.Form):
    kind = forms.ChoiceField(
        label="Jenis sumber",
        choices=[
            ("", "Pilih jenis sumber"),
            ("leads", "Prospek"),
            ("followups", "Janji follow-up"),
            ("bank", "Mutasi bank"),
        ],
    )
    name = forms.CharField(label="Nama sumber", max_length=180)
    reference = forms.CharField(label="Referensi dokumen/ekspor", max_length=180)
    expected_count = forms.IntegerField(label="Jumlah pada sumber", min_value=0)
    imported_count = forms.IntegerField(label="Masuk dalam lingkup ERP", min_value=0)
    duplicate_count = forms.IntegerField(
        label="Duplikat yang diperiksa", min_value=0, initial=0
    )
    excluded_count = forms.IntegerField(
        label="Dikecualikan dengan dasar", min_value=0, initial=0
    )
    unresolved_count = forms.IntegerField(
        label="Belum selesai diperiksa", min_value=0, initial=0
    )
    evidence = forms.CharField(
        label="Bukti dan dasar pengecualian", max_length=4000, widget=forms.Textarea
    )


SourceCheckFormSet = forms.formset_factory(
    SourceCheckForm,
    extra=4,
    max_num=50,
    validate_max=True,
    min_num=1,
    validate_min=True,
)


class SourceDecisionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_fingerprint = forms.CharField(
        min_length=64, max_length=64, widget=forms.HiddenInput
    )
    reason = forms.CharField(
        label="Catatan pemeriksaan atau penarikan",
        max_length=4000,
        widget=forms.Textarea,
    )


class EvidenceUploadForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    upload = forms.FileField(label="Bukti privat (PDF, PNG, JPEG; maksimal 10 MB)")
    replaces = forms.IntegerField(
        label="Nomor bukti yang diganti (opsional)", required=False, min_value=1
    )
    reason = forms.CharField(
        label="Alasan mengganti bukti",
        required=False,
        max_length=4000,
        widget=forms.Textarea,
    )
    responsibility_reason = forms.CharField(
        label="Alasan pengawasan direktur (jika bukan PIC/delegasi)",
        required=False,
        max_length=4000,
        widget=forms.Textarea,
    )


class WorkCorrectionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Dasar koreksi pekerjaan yang diterima",
        max_length=4000,
        widget=forms.Textarea,
    )
    due_date = forms.DateField(
        label="Tenggat pekerjaan pengganti",
        widget=forms.DateInput(attrs={"type": "date"}),
    )


class WorkHandoffForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    member = forms.ModelChoiceField(
        label="Pilih staf pengganti",
        queryset=None,
        help_text="Hanya staf aktif dengan akun perusahaan yang dapat menerima pekerjaan.",
    )
    reason = forms.CharField(
        label="Alasan memindahkan pekerjaan",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Contoh: staf lama cuti atau tanggung jawab berubah.",
    )

    def __init__(self, *args, organization, activity, **kwargs):
        from core.models import Membership

        from .models import TeamMember

        super().__init__(*args, **kwargs)
        accounts = Membership.objects.filter(
            organization=organization,
            user__is_active=True,
            role__in={"owner", "director", "finance", "marketing"},
        ).values("user_id")
        self.fields["member"].queryset = (
            TeamMember.objects.filter(
                organization=organization, active=True, account_id__in=accounts
            )
            .exclude(pk=activity.assigned_owner.pk)
            .order_by("name", "pk")
        )
