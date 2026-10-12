"""Accounting for reviewed tax amounts; no rate or regime inference here."""

from datetime import date as Date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from core.audit import record

from .models import (
    AdvanceApplication,
    Allocation,
    BankAccount,
    BankPosting,
    BankTransaction,
    CustomerAdvance,
    CustomerTaxSettlement,
    Invoice,
    Journal,
    OwnTaxCreditApplication,
    OwnTaxPayment,
    OwnTaxRecognition,
    Party,
)

ZERO = Decimal("0.00")


def _assert_payment_tax_point(obligation, bank_date):
    """Check the reviewed date against this supported bank settlement, not infer it."""
    if obligation.tax_type != "pph23":
        return
    point = obligation.tax_point_date
    if type(point) is not Date or type(bank_date) is not Date:
        raise ValidationError(
            "Tanggal mutasi dan tanggal terutang yang ditinjau wajib tersedia."
        )
    if bank_date < point or (
        obligation.tax_point_basis == "paid" and bank_date != point
    ):
        raise ValidationError(
            "Tanggal mutasi tidak cocok dengan waktu terutang yang ditinjau. Periksa bukti pembayaran dan keputusan profesional; aplikasi tidak mengganti masa pajak otomatis."
        )


def _current_obligation(obligation):
    from taxes.recipient_identity import assert_pph23_obligation
    from taxes.workflow import digest, subject_snapshot

    if obligation.status != "approved" or not obligation.external_review_id:
        raise ValidationError(
            "Gunakan kewajiban yang telah disetujui berdasarkan laporan pemeriksa pajak."
        )
    if obligation.external_review.subject_digest != digest(
        {
            "subject": subject_snapshot(obligation),
            "decision": {"action": "approve_obligation"},
        }
    ):
        raise ValidationError(
            "Dokumen pemeriksaan tidak cocok dengan versi kewajiban ini."
        )
    assert_pph23_obligation(obligation)


def _customer_credit_source(organization, obligation):
    from taxes.models import TaxVerification
    from taxes.recipient_identity import assert_pph23_credit_verification
    from taxes.workflow import digest, subject_snapshot

    from .services import _scoped

    _current_obligation(obligation)
    if (
        obligation.tax_type != "pph23"
        or obligation.direction != "receivable"
        or not obligation.source_invoice_id
        or not obligation.amount
        or obligation.amount <= ZERO
    ):
        raise ValidationError(
            "Potongan pelanggan membutuhkan kewajiban PPh 23 positif yang disetujui dan tertaut ke invoice tepat."
        )
    invoice = _scoped(Invoice, obligation.source_invoice_id, organization, lock=True)
    verification = (
        TaxVerification.objects.select_for_update()
        .filter(organization=organization, obligation=obligation, kind="credit")
        .first()
    )
    if (
        not verification
        or verification.amount != obligation.amount
        or verification.obligation_digest != digest(subject_snapshot(obligation))
    ):
        raise ValidationError(
            "Pemeriksa harus mencocokkan bukti potong resmi pelanggan sebelum kredit pajak dicatat."
        )
    assert_pph23_credit_verification(verification, obligation)
    if invoice.status not in ("issued", "delivered") or invoice.unit_credits.exists():
        raise ValidationError(
            "Gunakan invoice aktif tanpa koreksi layanan; koreksi terkait bukti potong memerlukan alur pajak tersendiri."
        )
    return invoice, verification


