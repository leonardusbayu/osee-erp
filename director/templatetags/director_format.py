from decimal import Decimal, InvalidOperation
from django import template
from webapp.templatetags.finance_format import money

register = template.Library()


@register.filter
def director_money(value):
    if value is None or value == "":
        return "—"
    try:
        number = Decimal(str(value))
        return money(number) if number.is_finite() else "—"
    except (InvalidOperation, ValueError, TypeError):
        return "—"


@register.filter
def director_number(value):
    if value is None or value == "":
        return "—"
    try:
        number = Decimal(str(value))
        return f"{number:,.0f}".replace(",", ".") if number.is_finite() else "—"
    except (InvalidOperation, ValueError, TypeError):
        return "—"


@register.filter
def director_percent(value):
    if value is None or value == "":
        return "—"
    try:
        number = Decimal(str(value))
        return f"{number:.1f}%".replace(".", ",") if number.is_finite() else "—"
    except (InvalidOperation, ValueError, TypeError):
        return "—"
