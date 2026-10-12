"""Human-reviewed statement coverage, separate from transaction import and posting."""

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.audit import record

from .models import BankAccount, BankTransaction
from .statement_models import StatementPeriodReview


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _date(value):
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else value
        if (
            not isinstance(parsed, date)
            or isinstance(parsed, datetime)
            or not 2000 <= parsed.year <= 2200
        ):
            raise ValueError
        return parsed
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            "Tanggal cakupan harus YYYY-MM-DD dalam tahun 2000–2200."
        ) from exc


def _money(value, *, signed=False):
    try:
        if isinstance(value, (float, bool)):
            raise ValueError
        parsed = Decimal(value)
        if (
            not parsed.is_finite()
            or abs(parsed) >= Decimal("1e18")
            or parsed != parsed.quantize(Decimal("0.01"))
            or not signed
            and parsed < 0
        ):
            raise ValueError
        return parsed.quantize(Decimal("0.01"))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ValidationError(
            "Saldo atau total harus berupa rupiah yang valid, maksimal dua desimal."
        ) from exc


def _source_snapshot(statement, start, end):
    from webapp.statement_import import MAX_BYTES, normalize_statement, read_statement

    issues, normalized, digest = [], [], ""
    if not statement.confirmed_at:
        issues.append("Impor e-statement belum dikonfirmasi.")
    try:
        with statement.file.open("rb") as source:
            content = source.read(MAX_BYTES + 1)
        digest = hashlib.sha256(content).hexdigest()
        if digest != statement.sha256:
            issues.append("File e-statement berubah setelah diunggah.")
        _, rows = read_statement(content, statement.original_name)
        normalized, errors = normalize_statement(rows, statement.mapping)
        issues.extend(f"Baris {error['row']}: {error['message']}" for error in errors)
    except (OSError, ValidationError, ValueError, KeyError, TypeError):
        issues.append("File sumber atau pemetaan e-statement tidak dapat diperiksa.")
    if any(not start <= _date(row["date"]) <= end for row in normalized):
        issues.append("Ada transaksi sumber di luar periode yang dinyatakan.")
    rows = list(
        BankTransaction.objects.filter(
            organization_id=statement.organization_id,
            account_id=statement.account_id,
            date__range=(start, end),
        )
        .order_by("reference")
        .values("pk", "date", "reference", "description", "amount")
    )
    observed = sorted(
        (str(row["date"]), row["reference"], row["description"], str(row["amount"]))
        for row in rows
    )
    expected = sorted(
        (row["date"], row["reference"], row["description"], row["amount"])
        for row in normalized
    )
    if observed != expected:
        issues.append(
            "Mutasi tersimpan dan seluruh transaksi sumber periode ini tidak sama; periksa baris hilang, tambahan, atau periode file."
        )
    return {
        "source_sha256": digest,
        "declared_source_sha256": statement.sha256,
        "statement_id": statement.pk,
        "mapping": statement.mapping,
        "normalized": normalized,
        "bank_rows": rows,
    }, issues


def _preview(
    statement,
    *,
    period_start,
    period_end,
    opening_balance,
    closing_balance,
    total_in,
    total_out,
    transaction_count,
    source_order="ascending",
    evidence,
):
    start, end = _date(period_start), _date(period_end)
    if start > end or (end - start).days > 365 or end > timezone.localdate():
        raise ValidationError(
            "Cakupan harus berurutan, maksimal 366 hari, dan tidak melebihi hari ini."
        )
    if type(transaction_count) is not int or not 0 <= transaction_count <= 5000:
        raise ValidationError(
            "Jumlah transaksi harus bilangan bulat 0–5.000 sesuai e-statement."
        )
    if source_order not in {"ascending", "descending"}:
        raise ValidationError("Pilih urutan transaksi sumber.")
    if (
        not isinstance(evidence, str)
        or not evidence.strip()
        or len(evidence) > 4000
        or "\x00" in evidence
    ):
        raise ValidationError(
            "Referensi pemeriksaan sumber wajib diisi, maksimal 4.000 karakter."
        )
    opening, closing = (
        _money(opening_balance, signed=True),
        _money(closing_balance, signed=True),
    )
    incoming, outgoing = _money(total_in), _money(total_out)
    source, issues = _source_snapshot(statement, start, end)
    normalized = source["normalized"]
    actual_in = sum(
        (Decimal(row["amount"]) for row in normalized if Decimal(row["amount"]) > 0),
        Decimal(0),
    )
    actual_out = -sum(
        (Decimal(row["amount"]) for row in normalized if Decimal(row["amount"]) < 0),
        Decimal(0),
    )
    if (actual_in, actual_out, len(normalized)) != (
        incoming,
        outgoing,
        transaction_count,
    ):
        issues.append(
            "Total masuk/keluar atau jumlah transaksi berbeda dari kontrol e-statement."
        )
    if opening + incoming - outgoing != closing:
        issues.append(
            "Saldo awal + pemasukan − pengeluaran tidak sama dengan saldo akhir."
        )
    ordered = list(reversed(normalized)) if source_order == "descending" else normalized
    timestamps = [row.get("timestamp", row["date"]) for row in ordered]
    if timestamps != sorted(timestamps):
        issues.append("Urutan tanggal transaksi tidak sesuai urutan yang dipilih.")
    balance, running_checked = (
        opening,
        bool(ordered) and all(row.get("balance") is not None for row in ordered),
    )
    for row in ordered:
        balance += Decimal(row["amount"])
        if row.get("balance") is not None and balance != Decimal(row["balance"]):
            issues.append(
                f"Saldo berjalan tidak berkesinambungan pada baris {row['row']}."
            )
    snapshot = {
        "schema_version": "statement-coverage-v1",
        "organization_id": statement.organization_id,
        "account_id": statement.account_id,
        "period_start": str(start),
        "period_end": str(end),
        "opening_balance": str(opening),
        "closing_balance": str(closing),
        "total_in": str(incoming),
        "total_out": str(outgoing),
        "transaction_count": transaction_count,
        "source_order": source_order,
        "evidence": evidence.strip(),
        "running_balance_checked": running_checked,
        "source": source,
    }
    return {
        "fingerprint": _hash(snapshot),
        "snapshot": snapshot,
        "issues": issues,
        "actual_in": str(actual_in),
        "actual_out": str(actual_out),
        "actual_count": len(normalized),
    }