@transaction.atomic
def reconcile_customer_tax_receipt(
    *, organization, actor, obligation, transaction, amount, reference, evidence
):
    from taxes.models import TaxObligation

    from .services import (
        _amount,
        _bank_post,
        _event_row,
        _lock_org,
        _review_text,
        _scoped,
        _write_actor,
    )

    organization = _lock_org(organization)
    _write_actor(organization, actor)
    obligation = _scoped(TaxObligation, obligation, organization, lock=True)
    bank_line = _scoped(BankTransaction, transaction, organization, lock=True)
    amount = _amount(amount)
    reference = _review_text(reference, "Referensi", limit=120)
    evidence = _review_text(evidence, "Bukti")
    old = CustomerTaxSettlement.objects.filter(
        organization=organization, obligation=obligation
    ).first()
    if old:
        if not old.bank_posting_id or (
            old.reference,
            old.bank_posting.transaction_id,
            old.cash_amount,
            old.evidence,
        ) != (reference, bank_line.pk, amount, evidence):
            raise ValidationError(
                "Bukti potong ini sudah digunakan. Periksa pencocokan yang telah tersimpan."
            )
        return old
    if CustomerTaxSettlement.objects.filter(
        organization=organization, reference=reference
    ).exists():
        raise ValidationError(
            "Referensi sudah dipakai untuk bukti potong atau invoice berbeda."
        )
    invoice, verification = _customer_credit_source(organization, obligation)
    _assert_payment_tax_point(obligation, bank_line.date)
    if (
        bank_line.date < invoice.date
        or bank_line.date.replace(day=1) != obligation.period
    ):
        raise ValidationError(
            "Tanggal penerimaan harus setelah invoice dan berada dalam masa potongan yang telah diperiksa."
        )
    gross = amount + obligation.amount
    if gross > invoice.outstanding_amount:
        raise ValidationError(
            "Uang masuk ditambah potongan pajak tidak boleh melebihi sisa invoice."
        )
    posting = _bank_post(
        organization,
        bank_line,
        amount,
        "customer_tax_receipt",
        reference,
        evidence,
        [
            ("BANK", amount, ZERO),
            ("TAX_RECEIVABLE", obligation.amount, ZERO),
            ("AR", ZERO, gross),
        ],
        actor,
    )
    row = _event_row(
        CustomerTaxSettlement,
        organization=organization,
        invoice=invoice,
        obligation=obligation,
        verification=verification,
        bank_posting=posting,
        cash_amount=amount,
        tax_amount=obligation.amount,
        gross_amount=gross,
        journal=posting.journal,
        date=posting.date,
        reference=reference,
        evidence=evidence,
    )
    record(
        organization,
        actor,
        "finance.customer_tax.receipt_reconciled",
        obj=row,
        detail={
            "invoice_id": invoice.pk,
            "obligation_id": obligation.pk,
            "cash": str(amount),
            "credit": str(obligation.amount),
            "settled": str(gross),
        },
    )
    return row


def _original_journal(organization, journal_id, source_key, posting_date, entries):
    """Check all original lines; do not recreate or reopen a historical journal."""
    from .services import _scoped

    journal = _scoped(Journal, journal_id, organization, lock=True)
    lines = list(
        journal.lines.order_by("pk").values_list(
            "organization_id", "account", "debit", "credit"
        )
    )
    expected = [
        (organization.pk, account, debit, credit) for account, debit, credit in entries
    ]
    if (
        journal.status != "posted"
        or journal.source_key != source_key
        or journal.date != posting_date
        or lines != expected
    ):
        raise ValidationError(
            "Jurnal pembayaran atau penerapan lama tidak cocok dengan bukti sumber. Periksa riwayat sebelum mencatat kredit pajak."
        )
    return journal


def _advance_credit_source(organization, application, invoice):
    from .services import _scoped

    advance = _scoped(CustomerAdvance, application.advance_id, organization, lock=True)
    party = _scoped(Party, advance.party_id, organization, lock=True)
    posting = _scoped(BankPosting, advance.bank_posting_id, organization, lock=True)
    bank_line = _scoped(
        BankTransaction, posting.transaction_id, organization, lock=True
    )
    _scoped(BankAccount, bank_line.account_id, organization, lock=True)
    if application.invoice_id != invoice.pk or party.pk != invoice.party_id:
        raise ValidationError(
            "Pilih penerapan uang muka yang sudah tercatat untuk invoice dan pelanggan yang sama."
        )
    if (
        application.amount <= ZERO
        or advance.amount <= ZERO
        or application.amount > advance.amount
        or bank_line.amount < advance.amount
        or posting.signed_amount != advance.amount
        or posting.kind != "customer_advance"
        or advance.journal_id != posting.journal_id
        or advance.date != bank_line.date
        or posting.date != bank_line.date
        or advance.reference != posting.reference
        or advance.evidence != posting.evidence
        or application.date < max(advance.date, invoice.date)
    ):
        raise ValidationError(
            "Bukti uang muka dan penerapannya tidak cocok dengan riwayat pembayaran asli."
        )
    _original_journal(
        organization,
        advance.journal_id,
        f"bank-event:{posting.reference}",
        bank_line.date,
        [("BANK", advance.amount, ZERO), ("CUSTOMER_ADVANCE", ZERO, advance.amount)],
    )
    _original_journal(
        organization,
        application.journal_id,
        f"advance-apply:{application.reference}",
        application.date,
        [
            ("CUSTOMER_ADVANCE", application.amount, ZERO),
            ("AR", ZERO, application.amount),
        ],
    )
    return bank_line


