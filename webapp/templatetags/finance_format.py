from decimal import Decimal, InvalidOperation

from django import template
from django.utils.formats import date_format

register = template.Library()


@register.filter
def tax_rate_percent(value):
    """Display a stored fractional rate as a percentage without changing tax math."""
    if value is None or value == "":
        return "—"
    try:
        rate = Decimal(str(value))
        if not rate.is_finite() or not Decimal(0) <= rate <= Decimal(1):
            return "—"
        number = format(rate * 100, "f")
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        return number.replace(".", ",") + "%"
    except (InvalidOperation, TypeError, ValueError):
        return "—"


@register.filter(name="abs")
def absolute(value):
    try:
        return abs(Decimal(str(value or 0)))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


@register.filter
def money(value):
    try:
        amount = Decimal(str(value or 0))
        sign = "−" if amount < 0 else ""
        precision = 0 if amount == amount.to_integral_value() else 2
        number = (
            format(abs(amount), f",.{precision}f")
            .replace(",", "_")
            .replace(".", ",")
            .replace("_", ".")
        )
        return f"{sign}Rp{number}"
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
