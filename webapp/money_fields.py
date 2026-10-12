"""Human Rupiah input for explicitly Rp-labeled forms."""

import re

from django import forms
from django.core.exceptions import ValidationError


class HumanRupiahField(forms.DecimalField):
    widget = forms.TextInput
    default_error_messages = {
        "invalid": "Masukkan jumlah alokasi yang valid.",
        "required": "Masukkan jumlah pembayaran.",
        "min_value": "Jumlah pembayaran harus paling sedikit Rp0,01.",
        "max_digits": "Jumlah pembayaran terlalu besar.",
        "max_whole_digits": "Jumlah pembayaran terlalu besar.",
        "max_decimal_places": "Gunakan paling banyak dua angka pecahan.",
    }

    def to_python(self, value):
        if value in self.empty_values:
            return None
        if not isinstance(value, str):
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        entered = value.strip()
        if re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", entered):
            canonical = entered
        elif re.fullmatch(r"[0-9]+,[0-9]{1,2}", entered):
            canonical = entered.replace(",", ".")
        elif re.fullmatch(r"[1-9][0-9]{0,2}(?:\.[0-9]{3})+(?:,[0-9]{1,2})?", entered):
            canonical = entered.replace(".", "").replace(",", ".")
        else:
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        return super().to_python(canonical)