@transaction.atomic
def recognize_customer_tax_credit(
    *,
    organization,
    actor,
    obligation,
    date,
    reference,
    evidence,
    source_allocation=None,
    source_advance_application=None,
):
    """Recognize a verified late credit against an existing receipt or applied advance, with no new cash."""
    from taxes.models import TaxObligation

    from .services import (
        _actual_date,
        _event_row,
        _lock_org,
        _post,
        _review_text,
        _scoped,
        _write_actor,
    )

    organization = _lock_org(organization)
    _write_actor(organization, actor)
    obligation = _scoped(TaxObligation, obligation, organization, lock=True)
    if (source_allocation is None) == (source_advance_application is None):
        raise ValidationError(
            "Pilih satu pembayaran lama atau satu penerapan uang muka yang sudah tercatat."
        )
    allocation = (
        _scoped(Allocation, source_allocation, organization, lock=True)
        if source_allocation is not None
        else None
    )
    application = (
        _scoped(AdvanceApplication, source_advance_application, organization, lock=True)
        if source_advance_application is not None
        else None
    )
    date = _actual_date(date)
    reference = _review_text(reference, "Referensi", limit=120)
    evidence = _review_text(evidence, "Bukti")
    old = CustomerTaxSettlement.objects.filter(
        organization=organization, obligation=obligation
    ).first()
    if old:
        if old.bank_posting_id or (
            old.reference,
            old.source_allocation_id,
            old.source_advance_application_id,
            old.date,
            old.evidence,
        ) != (
            reference,
            getattr(allocation, "pk", None),
            getattr(application, "pk", None),
            date,
            evidence,
        ):
            raise ValidationError(
                "Bukti potong ini sudah digunakan. Periksa pencocokan yang telah tersimpan."
            )
        return old
    if CustomerTaxSettlement.objects.filter(
        organization=organization, reference=reference
    ).exists():
        raise ValidationError(
            "Referensi sudah dipakai untuk bukti potong atau invoice berbeda."
        )
    invoice, verification = _customer_credit_source(organization, obligation)
    if allocation is not None:
        bank_line = _scoped(
            BankTransaction, allocation.transaction_id, organization, lock=True
        )
        _scoped(BankAccount, bank_line.account_id, organization, lock=True)
        if (
            allocation.invoice_id != invoice.pk
            or allocation.amount <= ZERO
            or bank_line.amount < allocation.amount
        ):
            raise ValidationError(
                "Pilih pembayaran yang sudah dicocokkan ke invoice yang sama."
            )
        cash_journal = Journal.objects.filter(
            organization=organization, source_key=f"allocation:{allocation.pk}"
        ).first()
        if not cash_journal:
            raise ValidationError(
                "Pembayaran lama harus memiliki jurnal penerimaan terposting yang cocok."
            )
        _original_journal(
            organization,
            cash_journal.pk,
            f"allocation:{allocation.pk}",
            bank_line.date,
            [("BANK", allocation.amount, ZERO), ("AR", ZERO, allocation.amount)],
        )
        if bank_line.date < invoice.date:
            raise ValidationError(
                "Pembayaran invoice tidak boleh mendahului invoice. Pilih penerapan uang muka bila pembayaran telah diterima sebelumnya."
            )
        earliest_date = max(bank_line.date, invoice.date, obligation.period)
    else:
        bank_line = _advance_credit_source(organization, application, invoice)
        earliest_date = max(
            bank_line.date, application.date, invoice.date, obligation.period
        )
    _assert_payment_tax_point(obligation, bank_line.date)
    if date < earliest_date:
        raise ValidationError(
            "Tanggal pencatatan kredit tidak boleh mendahului invoice, pembayaran lama, penerapan uang muka, atau masa pajak."
        )
    if obligation.amount > invoice.outstanding_amount:
        raise ValidationError(
            "Kredit pajak tidak boleh melebihi sisa invoice yang masih terbuka."
        )
    journal = _post(
        organization,
        date,
        f"customer-tax-credit:{obligation.pk}",
        f"Bukti potong diterima setelah pembayaran {invoice.number}",
        [("TAX_RECEIVABLE", obligation.amount, ZERO), ("AR", ZERO, obligation.amount)],
    )
    row = _event_row(
        CustomerTaxSettlement,
        organization=organization,
        invoice=invoice,
        obligation=obligation,
        verification=verification,
        source_allocation=allocation,
        source_advance_application=application,
        bank_posting=None,
        cash_amount=ZERO,
        tax_amount=obligation.amount,
        gross_amount=obligation.amount,
        journal=journal,
        date=date,
        reference=reference,
        evidence=evidence,
    )
    record(
        organization,
        actor,
        "finance.customer_tax.late_credit_recognized",
        obj=row,
        detail={
            "invoice_id": invoice.pk,
            "obligation_id": obligation.pk,
            "source_allocation_id": getattr(allocation, "pk", None),
            "source_advance_application_id": getattr(application, "pk", None),
            "cash": "0.00",
            "credit": str(obligation.amount),
        },
    )
    return row


