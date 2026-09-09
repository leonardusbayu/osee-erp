"""Indonesian forms; persistence always goes through authorized services."""
from decimal import Decimal
from django import forms
from django.utils import timezone
from webapp.forms import BusinessDateField
from .models import Objective, Decision, Budget, BudgetEntry, CashPlan, CashPlanLine, MarketingObservation

DATE = {"type": "date"}
CHANNELS = [("mitra", "Mitra"), ("meta_ads", "Meta Ads"), ("google_ads", "Google Ads"), ("whatsapp", "WhatsApp"), ("seo", "SEO"), ("sales", "Sales")]


class DirectorModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field, forms.DateField):
                field.widget = forms.DateInput(format="%Y-%m-%d", attrs=DATE)
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs["rows"] = 3
            if isinstance(field, forms.DecimalField):
                field.widget.attrs.update({"inputmode": "decimal", "step": "0.01"})


class ObjectiveForm(DirectorModelForm):
    class Meta:
        model = Objective
        fields = ["title", "metric_key", "target", "period_start", "period_end", "owner_name", "status", "progress_notes"]
        labels = {"title": "Sasaran perusahaan", "metric_key": "Ukuran keberhasilan", "target": "Nilai target (opsional)", "period_start": "Mulai periode", "period_end": "Akhir periode", "owner_name": "Penanggung jawab", "status": "Status sasaran", "progress_notes": "Progres, satuan target, dan catatan"}
        help_texts = {"target": "Target adalah rencana. Realisasi tidak dihitung otomatis dari angka ini.", "metric_key": "Contoh: kontribusi ITP, mitra aktif, atau pesanan dibayar."}


class DecisionForm(DirectorModelForm):
    class Meta:
        model = Decision
        fields = ["title", "problem", "evidence", "option", "risks", "owner_name", "due_date", "review_date", "amount"]
        labels = {"title": "Judul keputusan", "problem": "Masalah atau peluang", "evidence": "Dasar angka dan sumber", "option": "Tindakan yang diusulkan", "risks": "Risiko dan asumsi", "owner_name": "Penanggung jawab", "due_date": "Target pelaksanaan", "review_date": "Tanggal evaluasi", "amount": "Nilai rencana (Rp, opsional)"}
        help_texts = {"evidence": "Cantumkan periode dan sumber. Bedakan rekap, data buku, dan asumsi.", "amount": "Mencatat nilai rencana tidak memindahkan dana atau membuat otorisasi pembayaran."}


class BudgetForm(DirectorModelForm):
    class Meta:
        model = Budget
        fields = ["title", "year", "month", "channel", "amount"]
        labels = {"title": "Nama anggaran", "year": "Tahun", "month": "Bulan", "channel": "Kanal", "amount": "Batas anggaran (Rp)"}
        help_texts = {"amount": "Batas rencana belanja, bukan konfirmasi kas tersedia."}


class BudgetEntryForm(DirectorModelForm):
    class Meta:
        model = BudgetEntry
        fields = ["kind", "amount", "date", "description", "reference"]
        labels = {"kind": "Jenis pencatatan", "amount": "Nilai (Rp)", "date": "Tanggal", "description": "Keterangan", "reference": "Referensi dokumen"}
        help_texts = {"kind": "Pembayaran tidak mengurangi anggaran lagi. Gunakan Ganti catatan ketika komitmen berubah menjadi biaya."}


class CashPlanForm(DirectorModelForm):
    class Meta:
        model = CashPlan
        fields = ["title", "as_of", "opening_balance", "reserve", "unknown_obligations"]
        labels = {"title": "Nama rencana kas", "as_of": "Tanggal awal", "opening_balance": "Asumsi saldo awal (Rp)", "reserve": "Asumsi cadangan minimum (Rp)", "unknown_obligations": "Kewajiban belum lengkap / setelah 13 minggu"}
        help_texts = {"opening_balance": "Rencana ini menggunakan asumsi Anda, bukan saldo bank terverifikasi.", "unknown_obligations": "Catat biaya IIEF, pengajar, sewa, pajak, refund, atau layanan mendatang yang belum masuk jadwal."}


