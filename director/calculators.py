"""Deterministic planning calculations. Nothing here posts, pays, or predicts demand."""

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP, localcontext
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError


CENT = Decimal("0.01")
ZERO = Decimal("0.00")
JAKARTA = ZoneInfo("Asia/Jakarta")
FORMULA_VERSION = "director-planning-v1"


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _amount(value, name, *, negative=False):
    if isinstance(value, (bool, float)):
        raise ValidationError(f"{name}: gunakan angka desimal, bukan floating point.")
    try:
        result = Decimal(value)
        if not result.is_finite() or abs(result) >= Decimal("1000000000000000000"):
            raise InvalidOperation
        if result != result.quantize(CENT) or (result < 0 and not negative):
            raise InvalidOperation
        return result.quantize(CENT)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(f"{name}: jumlah tidak valid; maksimal dua angka desimal.") from exc


def _quantity(value, name="Jumlah"):
    try:
        if isinstance(value, (float, bool)):
            raise ValueError
        number = Decimal(value)
        if not number.is_finite() or number != number.to_integral_value() or not 0 <= number <= 1000000000:
            raise ValueError
        return int(number)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(f"{name}: gunakan bilangan bulat antara 0 dan 1 miliar.") from exc


def _money(value):
    # Input limits permit large scenarios; local precision avoids process-global changes.
    with localcontext() as context:
        context.prec = 48
        return format(value.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _percentage(numerator, denominator):
    if denominator == 0:
        return None
    with localcontext() as context:
        context.prec = 48
        return _money(numerator / denominator * 100)


def _day(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("Tanggal dengan waktu harus menyertakan zona waktu.")
        return value.astimezone(JAKARTA).date()
    if isinstance(value, date):
        return value
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except ValueError as exc:
        raise ValidationError("Tanggal harus menggunakan format YYYY-MM-DD.") from exc


def _economics(quantity, price, cost, variable, marketing):
    with localcontext() as context:
        context.prec = 48
        revenue = quantity * price
        unit = price - cost - variable
        contribution = quantity * unit
        return {
            "quantity": quantity, "unit_price": _money(price), "unit_cost": _money(cost),
            "variable_cost": _money(variable), "unit_contribution": _money(unit),
            "revenue": _money(revenue), "supplier_cost": _money(quantity * cost),
            "variable_cost_total": _money(quantity * variable), "contribution": _money(contribution),
            "additional_marketing_spend": _money(marketing),
            "net_contribution": _money(contribution - marketing),
            "margin_pct": _percentage(contribution - marketing, revenue),
        }


def price_cost_scenario(*, quantity, current_price, current_cost, variable_cost=0,
                        new_price=None, new_cost=None, new_variable_cost=None,
                        new_quantity=None, additional_marketing_spend=0):
    """Compare assumed unit economics; inputs must use one consistent tax/cost basis.

    Contribution excludes overhead, income tax, and unspecified costs. Marketing
    is the *additional* proposed spend, not a second copy of unit variable costs.
    Quantity changes and sensitivity ranges are assumptions, never elasticity.
    """
    quantity = _quantity(quantity)
    next_quantity = quantity if new_quantity is None else _quantity(new_quantity, "Jumlah usulan")
    price = _amount(current_price, "Harga saat ini")
    cost = _amount(current_cost, "Biaya pemasok saat ini")
    variable = _amount(variable_cost, "Biaya variabel")
    next_price = price if new_price is None else _amount(new_price, "Harga usulan")
    next_cost = cost if new_cost is None else _amount(new_cost, "Biaya pemasok usulan")
    next_variable = variable if new_variable_cost is None else _amount(new_variable_cost, "Biaya variabel usulan")
    marketing = _amount(additional_marketing_spend, "Tambahan marketing")
    baseline = _economics(quantity, price, cost, variable, ZERO)
    proposed = _economics(next_quantity, next_price, next_cost, next_variable, marketing)
    with localcontext() as context:
        context.prec = 48
        delta = {key: _money(Decimal(proposed[key]) - Decimal(baseline[key]))
                 for key in ("unit_contribution", "revenue", "contribution", "net_contribution")}
    delta["quantity"] = next_quantity - quantity
    unit_contribution = Decimal(proposed["unit_contribution"])
    with localcontext() as context:
        context.prec = 48
        target = Decimal(baseline["net_contribution"]) + marketing
        break_even = (max(0, int((target / unit_contribution).to_integral_value(rounding=ROUND_CEILING)))
                      if unit_contribution > 0 else None)
    sensitivity = []
    for factor, label in ((Decimal("0.8"), "Volume usulan -20%"), (Decimal("1"), "Volume usulan"),
                          (Decimal("1.2"), "Volume usulan +20%")):
        units = int((next_quantity * factor).to_integral_value(rounding=ROUND_HALF_UP))
        sensitivity.append({"label": label, "quantity": units,
                            "net_contribution": _economics(units, next_price, next_cost, next_variable, marketing)["net_contribution"]})
    warnings = [
        "SIMULASI: volume dan harga adalah asumsi; perubahan permintaan tidak diprediksi.",
        "Kontribusi ini belum mencakup biaya tetap, pajak penghasilan, atau biaya yang belum dimasukkan.",
        "Gunakan dasar harga dan biaya yang konsisten. Masukkan PPN pemasok yang tidak dapat dikreditkan sebagai biaya bila berlaku.",
        "Tambahan marketing dihitung sekali di usulan; jangan masukkan biaya yang sama lagi ke biaya variabel.",
    ]
    if unit_contribution <= 0:
        warnings.append("Kontribusi per unit usulan tidak positif; volume tambahan tidak memperbaiki kontribusi per unit.")
    return {
        "baseline": baseline, "proposed": proposed, "delta": delta, "sensitivity": sensitivity,
        "break_even_quantity_to_match_baseline": break_even,
        "inputs": {"quantity": quantity, "current_price": _money(price), "current_cost": _money(cost),
                   "variable_cost": _money(variable), "new_quantity": next_quantity, "new_price": _money(next_price),
                   "new_cost": _money(next_cost), "new_variable_cost": _money(next_variable),
                   "additional_marketing_spend": _money(marketing)},
        "basis": "assumed", "warnings": warnings, "formula_version": FORMULA_VERSION,
    }


def cash_forecast(opening_balance, as_of, reserve, lines):
    """91-day assumed plan with 13 weekly rows and the first 14 daily rows.

    Opening balance is beginning-of-day. Only planned lines in the horizon enter
    the calculation. Past receipts are excluded, so they cannot be counted again
    on top of an opening balance. Repeated stable line IDs are rejected; a shared
    source reference can legitimately identify multiple installments.
    """
    opening = _amount(opening_balance, "Saldo awal", negative=True)
    reserve_amount = _amount(reserve, "Cadangan")
    start = _day(as_of)
    if not 2000 <= start.year <= 2200:
        raise ValidationError("Tahun rencana harus antara 2000 dan 2200.")
    end = start + timedelta(days=90)
    buckets = {}
    excluded = {"void": 0, "before_start": 0, "after_horizon": 0}
    seen = set()
    for index, line in enumerate(lines):
        if index >= 10000:
            raise ValidationError("Maksimal 10.000 baris per rencana kas.")
        identity = _get(line, "id", _get(line, "pk"))
        if identity is not None:
            identity = str(identity)
            if identity in seen:
                raise ValidationError("Baris rencana kas yang sama dikirim lebih dari sekali.")
            seen.add(identity)
        status = _get(line, "status", "planned")
        if status == "void":
            excluded["void"] += 1
            continue
        if status != "planned":
            raise ValidationError("Proyeksi hanya menerima baris rencana atau baris yang dibatalkan.")
        direction = _get(line, "direction")
        if direction not in ("inflow", "outflow"):
            raise ValidationError("Arah arus kas harus inflow atau outflow.")
        amount = _amount(_get(line, "amount"), "Jumlah rencana kas")
        if amount <= ZERO:
            raise ValidationError("Jumlah rencana kas harus lebih besar dari nol.")
        day = _day(_get(line, "date"))
        if day < start:
            excluded["before_start"] += 1
        elif day > end:
            excluded["after_horizon"] += 1
        else:
            bucket = buckets.setdefault(day, {"inflow": ZERO, "outflow": ZERO})
            bucket[direction] += amount
    daily = []
    balance, minimum = opening, opening
    first_shortfall = start.isoformat() if opening < reserve_amount else None
    for offset in range(91):
        day = start + timedelta(days=offset)
        bucket = buckets.get(day, {"inflow": ZERO, "outflow": ZERO})
        closing = balance + bucket["inflow"] - bucket["outflow"]
        minimum = min(minimum, closing)
        if closing < reserve_amount and first_shortfall is None:
            first_shortfall = day.isoformat()
        daily.append({"date": day.isoformat(), "opening_balance": _money(balance),
                      "inflows": _money(bucket["inflow"]), "outflows": _money(bucket["outflow"]),
                      "closing_balance": _money(closing), "below_reserve": closing < reserve_amount})
        balance = closing
    weeks = []
    for index in range(13):
        days = daily[index * 7:(index + 1) * 7]
        week_minimum = min([Decimal(days[0]["opening_balance"])] + [Decimal(day["closing_balance"]) for day in days])
        weeks.append({"week": index + 1, "start": days[0]["date"], "end": days[-1]["date"],
                      "opening_balance": days[0]["opening_balance"], "closing_balance": days[-1]["closing_balance"],
                      "inflows": _money(sum((Decimal(day["inflows"]) for day in days), ZERO)),
                      "outflows": _money(sum((Decimal(day["outflows"]) for day in days), ZERO)),
                      "minimum_balance": _money(week_minimum), "below_reserve": week_minimum < reserve_amount})
    warnings = [
        "SIMULASI dengan saldo awal asumsi pada awal hari; bukan saldo bank terverifikasi atau izin belanja.",
        "Saldo hanya mencakup baris rencana yang dimasukkan. Kewajiban lain dan keterlambatan penerimaan belum diketahui.",
        "Minimum dihitung pada akhir hari; urutan transaksi di dalam satu hari belum dimodelkan.",
    ]
    if excluded["before_start"]:
        warnings.append("Baris sebelum tanggal saldo awal dikecualikan. Pastikan penerimaan dan pengeluaran sebelumnya sudah tercermin dalam saldo awal asumsi.")
    if excluded["after_horizon"]:
        warnings.append("Ada baris di luar 13 minggu yang tidak termasuk total proyeksi ini.")
    return {
        "basis": "assumed", "verified": False, "as_of": start.isoformat(), "horizon_end": end.isoformat(),
        "opening_balance": _money(opening), "reserve": _money(reserve_amount),
        "weeks": weeks, "daily": daily[:14], "minimum_balance": _money(minimum),
        "closing_balance": _money(balance), "first_shortfall_date": first_shortfall,
        "total_inflows": _money(sum((Decimal(day["inflows"]) for day in daily), ZERO)),
        "total_outflows": _money(sum((Decimal(day["outflows"]) for day in daily), ZERO)),
        "excluded_line_count": sum(excluded.values()), "excluded_lines": excluded,
        "safe_to_spend": None, "warnings": warnings, "formula_version": FORMULA_VERSION,
    }


def budget_position(budget, entries):
    """Sum mutually exclusive active commitments; payments do not consume twice.

    Services own transitions/replacements. The caller must supply residual open
    commitments and reservations, not the full superseded original obligation.
    """
    amount = _amount(_get(budget, "amount", budget), "Budget")
    status = _get(budget, "status", "assumed")
    totals = {kind: ZERO for kind in ("incurred", "open_commitment", "reservation", "payment")}
    seen = set()
    for entry in entries:
        identity = _get(entry, "id", _get(entry, "pk"))
        if identity is not None:
            if str(identity) in seen:
                raise ValidationError("Entri budget yang sama dikirim lebih dari sekali.")
            seen.add(str(identity))
        if _get(entry, "voided_at") is not None:
            continue
        kind = _get(entry, "kind")
        if kind not in totals:
            raise ValidationError("Jenis entri budget tidak dikenal.")
        total = _amount(_get(entry, "amount"), "Jumlah entri budget")
        if total <= ZERO:
            raise ValidationError("Jumlah entri budget harus lebih besar dari nol.")
        totals[kind] += total
    consumed = totals["incurred"] + totals["open_commitment"] + totals["reservation"]
    warnings = ["Pembayaran ditampilkan terpisah dan tidak mengurangi budget lagi.",
                "Komitmen dan reservasi harus menunjukkan sisa aktif setelah penggantian; nominal penuh tidak boleh dicatat kembali sebagai biaya terjadi."]
    if status != "approved":
        warnings.append("Budget ini belum berstatus disetujui; nominal tersedia bukan izin belanja.")
    return {"amount": _money(amount), "status": status, "incurred": _money(totals["incurred"]),
            "open_commitments": _money(totals["open_commitment"]), "reservations": _money(totals["reservation"]),
            "payments": _money(totals["payment"]), "consumed": _money(consumed),
            "available": _money(amount - consumed), "over_budget": consumed > amount,
            "utilization_pct": _percentage(consumed, amount), "basis": "planning_records",
            "warnings": warnings, "formula_version": FORMULA_VERSION}