@transaction.atomic
def recognize_own_tax(
    *,
    organization,
    actor,
    date,
    reference,
    evidence,
    obligation=None,
    workpaper=None,
    instruction_evidence=None,
    professional_instruction="",
    late_recognition_confirmed=False,
):
    from taxes.models import AnnualTaxWorkpaper, TaxEvidence, TaxObligation
    from taxes.workflow import annual_snapshot_status, digest, subject_snapshot

    from .services import (
        _actual_date,
        _amount,
        _event_row,
        _lock_org,
        _post,
        _review_text,
        _scoped,
        _write_actor,
    )

    organization = _lock_org(organization)
    _write_actor(organization, actor)
    date = _actual_date(date)
    reference = _review_text(reference, "Referensi", limit=120)
    evidence = _review_text(evidence, "Bukti")
    if (obligation is None) == (workpaper is None):
        raise ValidationError(
            "Pilih satu sumber: pajak final masa atau kertas kerja tahunan normal yang disetujui."
        )
    obligation = (
        _scoped(TaxObligation, obligation, organization, lock=True)
        if obligation is not None
        else None
    )
    workpaper = (
        _scoped(AnnualTaxWorkpaper, workpaper, organization, lock=True)
        if workpaper is not None
        else None
    )
    instruction_evidence = (
        _scoped(TaxEvidence, instruction_evidence, organization)
        if instruction_evidence is not None
        else None
    )
    if workpaper is not None:
        if late_recognition_confirmed is not True or instruction_evidence is None:
            raise ValidationError(
                "Tahun buku sudah ditutup. Lampirkan instruksi tertulis profesional untuk tanggal jurnal di periode terbuka; perubahan/reopen tahun lama belum tersedia."
            )
        professional_instruction = _review_text(
            professional_instruction, "Instruksi profesional", limit=2000
        )
        if len(professional_instruction) < 20:
            raise ValidationError(
                "Catat nama pemeriksa, tanggal jurnal yang diinstruksikan, dan perlakuan atas tahun buku yang sudah ditutup."
            )
    old = OwnTaxRecognition.objects.filter(
        organization=organization, reference=reference
    ).first()
    if old:
        if (
            old.obligation_id,
            old.workpaper_id,
            old.date,
            old.evidence,
            old.instruction_evidence_id,
            old.professional_instruction,
        ) != (
            getattr(obligation, "pk", None),
            getattr(workpaper, "pk", None),
            date,
            evidence,
            getattr(instruction_evidence, "pk", None),
            professional_instruction,
        ):
            raise ValidationError(
                "Referensi pengakuan sudah dipakai untuk catatan berbeda."
            )
        return old
    applications = []
    if obligation is not None:
        _current_obligation(obligation)
        if (
            obligation.tax_type != "final_turnover"
            or obligation.direction != "own_tax"
            or obligation.source_bill_id
            or obligation.source_invoice_id
        ):
            raise ValidationError(
                "Pengakuan masa hanya mendukung pajak final perusahaan yang telah disetujui. PPh 25 dan jenis lain belum didukung."
            )
        if OwnTaxRecognition.objects.filter(
            organization=organization, final_period=obligation.period
        ).exists():
            raise ValidationError(
                "Pajak final masa ini telah diakui; lanjutkan pencocokan pembayaran. Perubahan nominal memerlukan koreksi profesional tersendiri."
            )
        if date < obligation.period:
            raise ValidationError(
                "Tanggal pengakuan tidak boleh mendahului masa pajak."
            )
        expense, credits, year = _amount(obligation.amount), ZERO, None
    else:
        if not hasattr(
            workpaper, "approval"
        ) or workpaper.approval.external_review.subject_digest != digest(
            {
                "subject": subject_snapshot(workpaper),
                "decision": {"action": "approve_annual"},
            }
        ):
            raise ValidationError(
                "Gunakan kertas kerja tahunan dengan persetujuan profesional yang cocok."
            )
        status = annual_snapshot_status(organization, workpaper)
        if (
            not status["snapshot_current"]
            or not status["preparation_supported"]
            or workpaper.result.get("regime_code")
            not in ("corporate_22", "corporate_31e_small_11")
        ):
            raise ValidationError(
                "Paket tahunan harus normal, masih cocok dengan buku/bukti, dan menggunakan cabang PPh badan yang didukung."
            )
        if (
            Decimal(str(workpaper.result.get("instalments", "0"))) != ZERO
            or Decimal(str(workpaper.inputs.get("instalments", "0"))) != ZERO
        ):
            raise ValidationError(
                "Angsuran PPh 25 memerlukan register pembayaran di muka; pengakuan otomatis kasus ini belum didukung."
            )
        if OwnTaxRecognition.objects.filter(
            organization=organization, corporate_year=workpaper.year
        ).exists():
            raise ValidationError(
                "PPh badan tahun ini telah diakui. Pembetulan membutuhkan rekonsiliasi dan keputusan profesional tersendiri."
            )
        if date < Date.fromisoformat(workpaper.book_snapshot["fiscal_end"]):
            raise ValidationError(
                "Tanggal pengakuan tidak boleh mendahului akhir tahun buku."
            )
        expense = _amount(workpaper.result["income_tax"])
        credits = _amount(workpaper.result["credits"], positive=False)
        year = workpaper.year
        verification_ids = workpaper.inputs.get("credit_verification_ids", [])
        applications = list(
            CustomerTaxSettlement.objects.select_for_update()
            .filter(organization=organization, verification_id__in=verification_ids)
            .select_related("verification", "obligation")
        )
        if (
            len(applications) != len(verification_ids)
            or sum((row.tax_amount for row in applications), ZERO) != credits
        ):
            raise ValidationError(
                "Seluruh kredit pada paket tahunan harus memiliki penerimaan pelanggan dan aset kredit pajak yang sudah dibukukan."
            )
        for row in applications:
            if (
                row.date > date
                or OwnTaxCreditApplication.objects.filter(settlement=row).exists()
                or row.verification.obligation_digest
                != digest(subject_snapshot(row.obligation))
            ):
                raise ValidationError(
                    "Kredit pajak harus cocok, sudah terjadi, dan belum pernah digunakan pada pengakuan lain."
                )
            from taxes.recipient_identity import (
                assert_pph23_credit_verification,
                assert_pph23_obligation,
            )

            assert_pph23_obligation(row.obligation)
            assert_pph23_credit_verification(row.verification, row.obligation)
    payable = expense - credits
    if payable <= ZERO or (
        workpaper and payable != Decimal(str(workpaper.result["balance_due"]))
    ):
        raise ValidationError(
            "Pengakuan pembayaran ini mendukung saldo kurang bayar positif. Kasus nihil/lebih bayar dan restitusi memerlukan alur tersendiri."
        )
    entries = [("TAX_EXPENSE", expense, ZERO), ("OWN_TAX_PAYABLE", ZERO, payable)]
    if credits:
        entries.append(("TAX_RECEIVABLE", ZERO, credits))
    journal = _post(
        organization,
        date,
        f"own-tax:{reference}",
        "Pengakuan pajak penghasilan berdasarkan kertas kerja disetujui",
        entries,
    )
    row = _event_row(
        OwnTaxRecognition,
        organization=organization,
        obligation=obligation,
        workpaper=workpaper,
        corporate_year=year,
        final_period=obligation.period if obligation is not None else None,
        amount=payable,
        expense_amount=expense,
        credits_amount=credits,
        instruction_evidence=instruction_evidence,
        professional_instruction=professional_instruction,
        journal=journal,
        date=date,
        reference=reference,
        evidence=evidence,
    )
    for credit in applications:
        _event_row(
            OwnTaxCreditApplication,
            organization=organization,
            recognition=row,
            settlement=credit,
            amount=credit.tax_amount,
            journal=journal,
            date=date,
            reference=f"tax-credit:{row.pk}:{credit.pk}",
            evidence=evidence,
        )
    record(
        organization,
        actor,
        "finance.own_tax.recognized",
        obj=row,
        detail={
            "obligation_id": getattr(obligation, "pk", None),
            "workpaper_id": getattr(workpaper, "pk", None),
            "expense": str(expense),
            "credits": str(credits),
            "payable": str(payable),
        },
    )
    return row