@transaction.atomic
def preview_statement_period(*, organization, actor, statement, **facts):
    from webapp.models import StatementImport

    from .services import _lock_org, _scoped, _write_actor

    org = _lock_org(organization)
    _write_actor(org, actor)
    source = _scoped(StatementImport, statement, org, lock=True)
    return _preview(source, **facts)


@transaction.atomic
def review_statement_period(
    *,
    organization,
    actor,
    statement,
    expected_fingerprint,
    confirmed=False,
    supersedes=None,
    **facts,
):
    from webapp.models import StatementImport

    from .services import _lock_org, _scoped, _write_actor

    org = _lock_org(organization)
    _write_actor(org, actor)
    source = _scoped(StatementImport, statement, org, lock=True)
    result = _preview(source, **facts)
    if confirmed is not True:
        raise ValidationError(
            "Konfirmasikan bahwa seluruh transaksi periode telah dibandingkan dengan sumber bank."
        )
    if (
        not isinstance(expected_fingerprint, str)
        or expected_fingerprint != result["fingerprint"]
    ):
        raise ValidationError(
            "Dasar pemeriksaan berubah. Tampilkan ulang pratinjau dan periksa kembali."
        )
    if result["issues"]:
        raise ValidationError(result["issues"])
    snapshot = result["snapshot"]
    start, end = _date(snapshot["period_start"]), _date(snapshot["period_end"])
    previous = (
        _scoped(StatementPeriodReview, supersedes, org, lock=True)
        if supersedes
        else None
    )
    overlapping = StatementPeriodReview.objects.filter(
        organization=org,
        account=source.account,
        replacement__isnull=True,
        period_start__lte=end,
        period_end__gte=start,
    )
    if previous:
        if (
            previous.account_id != source.account_id
            or previous.period_start != start
            or previous.period_end != end
            or hasattr(previous, "replacement")
        ):
            raise ValidationError(
                "Pilih pemeriksaan terakhir pada rekening dan periode yang sama sebagai pengganti."
            )
        overlapping = overlapping.exclude(pk=previous.pk)
    if overlapping.exists():
        raise ValidationError(
            "Periode tumpang tindih dengan pemeriksaan yang masih berlaku. Pilih pengganti periode yang sama atau pisahkan cakupan."
        )
    adjacent = StatementPeriodReview.objects.filter(
        organization=org, account=source.account, replacement__isnull=True
    )
    before = adjacent.filter(period_end=start - timedelta(days=1)).first()
    after = adjacent.filter(period_start=end + timedelta(days=1)).first()
    if (
        before
        and before.closing_balance != Decimal(snapshot["opening_balance"])
        or after
        and after.opening_balance != Decimal(snapshot["closing_balance"])
    ):
        raise ValidationError(
            "Saldo tidak berkesinambungan dengan periode sebelahnya. Periksa dan koreksi sumber sebelum mengesahkan."
        )
    obj = StatementPeriodReview(
        organization=org,
        account=source.account,
        statement=source,
        period_start=start,
        period_end=end,
        created_by=actor,
        supersedes=previous,
        fingerprint=result["fingerprint"],
        snapshot=json.loads(json.dumps(snapshot, default=str)),
        **{
            key: snapshot[key]
            for key in (
                "opening_balance",
                "closing_balance",
                "total_in",
                "total_out",
                "transaction_count",
                "source_order",
                "evidence",
            )
        },
    )
    obj._statement_review_write = True
    obj.save()
    record(
        org,
        actor,
        "finance.statement.period_reviewed",
        obj=obj,
        detail={
            "account_id": source.account_id,
            "period_start": str(start),
            "period_end": str(end),
            "fingerprint": obj.fingerprint,
            "supersedes_id": previous.pk if previous else None,
        },
    )
    return obj


