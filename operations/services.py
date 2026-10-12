"""Audited operations commands. Registration never implies payment or revenue."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from core.audit import record
from core.models import Membership
from director.models import CHANNEL_CHOICES
from finance import services as finance
from finance.models import Party, Product

from .models import (
    CourseAttendance,
    OperationEvent,
    Order,
    OrderLine,
    Participant,
    Registration,
    TestSession,
)


def _org(organization, actor):
    organization = finance._lock_org(organization)
    _require_roles(organization, actor, ("owner", "finance", "operations"))
    return organization


def _finance_org(organization, actor):
    organization = _org(organization, actor)
    _require_roles(organization, actor, ("owner", "finance"))
    return organization


def _require_roles(organization, actor, roles):
    if (
        actor is None
        or not getattr(actor, "is_authenticated", False)
        or not Membership.objects.filter(
            organization=organization,
            user=actor,
            user__is_active=True,
            role__in=roles,
        ).exists()
    ):
        raise PermissionDenied("Anda tidak memiliki peran untuk tindakan ini.")


def _save(obj):
    obj._service_write = True
    try:
        obj.save()
    finally:
        obj.__dict__.pop("_service_write", None)
    return obj


def _text(value, label, maximum=100, required=True):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise ValidationError(f"{label} tidak valid atau terlalu panjang.")
    if required and not value.strip():
        raise ValidationError(f"{label} wajib diisi.")
    return value.strip()


def _event(org, actor, obj, action, reason, before=None, after=None):
    _save(
        OperationEvent(
            organization=org,
            created_by=actor,
            object_type=obj._meta.model_name,
            object_id=obj.pk,
            action=action,
            before=before or {},
            after=after or {},
            reason=reason,
        )
    )
    record(
        org,
        actor,
        "operations." + action,
        obj=obj,
        detail={"reason": reason, "before": before or {}, "after": after or {}},
    )


def _version(obj, expected):
    if type(expected) is not int or obj.version != expected:
        raise ValidationError("Catatan telah berubah. Muat ulang sebelum menyimpan.")


@transaction.atomic
def create_order(*, organization, actor, reference, buyer, date, channel=""):
    org = _org(organization, actor)
    buyer = finance._scoped(Party, buyer, org)
    reference, date = _text(reference, "Nomor pesanan"), finance._date(date)
    if buyer.kind not in ("customer", "reseller") or channel not in {
        "",
        *dict(CHANNEL_CHOICES),
    }:
        raise ValidationError("Pilih pembeli dan kanal yang sesuai.")
    old = Order.objects.filter(organization=org, reference=reference).first()
    if old:
        if (old.buyer_id, old.date, old.channel) != (buyer.pk, date, channel):
            raise ValidationError(
                "Nomor pesanan sudah dipakai dengan pemesan/tanggal/kanal berbeda."
            )
        return old
    obj = _save(
        Order(
            organization=org,
            created_by=actor,
            reference=reference,
            buyer=buyer,
            date=date,
            channel=channel,
        )
    )
    _event(
        org,
        actor,
        obj,
        "order.created",
        "Pemesan dikonfirmasi",
        after={"buyer_id": buyer.pk, "channel": channel},
    )
    return obj


@transaction.atomic
def add_order_line(*, organization, actor, order, product, quantity, reference=None):
    from finance.fulfillment import _quantity

    org = _org(organization, actor)
    order = finance._scoped(Order, order, org, lock=True)
    product = finance._scoped(Product, product, org)
    reference = _text(reference or product.code, "Referensi rincian")
    quantity = _quantity(quantity)
    old = order.lines.filter(reference=reference).first()
    if old:
        if (old.product_id, old.quantity) != (product.pk, quantity):
            raise ValidationError(
                "Referensi rincian sudah digunakan untuk produk/jumlah berbeda."
            )
        return old
    obj = _save(
        OrderLine(
            organization=org,
            created_by=actor,
            order=order,
            product=product,
            quantity=quantity,
            reference=reference,
        )
    )
    _event(
        org,
        actor,
        obj,
        "line.created",
        "Rincian pesanan dicatat",
        after={"quantity": quantity, "product_id": product.pk},
    )
    return obj


@transaction.atomic
def register_participant(
    *,
    organization,
    actor,
    line,
    name,
    source,
    external_id,
    email="",
    phone="",
    planned_date=None,
):
    org = _org(organization, actor)
    line = finance._scoped(OrderLine, line, org, lock=True)
    name = _text(name, "Nama peserta", 200)
    source, external_id = (
        _text(source, "Sumber"),
        _text(external_id, "ID pendaftaran", 160),
    )
    email, phone = (
        _text(email, "Email", 254, False),
        _text(phone, "Nomor kontak", 40, False),
    )
    planned_date = finance._date(planned_date) if planned_date else None
    old = (
        Registration.objects.filter(
            organization=org, source=source, external_id=external_id
        )
        .select_related("participant")
        .first()
    )
    if old:
        original_correction = (
            OperationEvent.objects.filter(
                organization=org,
                object_type="registration",
                object_id=old.pk,
                action="registration.identity_corrected",
            )
            .order_by("pk")
            .first()
        )
        original_identity = original_correction.before if original_correction else None
        source_name = (
            original_identity.get("name") if original_identity else old.participant.name
        )
        source_email = (
            original_identity.get("email")
            if original_identity
            else old.participant.email
        )
        source_phone = (
            original_identity.get("phone")
            if original_identity
            else old.participant.phone
        )
        if (
            old.line_id,
            source_name,
            source_email,
            source_phone,
            old.planned_date,
        ) != (line.pk, name, email, phone, planned_date):
            raise ValidationError(
                "ID sumber memiliki data asli yang berbeda. Periksa sumber; jangan menggandakan pendaftaran."
            )
        return old
    if line.registrations.exclude(status="cancelled").count() >= line.quantity:
        raise ValidationError("Jumlah peserta sudah memenuhi jumlah pesanan.")
    if line.invoice_id:
        from finance.fulfillment import fulfillment_summary

        facts = fulfillment_summary(organization=org, invoice=line.invoice)
        pending = line.registrations.exclude(
            status__in=("cancelled", "completed")
        ).count()
        if line.invoice.status != "issued" or pending >= facts["remaining_quantity"]:
            raise ValidationError(
                "Invoice tidak memiliki unit terbuka untuk peserta baru. Periksa pelaksanaan atau koreksi sebelumnya."
            )
    participant = _save(
        Participant(
            organization=org, created_by=actor, name=name, email=email, phone=phone
        )
    )
    obj = _save(
        Registration(
            organization=org,
            created_by=actor,
            line=line,
            participant=participant,
            source=source,
            external_id=external_id,
            planned_date=planned_date,
        )
    )
    _event(
        org,
        actor,
        obj,
        "registration.created",
        "Pendaftaran diterima; belum membuktikan pembayaran",
        after={"line_id": line.pk},
    )
    return obj


def order_summary(*, organization, order):
    order = finance._scoped(Order, order, organization)
    registrations = Registration.objects.filter(
        organization=organization, line__order=order
    )
    return {
        "order": order,
        "lines": list(order.lines.select_related("product", "invoice")),
        "registration_count": registrations.count(),
        "completed_count": registrations.filter(status="completed").count(),
        "cancelled_count": registrations.filter(status="cancelled").count(),
    }


@transaction.atomic
def invoice_quote(*, organization, actor, line, date):
    import hashlib
    import json

    org = _finance_org(organization, actor)
    line = finance._scoped(OrderLine, line, org)
    date = finance._date(date)
    amount, price = finance._resolve_price(org, line.order.buyer, line.product, date)
    if amount <= 0 or line.quantity <= 0 or date < line.order.date:
        raise ValidationError(
            "Harga, jumlah, dan tanggal invoice harus sudah valid sebelum menerbitkan."
        )
    result = {
        "line_id": line.pk,
        "buyer_id": line.order.buyer_id,
        "product_id": line.product_id,
        "date": str(date),
        "quantity": line.quantity,
        "unit_price": str(amount),
        "total": str(amount * line.quantity),
        "price_version_id": price.pk if price else None,
    }
    result["fingerprint"] = hashlib.sha256(
        json.dumps(result, sort_keys=True).encode()
    ).hexdigest()
    return result


@transaction.atomic
def invoice_line(
    *, organization, actor, line, date, due_date=None, expected_price_fingerprint=None
):
    org = _finance_org(organization, actor)
    line = finance._scoped(OrderLine, line, org, lock=True)
    date = finance._date(date)
    if line.invoice_id:
        if line.invoice.date != date:
            raise ValidationError("Rincian sudah ditagihkan pada tanggal berbeda.")
        return line.invoice
    if line.quantity < 1 or date < line.order.date:
        raise ValidationError("Periksa jumlah pesanan dan tanggal invoice.")
    if (
        expected_price_fingerprint
        and invoice_quote(organization=org, actor=actor, line=line, date=date)[
            "fingerprint"
        ]
        != expected_price_fingerprint
    ):
        raise ValidationError(
            "Harga atau jumlah berubah setelah pratinjau. Periksa ulang sebelum menerbitkan."
        )
    invoice = finance.create_invoice(
        organization=org,
        actor=actor,
        party=line.order.buyer,
        product=line.product,
        quantity=line.quantity,
        date=date,
        due_date=due_date or date,
    )
    invoice = finance.issue_invoice(organization=org, actor=actor, invoice=invoice)
    line.invoice = invoice
    _save(line)
    _event(
        org,
        actor,
        line,
        "line.invoiced",
        "Harga berlaku dibekukan pada invoice",
        after={"invoice_id": invoice.pk},
    )
    return invoice


@transaction.atomic
def create_session(*, organization, actor, reference, product, starts_at, capacity):
    from datetime import datetime

    from finance.fulfillment import _quantity

    org = _org(organization, actor)
    product = finance._scoped(Product, product, org)
    if product.kind == "course":
        raise ValidationError(
            "Kursus perlu jadwal beberapa pertemuan. Gunakan halaman jadwal kursus."
        )
    reference, capacity = _text(reference, "Nama sesi"), _quantity(capacity)
    if not isinstance(starts_at, datetime) or timezone.is_naive(starts_at):
        raise ValidationError("Waktu sesi harus valid dan menyertakan zona waktu.")
    old = TestSession.objects.filter(organization=org, reference=reference).first()
    if old:
        if (old.product_id, old.starts_at, old.capacity) != (
            product.pk,
            starts_at,
            capacity,
        ):
            raise ValidationError(
                "Nama sesi sudah dipakai untuk jadwal/kapasitas berbeda."
            )
        return old
    obj = _save(
        TestSession(
            organization=org,
            created_by=actor,
            reference=reference,
            product=product,
            starts_at=starts_at,
            capacity=capacity,
        )
    )
    _event(org, actor, obj, "session.created", "Jadwal dan kapasitas dikonfirmasi")
    return obj


@transaction.atomic
def schedule_registration(
    *, organization, actor, registration, session, expected_version, reason
):
    org = _org(organization, actor)
    reg = finance._scoped(Registration, registration, org, lock=True)
    session = finance._scoped(TestSession, session, org, lock=True)
    _version(reg, expected_version)
    reason = _text(reason, "Alasan penjadwalan", 2000)
    if (
        reg.status in ("completed", "cancelled", "attended")
        or session.product_id != reg.line.product_id
        or reg.line.product.kind == "course"
    ):
        raise ValidationError(
            "Produk sesi dan keadaan pendaftaran tidak sesuai untuk penjadwalan."
        )
    if (
        session.registrations.exclude(status="cancelled").exclude(pk=reg.pk).count()
        >= session.capacity
    ):
        raise ValidationError("Kapasitas sesi sudah penuh. Pilih sesi lain.")
    before = {"session_id": reg.session_id, "status": reg.status}
    reg.session, reg.status, reg.version = session, "scheduled", reg.version + 1
    _save(reg)
    _event(
        org,
        actor,
        reg,
        "registration.scheduled",
        reason,
        before,
        {"session_id": session.pk, "status": reg.status},
    )
    return reg


def _eligibility(reg):
    from finance.fulfillment import fulfillment_summary
    from finance.settlement import applied_payment_amounts

    invoice = reg.line.invoice
    if not invoice or invoice.status != "issued":
        raise ValidationError("Peserta perlu invoice terbit sebelum tes dilaksanakan.")
    day = timezone.localdate(reg.session.starts_at)
    if invoice.date > day:
        raise ValidationError(
            "Tanggal invoice mendahului pelaksanaan diperlukan dalam alur ini."
        )
    if reg.line.order.buyer.kind == "reseller":
        require_order_payment(organization=reg.organization, invoice=invoice, as_of=day)
        paid = applied_payment_amounts(
            organization=reg.organization, invoice_ids=[invoice.pk], as_of=day
        )[invoice.pk]
        if (
            paid
            < fulfillment_summary(
                organization=reg.organization, invoice=invoice, as_of=day
            )["net_total"]
        ):
            raise ValidationError(
                "Pembayaran mitra belum lunas pada tanggal tes. Hubungkan pembayaran melalui Finance."
            )


def require_order_payment(*, organization, invoice, as_of):
    """Retained components of one partner order must all be billed and paid."""
    from finance.fulfillment import fulfillment_summary
    from finance.settlement import applied_payment_amounts

    line = (
        OrderLine.objects.filter(organization=organization, invoice=invoice)
        .select_related("order__buyer")
        .first()
    )
    if not line or line.order.buyer.kind != "reseller":
        return
    lines = list(line.order.lines.filter(quantity__gt=0).select_related("invoice"))
    if any(not item.invoice_id or item.invoice.date > as_of for item in lines):
        raise ValidationError(
            "Seluruh komponen pesanan mitra harus ditagihkan dan dilunasi sebelum tes."
        )
    paid = applied_payment_amounts(
        organization=organization,
        invoice_ids=[item.invoice_id for item in lines],
        as_of=as_of,
    )
    for item in lines:
        facts = fulfillment_summary(
            organization=organization, invoice=item.invoice, as_of=as_of
        )
        if facts["net_total"] > 0 and (
            item.invoice.status == "draft"
            or paid.get(item.invoice_id, 0) < facts["net_total"]
        ):
            raise ValidationError(
                "Masih ada komponen pesanan mitra yang belum lunas pada tanggal tes."
            )


@transaction.atomic
def record_attendance(
    *, organization, actor, registration, expected_version, attended, evidence
):
    org = _org(organization, actor)
    reg = finance._scoped(Registration, registration, org, lock=True)
    _version(reg, expected_version)
    if reg.line.product.kind == "course":
        raise ValidationError(
            "Untuk kursus, catat kehadiran pada setiap pertemuan di halaman jadwal kursus."
        )
    evidence = _text(evidence, "Bukti kehadiran", 2000)
    if (
        type(attended) is not bool
        or reg.status != "scheduled"
        or not reg.session_id
        or reg.session.starts_at > timezone.now()
    ):
        raise ValidationError(
            "Kehadiran hanya dapat dicatat untuk peserta terjadwal setelah sesi dimulai."
        )
    if attended:
        _eligibility(reg)
    before = {"status": reg.status}
    reg.status, reg.version = ("attended" if attended else "absent"), reg.version + 1
    _save(reg)
    _event(
        org,
        actor,
        reg,
        "registration.attendance",
        evidence,
        before,
        {"status": reg.status},
    )
    return reg


@transaction.atomic
def correct_attendance(
    *, organization, actor, registration, expected_version, attended, reason
):
    """Correct a recorded attendance result before its service unit is delivered."""
    org = _org(organization, actor)
    reg = finance._scoped(Registration, registration, org, lock=True)
    _version(reg, expected_version)
    if reg.line.product.kind == "course":
        raise ValidationError(
            "Koreksi kursus dilakukan pada catatan pertemuan di halaman jadwal kursus."
        )
    reason = _text(reason, "Alasan koreksi kehadiran", 2000)
    if (
        type(attended) is not bool
        or reg.status not in ("attended", "absent")
        or not reg.session_id
        or reg.delivery_id
        or reg.session.starts_at > timezone.now()
    ):
        raise ValidationError(
            "Kehadiran hanya dapat dikoreksi setelah sesi dimulai dan sebelum layanan dicatat selesai."
        )
    corrected_status = "attended" if attended else "absent"
    if reg.status == corrected_status:
        raise ValidationError(
            "Hasil yang dipilih sama dengan catatan saat ini. Periksa ulang sebelum menyimpan koreksi."
        )
    if attended:
        _eligibility(reg)
    before = {"status": reg.status}
    reg.status, reg.version = corrected_status, reg.version + 1
    _save(reg)
    _event(
        org,
        actor,
        reg,
        "registration.attendance_corrected",
        reason,
        before,
        {"status": reg.status},
    )
    return reg


@transaction.atomic
def correct_registration_identity(
    *,
    organization,
    actor,
    registration,
    expected_version,
    name,
    email="",
    phone="",
    reason,
):
    """Correct one registration while keeping its source participant unchanged."""
    org = _org(organization, actor)
    reg = finance._scoped(Registration, registration, org, lock=True)
    _version(reg, expected_version)
    name = _text(name, "Nama peserta", 200)
    email = _text(email, "Email peserta", 254, required=False)
    phone = _text(phone, "Nomor kontak", 40, required=False)
    reason = _text(reason, "Alasan dan bukti koreksi", 2000)
    if email:
        try:
            validate_email(email)
        except ValidationError as exc:
            raise ValidationError("Email peserta tidak valid.") from exc

    original = reg.participant
    before = {
        "participant_id": original.pk,
        "name": original.name,
        "email": original.email,
        "phone": original.phone,
    }
    if (name, email, phone) == (original.name, original.email, original.phone):
        raise ValidationError("Tidak ada perubahan data peserta untuk disimpan.")

    corrected = _save(
        Participant(
            organization=org,
            created_by=actor,
            name=name,
            email=email,
            phone=phone,
        )
    )
    reg.participant = corrected
    reg.version += 1
    reg._allow_participant_identity_correction = True
    _save(reg)
    after = {
        "participant_id": corrected.pk,
        "name": corrected.name,
        "email": corrected.email,
        "phone": corrected.phone,
    }
    _save(
        OperationEvent(
            organization=org,
            created_by=actor,
            object_type=reg._meta.model_name,
            object_id=reg.pk,
            action="registration.identity_corrected",
            before=before,
            after=after,
            reason=reason,
        )
    )
    # Keep PII and free-text reasons in the restricted registration history only.
    record(
        org,
        actor,
        "operations.registration.identity_corrected",
        obj=reg,
        detail={"fields": ["name", "email", "phone"]},
    )
    return reg


@transaction.atomic
def complete_registration(
    *, organization, actor, registration, expected_version, date, evidence
):
    from finance.fulfillment import deliver_units

    org = _finance_org(organization, actor)
    reg = finance._scoped(Registration, registration, org, lock=True)
    _version(reg, expected_version)
    date, evidence = (
        finance._actual_date(date),
        _text(evidence, "Bukti penyelesaian", 2000),
    )
    if reg.line.product.kind == "course":
        raise ValidationError(
            "Kursus diselesaikan per rangkaian pertemuan. Gunakan jadwal kursus; jangan tandai satu sesi sebagai seluruh kursus selesai."
        )
    if (
        reg.status != "attended"
        or not reg.session_id
        or date < timezone.localdate(reg.session.starts_at)
    ):
        raise ValidationError(
            "Konfirmasikan kehadiran dan tanggal selesai yang sesuai sesi terlebih dahulu."
        )
    _eligibility(reg)
    delivery = deliver_units(
        organization=org,
        actor=actor,
        invoice=reg.line.invoice,
        quantity=1,
        date=date,
        reference=f"registration:{reg.pk}",
        evidence=evidence,
        registration=reg,
    )
    reg.delivery, reg.status, reg.version = delivery, "completed", reg.version + 1
    _save(reg)
    _event(
        org,
        actor,
        reg,
        "registration.completed",
        evidence,
        {"status": "attended"},
        {"delivery_id": delivery.pk},
    )
    return reg


@transaction.atomic
def cancel_registration(
    *, organization, actor, registration, expected_version, date, reason
):
    from finance.fulfillment import credit_units

    org = _org(organization, actor)
    finance._owner(org, actor)
    reg = finance._scoped(Registration, registration, org, lock=True)
    _version(reg, expected_version)
    date, reason = finance._actual_date(date), _text(reason, "Alasan pembatalan", 2000)
    if date < reg.line.order.date:
        raise ValidationError("Pembatalan tidak boleh mendahului tanggal pesanan.")
    if (
        reg.line.product.kind == "course"
        and CourseAttendance.objects.filter(
            organization=org,
            registration=reg,
            status=CourseAttendance.Status.ATTENDED,
        ).exists()
    ):
        raise ValidationError(
            "Peserta sudah mengikuti pertemuan kursus. Pembatalan dan kredit penuh harus ditinjau Finance sesuai kebijakan pengembalian yang disetujui."
        )
    if reg.status in ("completed", "cancelled"):
        raise ValidationError(
            "Pendaftaran selesai/dibatalkan tidak dapat dibatalkan ulang. Gunakan koreksi Finance untuk layanan selesai."
        )
    before = {"status": reg.status}
    if reg.line.invoice_id:
        credit = credit_units(
            organization=org,
            actor=actor,
            invoice=reg.line.invoice,
            quantity=1,
            kind="unfulfilled",
            date=date,
            reference=f"registration-cancel:{reg.pk}",
            evidence=reason,
            registration=reg,
        )
        before["credit_id"] = credit.pk
    else:
        line = finance._scoped(OrderLine, reg.line, org, lock=True)
        line.quantity = max(0, line.quantity - 1)
        _save(line)
    reg.status, reg.version = "cancelled", reg.version + 1
    _save(reg)
    _event(
        org,
        actor,
        reg,
        "registration.cancelled",
        reason,
        before,
        {"status": "cancelled", "date": str(date)},
    )
    return reg


@transaction.atomic
def link_order_lead(
    *,
    organization,
    actor,
    order,
    lead,
    expected_version,
    reason,
    confirm_identity=False,
):
    from marketing.models import Lead

    org = _finance_org(organization, actor)
    order = finance._scoped(Order, order, org, lock=True)
    _version(order, expected_version)
    lead = finance._scoped(Lead, lead, org, lock=True) if lead is not None else None
    reason = _text(reason, "Alasan pengaitan / koreksi", 2000)
    if lead:
        if (
            lead.party_id
            and lead.party_id != order.buyer_id
            or order.date < timezone.localdate(lead.received_at)
        ):
            raise ValidationError(
                "Pemesan dan tanggal pesanan harus sesuai dengan peluang pembelian."
            )
        if not lead.party_id and confirm_identity is not True:
            raise ValidationError(
                "Konfirmasikan identitas pemesan untuk prospek yang belum memiliki identitas pembeli."
            )
        if (lead.segment == "reseller") != (order.buyer.kind == "reseller"):
            raise ValidationError("Segmen prospek dan jenis pemesan harus sesuai.")
        if (
            Order.objects.filter(organization=org, lead=lead)
            .exclude(pk=order.pk)
            .exists()
        ):
            raise ValidationError(
                "Peluang pembelian sudah terhubung ke pesanan lain. Catat peluang baru untuk pembelian berulang."
            )
        invoice_ids = list(
            order.lines.exclude(invoice=None).values_list("invoice_id", flat=True)
        )
        if lead.invoice_id and lead.invoice_id not in invoice_ids:
            raise ValidationError(
                "Prospek memiliki invoice lama. Hubungkan invoice itu ke rincian pesanan dahulu atau koreksi atribusinya."
            )
        if (
            Lead.objects.filter(organization=org, invoice_id__in=invoice_ids)
            .exclude(pk=lead.pk)
            .exists()
        ):
            raise ValidationError(
                "Invoice pesanan masih diatribusikan ke prospek lain. Koreksi hubungan lama dahulu."
            )
    before = {"lead_id": order.lead_id}
    order.lead, order.version = lead, order.version + 1
    _save(order)
    _event(
        org,
        actor,
        order,
        "order.attribution_changed",
        reason,
        before,
        {"lead_id": order.lead_id},
    )
    return order


@transaction.atomic
def link_order_invoice(*, organization, actor, line, invoice, reason):
    from finance.models import Invoice
    from marketing.models import Lead

    org = _finance_org(organization, actor)
    line = finance._scoped(OrderLine, line, org, lock=True)
    invoice = finance._scoped(Invoice, invoice, org, lock=True)
    reason = _text(reason, "Alasan pengaitan", 2000)
    if line.invoice_id == invoice.pk:
        return line
    if (
        line.invoice_id
        or OrderLine.objects.filter(organization=org, invoice=invoice).exists()
    ):
        raise ValidationError(
            "Rincian atau invoice sudah terhubung. Hubungan tagihan tidak boleh ditimpa."
        )
    if (
        invoice.status != "issued"
        or (invoice.party_id, invoice.product_id, invoice.quantity)
        != (line.order.buyer_id, line.product_id, line.quantity)
        or invoice.date < line.order.date
    ):
        raise ValidationError(
            "Pilih invoice terbit dengan pemesan, produk, kuantitas, dan tanggal yang sesuai."
        )
    if invoice.unit_deliveries.exists() or invoice.unit_credits.exists():
        raise ValidationError(
            "Invoice sudah memiliki pelaksanaan atau koreksi unit. Hubungan peserta harus diperiksa melalui migrasi terkontrol."
        )
    if (
        line.order.lead_id
        and Lead.objects.filter(organization=org, invoice=invoice)
        .exclude(pk=line.order.lead_id)
        .exists()
    ):
        raise ValidationError(
            "Invoice diatribusikan ke prospek lain; koreksi terlebih dahulu."
        )
    line.invoice = invoice
    _save(line)
    _event(
        org,
        actor,
        line,
        "line.invoice_linked",
        reason,
        after={"invoice_id": invoice.pk},
    )
    return line