class CashLineForm(DirectorModelForm):
    class Meta:
        model = CashPlanLine
        fields = ["direction", "amount", "date", "category", "description", "source_reference", "evidence"]
        labels = {"direction": "Arus dana", "amount": "Nilai (Rp)", "date": "Perkiraan tanggal", "category": "Kategori", "description": "Keterangan", "source_reference": "Referensi pesanan / kewajiban", "evidence": "Dasar perkiraan"}
        help_texts = {"source_reference": "Gunakan referensi yang sama saat mengganti estimasi. Jangan masukkan uang yang sudah termasuk saldo awal sebagai penerimaan baru."}


class MarketingForm(DirectorModelForm):
    class Meta:
        model = MarketingObservation
        fields = ["date", "channel", "spend", "leads", "paid_orders", "reported_value", "source", "reference", "basis"]
        labels = {"date": "Tanggal laporan", "channel": "Kanal", "spend": "Biaya dilaporkan (Rp)", "leads": "Jumlah prospek dilaporkan", "paid_orders": "Pesanan dibayar menurut sumber", "reported_value": "Nilai konversi menurut sumber (Rp)", "source": "Sumber laporan", "reference": "ID laporan / kampanye / dokumen", "basis": "Status sumber"}
        help_texts = {"source": "Contoh: export Meta Ads, laporan Google Ads, atau catatan sales.", "spend": "Kosong berarti belum diketahui. Nol berarti sumber menyatakan tidak ada biaya.", "reported_value": "Klaim sumber dapat bertumpang tindih dengan kanal lain; tidak otomatis menjadi omzet OSEE."}


class ScenarioForm(forms.Form):
    title = forms.CharField(label="Nama simulasi", max_length=200)
    quantity = forms.IntegerField(label="Jumlah unit saat ini", min_value=1, max_value=1000000)
    current_price = forms.DecimalField(label="Harga jual saat ini per unit (Rp)", min_value=Decimal("0"), max_digits=16, decimal_places=2)
    current_cost = forms.DecimalField(label="Biaya pemasok saat ini per unit (Rp)", min_value=Decimal("0"), max_digits=16, decimal_places=2)
    variable_cost = forms.DecimalField(label="Biaya variabel lain per unit (Rp)", min_value=Decimal("0"), max_digits=16, decimal_places=2, initial=0)
    new_price = forms.DecimalField(label="Harga jual skenario per unit (Rp)", min_value=Decimal("0"), max_digits=16, decimal_places=2, required=False)
    new_cost = forms.DecimalField(label="Biaya pemasok skenario per unit (Rp)", min_value=Decimal("0"), max_digits=16, decimal_places=2, required=False)
    new_quantity = forms.IntegerField(label="Jumlah unit skenario", min_value=0, max_value=1000000, required=False)
    additional_marketing_spend = forms.DecimalField(label="Tambahan biaya marketing (Rp)", min_value=Decimal("0"), max_digits=16, decimal_places=2, initial=0)


class ActionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    notes = forms.CharField(label="Catatan pemeriksaan / alasan", max_length=4000, required=True, widget=forms.Textarea(attrs={"rows": 3}))
    self_approval_reason = forms.CharField(label="Alasan bila pengusul juga menyetujui (pemilik saja)", max_length=1000, required=False, widget=forms.Textarea(attrs={"rows": 2}))


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Alasan perubahan", max_length=500, widget=forms.Textarea(attrs={"rows": 3}))


class ChatForm(forms.Form):
    question = forms.CharField(label="Pertanyaan untuk AI Direktur", max_length=2000, widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Apa yang perlu saya perhatikan dari pertumbuhan OSEE tahun ini?"}))
    year = forms.IntegerField(min_value=2000, max_value=2200, widget=forms.HiddenInput)
    conversation_id = forms.IntegerField(required=False, min_value=1, widget=forms.HiddenInput)


class AIPolicyForm(forms.Form):
    enabled = forms.BooleanField(label="Izinkan analisis agregat melalui endpoint AI yang telah dikonfigurasi", required=False)
    monthly_budget_usd = forms.DecimalField(label="Batas bulanan perusahaan (USD)", max_digits=8, decimal_places=2, min_value=0)
    request_cap_usd = forms.DecimalField(label="Batas per permintaan (USD)", max_digits=8, decimal_places=4, min_value=0)


class MarketingImportForm(forms.Form):
    file = forms.FileField(label="File CSV marketing", help_text="UTF-8, maksimal 1 MB dan 500 baris. Unduh template terlebih dahulu.")

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if uploaded.size > 1024 * 1024 or not uploaded.name.lower().endswith(".csv"):
            raise forms.ValidationError("Gunakan file .csv berukuran maksimal 1 MB.")
        return uploaded
