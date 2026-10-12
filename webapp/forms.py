from decimal import Decimal
from uuid import uuid4

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.models import Membership, Organization
from finance.costing_models import PriceOverrideRequest
from finance.models import BankAccount, Bill, Party, PriceVersion, Product

from .money_fields import HumanRupiahField

DATE = {"type": "date"}


class BankMatchAmountForm(forms.Form):
    amount = HumanRupiahField(
        label="Jumlah pembayaran (Rp)",
        max_digits=20,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )

    def clean_amount(self):
        if hasattr(self.data, "getlist") and len(self.data.getlist("amount")) != 1:
            raise ValidationError("Masukkan satu jumlah alokasi saja.")
        return self.cleaned_data["amount"]


def validate_business_date(value):
    if not 2000 <= value.year <= 2200:
        raise ValidationError("Tanggal harus berada antara tahun 2000 dan 2200.")


class BusinessDateField(forms.DateField):
    default_validators = [validate_business_date]


class BusinessModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field, forms.DateField):
                field.validators.append(validate_business_date)


class SetupForm(UserCreationForm):
    company_name = forms.CharField(
        label="Nama perusahaan", initial="PT Langkah Pintar Nusantara", max_length=200
    )
    email = forms.EmailField(label="Email Anda")

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ["company_name", "username", "email", "password1", "password2"]
        labels = {"username": "Nama pengguna"}
        help_texts = {
            "username": "Dipakai untuk masuk ke aplikasi. Contoh: andi.osee (tanpa spasi)."
        }
        widgets = {
            "username": forms.TextInput(
                attrs={"placeholder": "Contoh: andi.osee", "autocomplete": "username"}
            )
        }