@transaction.atomic
def reconcile_own_tax_payment(
    *, organization, actor, recognition, transaction, amount, reference, evidence
):
    from .services import (
        _amount,
        _bank_post,
        _event_row,
        _lock_org,
        _review_text,
        _scoped,
        _write_actor,
    )

    organization = _lock_org(organization)
    _write_actor(organization, actor)
    recognition = _scoped(OwnTaxRecognition, recognition, organization, lock=True)
    bank_line = _scoped(BankTransaction, transaction, organization, lock=True)
    amount = _amount(amount)
    reference = _review_text(reference, "Referensi", limit=120)
    evidence = _review_text(evidence, "Bukti")
    old = OwnTaxPayment.objects.filter(
        organization=organization, bank_posting__reference=reference
    ).first()
    if old:
        if (
            old.recognition_id,
            old.bank_posting.transaction_id,
            old.amount,
            old.evidence,
        ) != (recognition.pk, bank_line.pk, amount, evidence):
            raise ValidationError(
                "Referensi pembayaran sudah dipakai untuk catatan berbeda."
            )
        return old
    if bank_line.date < recognition.date or amount > recognition.remaining_amount:
        raise ValidationError(
            "Pembayaran harus sesudah pengakuan dan tidak melebihi sisa utang pajak perusahaan."
        )
    posting = _bank_post(
        organization,
        bank_line,
        -amount,
        "own_tax_payment",
        reference,
        evidence,
        [("OWN_TAX_PAYABLE", amount, ZERO), ("BANK", ZERO, amount)],
        actor,
    )
    row = _event_row(
        OwnTaxPayment,
        organization=organization,
        recognition=recognition,
        bank_posting=posting,
        amount=amount,
        journal=posting.journal,
        date=posting.date,
        reference=reference,
        evidence=evidence,
    )
    record(
        organization,
        actor,
        "finance.own_tax.payment_reconciled",
        obj=row,
        detail={
            "recognition_id": recognition.pk,
            "amount": str(amount),
            "official_evidence_verified": False,
        },
    )
    return row
