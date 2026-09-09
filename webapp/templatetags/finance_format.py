from decimal import Decimal, InvalidOperation
from django import template
from django.utils.formats import date_format

register = template.Library()

@register.filter(name="abs")
def absolute(value):
    try:
        return abs(Decimal(str(value or 0)))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")

@register.filter
def money(value):
    try:
        amount = Decimal(str(value or 0))
        sign = "−" if amount < 0 else ""
        return f"{sign}Rp{abs(amount):,.0f}".replace(",", ".")
    except (InvalidOperation, TypeError, ValueError):
        return "—"

@register.filter
def iddate(value):
    return date_format(value, "d M Y") if value else "—"

@register.filter
def compact_money(value):
    try:
        amount = Decimal(str(value or 0))
        if abs(amount) >= 1000000:
            return f"Rp{amount / 1000000:.1f} jt".replace(".", ",")
        return money(amount)
    except (InvalidOperation, ValueError):
        return "—"
