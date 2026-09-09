from decimal import Decimal
from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
from core.models import Membership, Organization
from finance.models import Party, Product, PriceVersion, Bill, BankAccount

DATE = {"type": "date"}

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
    company_name = forms.CharField(label="Nama perusahaan", initial="PT Langkah Pintar Nusantara", max_length=200)
    email = forms.EmailField(label="Email Anda")

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ["company_name", "username", "email", "password1", "password2"]
        labels = {"username": "Nama pengguna"}
        help_texts = {"username": "Dipakai untuk masuk ke aplikasi. Contoh: andi.osee (tanpa spasi)."}
        widgets = {"username": forms.TextInput(attrs={"placeholder": "Contoh: andi.osee", "autocomplete": "username"})}

class InvoiceForm(forms.Form):
    party = forms.ModelChoiceField(label="Pelanggan / mitra", queryset=Party.objects.none())
    product = forms.ModelChoiceField(label="Produk", queryset=Product.objects.none())
    quantity = forms.IntegerField(label="Jumlah peserta / unit", min_value=1, max_value=100000, initial=1)
    unit_price = forms.DecimalField(label="Harga per unit (Rp)", max_digits=16, decimal_places=2, min_value=Decimal("0.01"), required=False, help_text="Kosongkan untuk memakai harga aktif mitra atau harga produk. Harga dikunci saat invoice diterbitkan.")
    override_reason = forms.CharField(label="Alasan harga khusus (jika berbeda)", required=False, max_length=300, help_text="Wajib bila harga yang dimasukkan berbeda dari harga aktif.")
    date = BusinessDateField(label="Tanggal invoice", widget=forms.DateInput(attrs=DATE), initial=timezone.localdate)
    due_date = BusinessDateField(label="Batas pembayaran", widget=forms.DateInput(attrs=DATE), initial=timezone.localdate)
    service_date = BusinessDateField(label="Jadwal tes / layanan", widget=forms.DateInput(attrs=DATE))

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["party"].queryset = Party.objects.filter(organization=organization, kind__in=["customer", "reseller"]).order_by("name")
        self.fields["product"].queryset = Product.objects.filter(organization=organization).order_by("name")

    def clean(self):
        data = super().clean()
        if data.get("date") and data.get("due_date") and data["due_date"] < data["date"]:
            self.add_error("due_date", "Batas pembayaran tidak boleh sebelum tanggal invoice.")
        if data.get("date") and data.get("service_date") and data["service_date"] < data["date"]:
            self.add_error("service_date", "Tanggal layanan tidak boleh sebelum tanggal invoice untuk pesanan baru.")
        return data

class PartyForm(forms.ModelForm):
    class Meta:
        model = Party
        fields = ["name", "kind", "tax_id"]
        labels = {"name": "Nama mitra / pelanggan / supplier", "kind": "Hubungan dengan OSEE", "tax_id": "NPWP (opsional)"}

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ["code", "name", "kind", "default_price"]
        labels = {"code": "Kode produk", "name": "Nama produk", "kind": "Jenis layanan", "default_price": "Harga standar (Rp)"}

class PriceForm(BusinessModelForm):
    class Meta:
        model = PriceVersion
        fields = ["product", "party", "kind", "amount", "effective_from", "effective_to"]
        labels = {"product": "Produk", "party": "Mitra / supplier (opsional)", "kind": "Jenis harga", "amount": "Harga per unit (Rp)", "effective_from": "Berlaku mulai", "effective_to": "Berlaku sampai (opsional)"}
        widgets = {"effective_from": forms.DateInput(attrs=DATE), "effective_to": forms.DateInput(attrs=DATE)}

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.organization = organization
        self.fields["party"].queryset = Party.objects.filter(organization=organization).order_by("name")
        self.fields["product"].queryset = Product.objects.filter(organization=organization).order_by("name")
        self.fields["effective_from"].initial = timezone.localdate()

class BillForm(BusinessModelForm):
    class Meta:
        model = Bill
        fields = ["supplier", "amount", "supplier_vat", "date", "service_date", "category"]
        labels = {"supplier": "Supplier", "amount": "Total tagihan termasuk pajak (Rp)", "supplier_vat": "PPN yang tertulis di tagihan (Rp)", "date": "Tanggal tagihan", "service_date": "Tanggal layanan", "category": "Jenis biaya"}
        widgets = {"date": forms.DateInput(attrs=DATE), "service_date": forms.DateInput(attrs=DATE)}
        help_texts = {"supplier_vat": "Bagian dari total tagihan. Dicatat sebagai informasi, bukan otomatis kredit pajak.", "category": "Perlakuan pajak diperiksa terpisah sebelum pembayaran dan tutup bulan."}

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.organization = organization
        self.fields["supplier"].queryset = Party.objects.filter(organization=organization, kind="supplier").order_by("name")
        self.fields["date"].initial = timezone.localdate()
        self.fields["service_date"].initial = timezone.localdate()

class BankImportForm(forms.Form):
    account = forms.ModelChoiceField(label="Rekening bank", queryset=BankAccount.objects.none())
    file = forms.FileField(label="File mutasi CSV", help_text="Gunakan template CSV OSEE: date,reference,description,amount. Nilai masuk positif; nilai keluar negatif. Maksimum 2 MB.")

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = BankAccount.objects.filter(organization=organization)

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
    registration_date = BusinessDateField(label="Tanggal pertama terdaftar NPWP", required=False, widget=forms.DateInput(attrs=DATE))
    registration_evidence = forms.CharField(label="Referensi dokumen pendaftaran pajak", max_length=300, required=False, help_text="Nama atau nomor dokumen pendukung. Tidak mengaktifkan tarif pajak secara otomatis.")
    vat_status_effective_from = BusinessDateField(label="Non-PKP berlaku sejak (jika diketahui)", required=False, widget=forms.DateInput(attrs=DATE))
    vat_status_evidence = forms.CharField(label="Referensi status non-PKP", max_length=300, required=False)

class PriceReplacementForm(forms.Form):
    amount = forms.DecimalField(label="Harga baru per unit (Rp)", max_digits=16, decimal_places=2, min_value=Decimal("0.01"))
    effective_from = BusinessDateField(label="Mulai berlaku", widget=forms.DateInput(attrs=DATE), initial=timezone.localdate)
    reason = forms.CharField(label="Alasan perubahan", max_length=500, widget=forms.Textarea(attrs={"rows": 3}), help_text="Harga pada invoice yang sudah diterbitkan tetap dipertahankan.")

class TeamMemberForm(UserCreationForm):
    first_name = forms.CharField(label="Nama lengkap", max_length=150)
    email = forms.EmailField(label="Email", required=False)
    role = forms.ChoiceField(
        label="Peran dalam tim",
        choices=Membership.TEAM_ROLES,
        initial="finance",
        help_text="Owner/Direktur mengelola akun dan menyetujui anggaran. Marketing mengelola pemasaran. Finance mengerjakan transaksi, laporan, dan persiapan pajak. Pilih Owner/Direktur hanya untuk pimpinan perusahaan.",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ["first_name", "username", "email", "role", "password1", "password2"]
        labels = {"username": "Nama pengguna"}
        help_texts = {"username": "Dipakai untuk masuk ke aplikasi. Contoh: andi.osee (tanpa spasi)."}
        widgets = {"username": forms.TextInput(attrs={"placeholder": "Contoh: andi.osee", "autocomplete": "username"})}