def _review_current(review):
    try:
        facts = {
            key: getattr(review, key)
            for key in (
                "period_start",
                "period_end",
                "opening_balance",
                "closing_balance",
                "total_in",
                "total_out",
                "transaction_count",
                "source_order",
                "evidence",
            )
        }
        current = _preview(review.statement, **facts)
        return not current["issues"] and current[
            "fingerprint"
        ] == review.fingerprint, current["fingerprint"]
    except (ValidationError, OSError, ValueError):
        return False, "unavailable"


@transaction.atomic
def statement_coverage(*, organization, start, end, account=None):
    from .services import _lock_org, _scoped

    org = _lock_org(organization)
    start, end = _date(start), _date(end)
    if start > end or end > timezone.localdate():
        raise ValidationError("Interval cakupan tidak valid atau belum berakhir.")
    accounts = BankAccount.objects.filter(organization=org).order_by("pk")
    if account is not None:
        chosen = _scoped(BankAccount, account, org)
        accounts = accounts.filter(pk=chosen.pk)
    results = []
    for bank in accounts:
        reviews = list(
            StatementPeriodReview.objects.filter(
                organization=org,
                account=bank,
                replacement__isnull=True,
                period_start__lte=end,
                period_end__gte=start,
            )
            .select_related("statement")
            .order_by("period_start", "pk")
        )
        issues, ids, fingerprints, cursor, balance = [], [], [], start, None
        for review in reviews:
            current, fingerprint = _review_current(review)
            fingerprints.append(
                {"id": review.pk, "stored": review.fingerprint, "current": fingerprint}
            )
            if not current:
                issues.append(
                    f"Pemeriksaan #{review.pk} sudah tidak sesuai sumber/mutasi saat ini."
                )
                continue
            if review.period_start > cursor:
                issues.append(f"Cakupan terputus mulai {cursor.isoformat()}.")
            if balance is not None and review.opening_balance != balance:
                issues.append("Saldo antarperiode tidak berkesinambungan.")
            cursor = max(cursor, review.period_end + timedelta(days=1))
            balance = review.closing_balance
            ids.append(review.pk)
        if cursor <= end:
            issues.append(f"Belum ada pemeriksaan lengkap sampai {end.isoformat()}.")
        verified = bool(ids) and not issues
        # A review ending later covers the interval, but is not the closing balance of this exact end date.
        exact = next(
            (
                review
                for review in reviews
                if review.pk in ids and review.period_end == end
            ),
            None,
        )
        results.append(
            {
                "account_id": bank.pk,
                "verified": verified,
                "issues": issues,
                "review_ids": ids,
                "closing_balance": str(exact.closing_balance)
                if verified and exact
                else None,
                "fingerprints": fingerprints,
            }
        )
    output = {
        "verified": bool(results) and all(row["verified"] for row in results),
        "accounts": results,
        "basis": "finance_reviewed_statement_coverage",
        "start": str(start),
        "end": str(end),
    }
    output["fingerprint"] = _hash(output)
    return output


def statement_close_blockers(*, organization, end):
    from .services import report_balances

    end = _date(end)
    if not BankAccount.objects.filter(organization=organization).exists():
        return []
    if end > timezone.localdate():
        return [
            "Periode bank belum berakhir; pengesahan saldo akhir untuk tutup buku menunggu akhir periode."
        ]
    coverage = statement_coverage(
        organization=organization, start=end.replace(day=1), end=end
    )
    if not coverage["verified"]:
        return [
            "Cakupan e-statement rekening belum diperiksa lengkap atau sudah berubah; selesaikan rekonsiliasi saldo periode."
        ]
    if any(row["closing_balance"] is None for row in coverage["accounts"]):
        return [
            "Pemeriksaan e-statement harus menyediakan saldo akhir tepat pada tanggal akhir periode penutupan."
        ]
    source_balance = sum(
        (Decimal(row["closing_balance"]) for row in coverage["accounts"]), Decimal(0)
    )
    book_balance = report_balances(organization=organization, as_of=end).get(
        "BANK", Decimal(0)
    )
    if source_balance != book_balance:
        return [
            "Jumlah saldo akhir e-statement tidak sama dengan saldo BANK buku besar. Periksa saldo awal dan rekonsiliasi sebelum menutup periode."
        ]
    return []