class InvoiceForm(forms.Form):
    party = forms.ModelChoiceField(
        label="Pelanggan / mitra", queryset=Party.objects.none()
    )
    product = forms.ModelChoiceField(label="Produk", queryset=Product.objects.none())
    quantity = forms.IntegerField(
        label="Jumlah peserta / unit", min_value=1, max_value=100000, initial=1
    )
    unit_price = HumanRupiahField(
        label="Harga per unit (Rp)",
        max_digits=16,
        decimal_places=2,
        min_value=Decimal("0.01"),
        required=False,
        widget=forms.TextInput(
            attrs={"inputmode": "decimal", "placeholder": "Contoh: 500.000"}
        ),
        help_text="Kosongkan untuk memakai harga aktif mitra atau harga produk. Contoh: 500.000 atau 500.000,25. Harga dikunci saat invoice diterbitkan.",
        error_messages={
            "invalid": "Masukkan harga per unit, misalnya 500.000.",
            "min_value": "Harga per unit harus lebih dari nol.",
            "max_digits": "Harga per unit terlalu besar.",
            "max_whole_digits": "Harga per unit terlalu besar.",
        },
    )
    override_reason = forms.CharField(
        label="Catatan tambahan harga (opsional)", required=False, max_length=300
    )
    price_override = forms.ModelChoiceField(
        label="Persetujuan Direktur untuk harga khusus",
        required=False,
        queryset=PriceOverrideRequest.objects.none(),
        help_text="Ajukan melalui Penjualan → Harga khusus. Pilih persetujuan untuk pelanggan, produk, jumlah dan tanggal yang sama. Berlaku sekali.",
    )
    date = BusinessDateField(
        label="Tanggal invoice",
        widget=forms.DateInput(attrs=DATE),
        initial=timezone.localdate,
    )
    due_date = BusinessDateField(
        label="Batas pembayaran",
        widget=forms.DateInput(attrs=DATE),
        initial=timezone.localdate,
    )
    service_date = BusinessDateField(
        label="Jadwal tes / layanan", widget=forms.DateInput(attrs=DATE)
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["party"].queryset = Party.objects.filter(
            organization=organization, kind__in=["customer", "reseller"]
        ).order_by("name")
        self.fields["product"].queryset = Product.objects.filter(
            organization=organization
        ).order_by("name")
        self.fields["price_override"].queryset = PriceOverrideRequest.objects.filter(
            organization=organization,
            decision_record__decision="approved",
            use__isnull=True,
        ).select_related("party", "product")

    def clean(self):
        data = super().clean()
        if data.get("price_override") and data.get("unit_price") is None:
            data["unit_price"] = data["price_override"].unit_price
        if (
            data.get("date")
            and data.get("due_date")
            and data["due_date"] < data["date"]
        ):
            self.add_error(
                "due_date", "Batas pembayaran tidak boleh sebelum tanggal invoice."
            )
        if (
            data.get("date")
            and data.get("service_date")
            and data["service_date"] < data["date"]
        ):
            self.add_error(
                "service_date",
                "Tanggal layanan tidak boleh sebelum tanggal invoice untuk pesanan baru.",
            )
        return data


class DeliveryForm(forms.Form):
    date = BusinessDateField(
        label="Tanggal seluruh layanan benar-benar selesai",
        widget=forms.DateInput(attrs=DATE),
        help_text="Gunakan tanggal tes atau penyelesaian seluruh kewajiban layanan, termasuk jika baru dicatat hari ini.",
    )
    evidence = forms.CharField(
        label="Referensi bukti penyelesaian",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Contoh: nomor berita acara atau daftar pelaksanaan tes. Lampirkan dokumen pendukung melalui rincian invoice.",
    )
    confirmed = forms.BooleanField(
        label="Saya memastikan seluruh layanan pada invoice ini sudah selesai",
        help_text="Satu pertemuan dari paket kursus yang masih berlangsung belum memenuhi konfirmasi ini.",
    )

    def clean_date(self):
        value = self.cleaned_data["date"]
        if value > timezone.localdate():
            raise ValidationError(
                "Tanggal penyelesaian aktual tidak boleh di masa depan."
            )
        return value


class PartyForm(forms.ModelForm):
    class Meta:
        model = Party
        fields = ["name", "kind", "tax_id"]
        labels = {
            "name": "Nama mitra / pelanggan / supplier",
            "kind": "Hubungan dengan OSEE",
            "tax_id": "NPWP (opsional)",
        }


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ["code", "name", "kind", "default_price"]
        labels = {
            "code": "Kode produk",
            "name": "Nama produk",
            "kind": "Jenis layanan",
            "default_price": "Harga standar (Rp)",
        }


class PriceForm(BusinessModelForm):
    class Meta:
        model = PriceVersion
        fields = [
            "product",
            "party",
            "kind",
            "amount",
            "effective_from",
            "effective_to",
        ]
        labels = {
            "product": "Produk",
            "party": "Mitra / supplier (opsional)",
            "kind": "Jenis harga",
            "amount": "Harga per unit (Rp)",
            "effective_from": "Berlaku mulai",
            "effective_to": "Berlaku sampai (opsional)",
        }
        widgets = {
            "effective_from": forms.DateInput(attrs=DATE),
            "effective_to": forms.DateInput(attrs=DATE),
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.organization = organization
        self.fields["party"].queryset = Party.objects.filter(
            organization=organization
        ).order_by("name")
        self.fields["product"].queryset = Product.objects.filter(
            organization=organization
        ).order_by("name")
        self.fields["effective_from"].initial = timezone.localdate()


class BillForm(forms.Form):
    supplier = forms.ModelChoiceField(label="Supplier", queryset=Party.objects.none())
    supplier_reference = forms.CharField(
        label="Nomor dokumen asli pemasok",
        max_length=160,
        help_text="Salin nomor invoice pemasok. Nomor internal BILL dibuat otomatis. Dokumen yang sama tidak dapat dicatat dua kali untuk pemasok yang sama.",
    )
    submission_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid4)
    amount = HumanRupiahField(
        label="Total tagihan termasuk pajak (Rp)",
        max_digits=20,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.TextInput(
            attrs={"inputmode": "decimal", "placeholder": "Contoh: 50.000"}
        ),
        help_text="Contoh: 50.000 atau 50.000,25.",
        error_messages={
            "invalid": "Masukkan total tagihan, misalnya 50.000.",
            "required": "Isi total tagihan.",
            "min_value": "Total tagihan harus lebih dari nol.",
            "max_digits": "Nominal terlalu besar.",
            "max_whole_digits": "Nominal terlalu besar.",
        },
    )
    supplier_vat = HumanRupiahField(
        label="PPN yang tertulis di tagihan (Rp)",
        max_digits=20,
        decimal_places=2,
        min_value=Decimal(0),
        initial=0,
        widget=forms.TextInput(
            attrs={"inputmode": "decimal", "placeholder": "Contoh: 5.000 atau 0"}
        ),
        help_text="Bagian dari total tagihan. Dicatat sebagai informasi, bukan otomatis kredit pajak. Contoh: 5.000 atau 0.",
        error_messages={
            "invalid": "Masukkan nominal PPN, misalnya 5.000 atau 0.",
            "required": "Isi nominal PPN sesuai dokumen.",
            "min_value": "PPN tidak boleh bernilai negatif.",
            "max_digits": "Nominal terlalu besar.",
            "max_whole_digits": "Nominal terlalu besar.",
        },
    )
    date = BusinessDateField(
        label="Tanggal tagihan",
        widget=forms.DateInput(attrs=DATE),
        initial=timezone.localdate,
    )
    service_date = BusinessDateField(
        label="Tanggal layanan",
        widget=forms.DateInput(attrs=DATE),
        initial=timezone.localdate,
    )
    category = forms.ChoiceField(
        label="Jenis biaya",
        choices=Bill._meta.get_field("category").choices,
        help_text="Perlakuan pajak diperiksa terpisah sebelum pembayaran dan tutup bulan.",
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Party.objects.filter(
            organization=organization, kind="supplier"
        ).order_by("name")


class BankImportForm(forms.Form):
    account = forms.ModelChoiceField(
        label="Rekening bank", queryset=BankAccount.objects.none()
    )
    file = forms.FileField(
        label="File mutasi CSV",
        help_text="Gunakan template CSV OSEE: date,reference,description,amount. Nilai masuk positif; nilai keluar negatif. Maksimum 2 MB.",
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = BankAccount.objects.filter(
            organization=organization
        )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 2 * 1024 * 1024 or not upload.name.lower().endswith(".csv"):
            raise ValidationError("Pilih file .csv dengan ukuran maksimal 2 MB.")
        return upload


class BankAccountForm(forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["name", "account_number"]
        labels = {"name": "Nama rekening", "account_number": "Nomor rekening"}


class OrganizationForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ["name", "brand_name"]
        labels = {"name": "Nama badan usaha", "brand_name": "Nama usaha"}


class RegistrationEvidenceForm(forms.Form):
    registration_date = BusinessDateField(
        label="Tanggal pertama terdaftar NPWP",
        required=False,
        widget=forms.DateInput(attrs=DATE),
    )
    registration_evidence = forms.CharField(
        label="Referensi dokumen pendaftaran pajak",
        max_length=300,
        required=False,
        help_text="Nama atau nomor dokumen pendukung. Tidak mengaktifkan tarif pajak secara otomatis.",
    )
    vat_status_effective_from = BusinessDateField(
        label="Non-PKP berlaku sejak (jika diketahui)",
        required=False,
        widget=forms.DateInput(attrs=DATE),
    )
    vat_status_evidence = forms.CharField(
        label="Referensi status non-PKP", max_length=300, required=False
    )


class PriceReplacementForm(forms.Form):
    amount = forms.DecimalField(
        label="Harga baru per unit (Rp)",
        max_digits=16,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    effective_from = BusinessDateField(
        label="Mulai berlaku",
        widget=forms.DateInput(attrs=DATE),
        initial=timezone.localdate,
    )
    reason = forms.CharField(
        label="Alasan perubahan",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Harga pada invoice yang sudah diterbitkan tetap dipertahankan.",
    )


class TeamMemberForm(UserCreationForm):
    first_name = forms.CharField(label="Nama lengkap", max_length=150)
    email = forms.EmailField(label="Email", required=False)
    role = forms.ChoiceField(
        label="Peran dalam tim",
        choices=Membership.TEAM_ROLES,
        initial="finance",
        help_text="Owner/Direktur mengelola perusahaan. Marketing mengelola prospek dan kampanye. Finance menangani transaksi, laporan, pajak, dan gaji. Operations menangani pendaftaran, peserta, jadwal, serta kehadiran—tanpa akses invoice atau keuangan.",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ["first_name", "username", "email", "role", "password1", "password2"]
        labels = {"username": "Nama pengguna"}
        help_texts = {
            "username": "Dipakai untuk masuk ke aplikasi. Contoh: andi.osee (tanpa spasi)."
        }
        widgets = {
            "username": forms.TextInput(
                attrs={"placeholder": "Contoh: andi.osee", "autocomplete": "username"}
            )
        }


class LateCustomerTaxCreditForm(forms.Form):
    """One scoped recorded-payment choice, preserving the existing HTML name."""

    source_allocation = forms.ChoiceField(label="Pembayaran yang sudah tercatat")
    date = BusinessDateField(
        label="Tanggal pencatatan kredit pada periode terbuka",
        widget=forms.DateInput(format="%Y-%m-%d", attrs=DATE),
        help_text="Isi tanggal jurnal yang diperiksa. Tanggal pembayaran lama tetap dipertahankan.",
    )
    reference = forms.CharField(
        max_length=120, initial=lambda: f"UI-{uuid4().hex}", widget=forms.HiddenInput()
    )
    evidence = forms.CharField(
        label="Bukti dan hasil pemeriksaan",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Catat referensi pembayaran, bukti potong, dan alasan pencatatan pada tanggal ini.",
    )
    confirmed = forms.BooleanField(
        label="Saya telah memeriksa pembayaran, invoice, bukti potong, dan tanggal jurnal. Uang bank baru Rp0."
    )

    def __init__(self, data=None, *, organization, obligation, **kwargs):
        from finance.models import AdvanceApplication, Allocation, Invoice

        from .templatetags.finance_format import iddate, money

        super().__init__(data, **kwargs)
        self.payment_sources = {}
        self.source_cards = []
        invoice = Invoice.objects.filter(
            organization=organization, pk=obligation.source_invoice_id
        ).first()
        if invoice:
            allocations = Allocation.objects.filter(
                organization=organization,
                invoice=invoice,
                transaction__organization=organization,
                transaction__account__organization=organization,
                amount__gt=0,
            ).select_related("transaction__account")
            applications = AdvanceApplication.objects.filter(
                organization=organization,
                invoice=invoice,
                advance__organization=organization,
                advance__party_id=invoice.party_id,
                advance__bank_posting__organization=organization,
                advance__bank_posting__transaction__organization=organization,
                advance__bank_posting__transaction__account__organization=organization,
                amount__gt=0,
            ).select_related("advance__bank_posting__transaction__account")
            for item in allocations.order_by("pk"):
                bank = item.transaction
                value = str(item.pk)
                label = f"Pembayaran bank · {invoice.number} · {iddate(bank.date)} · {money(item.amount)}"
                self.payment_sources[value] = ("allocation", item)
                self.source_cards.append(
                    {"value": value, "label": label, "cash": item.amount, "bank": bank}
                )
            for item in applications.order_by("date", "pk"):
                bank = item.advance.bank_posting.transaction
                value = f"advance:{item.pk}"
                label = f"Uang muka digunakan · {invoice.number} · {iddate(item.date)} · {money(item.amount)}"
                self.payment_sources[value] = ("advance", item)
                self.source_cards.append(
                    {
                        "value": value,
                        "label": label,
                        "cash": item.advance.amount,
                        "bank": bank,
                        "application": item,
                    }
                )
        self.fields["source_allocation"].choices = [
            ("", "Pilih pembayaran yang sudah tercatat")
        ] + [(item["value"], item["label"]) for item in self.source_cards]
        self.fields[
            "source_allocation"
        ].help_text = "Pilih satu pembayaran bank atau uang muka yang sudah digunakan untuk invoice ini. Jumlah kredit mengikuti bukti potong yang telah dicocokkan."

    def clean(self):
        values = super().clean()
        # Neither a second transport field nor repeated selectors may hide a
        # conflicting source behind QueryDict's last-value behavior.
        if self.data.get("source_advance_application") or (
            hasattr(self.data, "getlist")
            and len(self.data.getlist("source_allocation")) != 1
        ):
            raise ValidationError("Pilih tepat satu pembayaran yang sudah tercatat.")
        selected = values.get("source_allocation")
        if selected in self.payment_sources:
            kind, item = self.payment_sources[selected]
            values["source_allocation"] = item if kind == "allocation" else None
            values["source_advance_application"] = item if kind == "advance" else None
        return values
