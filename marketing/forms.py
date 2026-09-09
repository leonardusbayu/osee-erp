"""Indonesian input forms; only services persist their validated values."""
from django import forms

from finance.models import Invoice
from .models import AdDailyObservation, Campaign, Lead, MarketingAction, TeamMember, METRIC_CHOICES


class MarketingModelForm(forms.ModelForm):
    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.instance.organization = organization
        for name, field in self.fields.items():
            if isinstance(field, forms.ModelChoiceField):
                field.queryset = field.queryset.filter(organization=organization)
                if name == "owner":
                    field.queryset = field.queryset.filter(active=True)
                if name == "party":
                    field.queryset = field.queryset.exclude(kind="supplier")
            if isinstance(field, forms.DateTimeField):
                field.widget = forms.DateTimeInput(format="%Y-%m-%dT%H:%M:%S", attrs={"type": "datetime-local", "step": "1"})
                field.input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]
                field.help_text = "Waktu Indonesia Barat (WIB)."
            elif isinstance(field, forms.DateField):
                field.widget = forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs["rows"] = 3
            if isinstance(field, forms.DecimalField):
                field.widget.attrs.update({"step": "0.01", "inputmode": "decimal"})


class TeamMemberForm(MarketingModelForm):
    class Meta:
        model = TeamMember
        fields = ["name", "discipline", "team", "active"]
        labels = {"name": "Nama anggota tim", "discipline": "Peran kerja", "team": "Tim", "active": "Anggota aktif"}


class CampaignForm(MarketingModelForm):
    class Meta:
        model = Campaign
        fields = ["name", "channel", "product", "owner", "objective", "start_date", "end_date", "status"]
        labels = {"name": "Nama kampanye", "channel": "Sumber akuisisi", "product": "Produk", "owner": "Penanggung jawab kampanye", "objective": "Tujuan kampanye", "start_date": "Mulai", "end_date": "Selesai", "status": "Status"}
        help_texts = {"owner": "Pengelola kampanye. Petugas follow-up dipilih pada prospek.", "channel": "Pilih sumber awal prospek; WA sebagai tempat mengobrol tidak mengganti asal iklan."}


class AdObservationForm(MarketingModelForm):
    class Meta:
        model = AdDailyObservation
        fields = ["campaign", "date", "ad_name", "audience", "region", "spend", "impressions", "clicks", "source", "reference"]
        labels = {"campaign": "Kampanye", "date": "Tanggal laporan", "ad_name": "Nama / ID iklan", "audience": "Audiens", "region": "Wilayah", "spend": "Biaya iklan dilaporkan (Rp)", "impressions": "Tayangan", "clicks": "Klik", "source": "Sumber laporan", "reference": "Referensi unik laporan"}
        help_texts = {"spend": "Kosong berarti belum diketahui. Nol berarti sumber menyatakan tidak ada biaya.", "reference": "Satu referensi untuk satu baris sumber. Unggahan ulang harus mempunyai angka yang sama.", "ad_name": "Gunakan satu tingkat rincian per kampanye dan hari; jangan mencampur total dengan rincian yang termasuk di dalamnya."}


class LeadForm(MarketingModelForm):
    class Meta:
        model = Lead
        fields = ["reference", "name", "received_at", "campaign", "channel", "owner", "product", "party", "segment", "region", "stage", "first_response_at", "next_follow_up", "notes"]
        labels = {"reference": "ID prospek", "name": "Nama singkat prospek", "received_at": "Prospek diterima", "campaign": "Kampanye asal", "channel": "Sumber akuisisi", "owner": "Penanggung jawab follow-up", "product": "Produk yang diminati", "party": "Pembeli di Finance (bila sudah diketahui)", "segment": "Jenis pembeli", "region": "Wilayah", "stage": "Tahap saat dicatat", "first_response_at": "Respons pertama", "next_follow_up": "Tindak lanjut berikutnya", "notes": "Catatan kebutuhan"}
        help_texts = {"reference": "Gunakan ID unik dari formulir atau CRM; jangan masukkan nomor telepon sebagai ID.", "name": "Cukup nama singkat; hindari data pribadi yang tidak diperlukan.", "party": "Identitas pembeli dan produk diperlukan agar Finance dapat menghubungkan invoice dengan aman.", "stage": "Sepakat membeli belum berarti pembayaran sudah diterima."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["channel"].required = False

    def clean(self):
        cleaned = super().clean()
        campaign = cleaned.get("campaign")
        if campaign:
            if cleaned.get("channel") and cleaned["channel"] != campaign.channel:
                self.add_error("channel", "Sumber akuisisi harus sesuai kampanye asal.")
            cleaned["channel"] = campaign.channel
            if cleaned.get("product") is None:
                cleaned["product"] = campaign.product
        elif not cleaned.get("channel"):
            self.add_error("channel", "Pilih sumber akuisisi bila kampanye belum diketahui.")
        return cleaned


class LeadUpdateForm(MarketingModelForm):
    class Meta:
        model = Lead
        fields = ["stage", "first_response_at", "next_follow_up", "notes"]
        labels = {key: LeadForm.Meta.labels[key] for key in fields}
        help_texts = {"stage": "Perubahan tahap tercatat dalam riwayat. Pembayaran tetap berasal dari Finance."}


class InvoiceLinkForm(forms.Form):
    invoice = forms.ModelChoiceField(queryset=Invoice.objects.none(), label="Invoice yang sudah diterbitkan")
    confirm_identity = forms.BooleanField(required=False, label="Saya telah memeriksa bahwa invoice benar milik prospek ini", help_text="Bila pembeli atau produk belum diketahui, identitasnya dilengkapi dari invoice yang Anda periksa. Perubahan pemetaan disimpan dalam audit.")

    def __init__(self, *args, organization, lead=None, **kwargs):
        super().__init__(*args, **kwargs)
        rows = Invoice.objects.filter(organization=organization, status__in=("issued", "delivered"), marketing_leads__isnull=True)
        if lead is not None:
            if lead.product_id:
                rows = rows.filter(product_id=lead.product_id)
            if lead.party_id:
                rows = rows.filter(party_id=lead.party_id)
            rows = rows.filter(party__kind="reseller" if lead.segment == "reseller" else "customer")
        self.fields["invoice"].queryset = rows
        self.fields["invoice"].label_from_instance = lambda obj: f"{obj.number} · {obj.party.name} · {obj.product.name}"


class MarketingActionForm(MarketingModelForm):
    metric_key = forms.ChoiceField(label="Ukuran hasil", choices=METRIC_CHOICES,
                                  help_text="Pilih ukuran yang akan diperiksa setelah tindakan selesai.")
    class Meta:
        model = MarketingAction
        fields = ["title", "evidence", "description", "owner", "due_date", "metric_key"]
        labels = {"title": "Tindakan yang akan dikerjakan", "evidence": "Dasar / temuan dan periode", "description": "Langkah pelaksanaan", "owner": "Penanggung jawab", "due_date": "Tenggat", "metric_key": "Ukuran hasil"}
        help_texts = {"metric_key": "Contoh: cash_in, respons pertama, atau prospek yang sesuai kebutuhan. Hasil perlu dievaluasi setelah tindakan selesai."}


class ActionUpdateForm(MarketingModelForm):
    class Meta:
        model = MarketingAction
        fields = ["status", "outcome"]
        labels = {"status": "Status tindakan", "outcome": "Hasil evaluasi / alasan tidak dilanjutkan"}
        help_texts = {"outcome": "Catat hasil yang teramati dan periode. Perbaikan sesudah tindakan belum membuktikan sebab akibat."}


class MarketingAIPolicyForm(forms.Form):
    enabled = forms.BooleanField(required=False, label="Izinkan AI memprioritaskan temuan agregat marketing")
    monthly_budget_usd = forms.DecimalField(min_value=0, max_digits=12, decimal_places=6, label="Batas bulanan (USD)")
    request_cap_usd = forms.DecimalField(min_value=0, max_digits=12, decimal_places=6, label="Batas tiap permintaan (USD)")
