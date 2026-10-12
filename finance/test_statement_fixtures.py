"""Synthetic bank-source attestation through the same public intake as Finance."""

import csv
import hashlib
import io
from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from webapp.models import StatementImport
from .models import BankTransaction
from .statement_services import preview_statement_period, review_statement_period


def reviewed_statement(
    test,
    *,
    organization,
    actor,
    account,
    start,
    end,
    opening="0",
    closing="0",
    total_in="0",
    total_out="0",
    count=0,
):
    """Controls are explicit expected facts, not inferred from the ledger under test."""
    if not hasattr(test, "_statement_fixture_storage"):
        test._statement_fixture_storage = TemporaryDirectory(
            prefix="synthetic-statement-controls-"
        )
        test.addCleanup(test._statement_fixture_storage.cleanup)
        settings = override_settings(MEDIA_ROOT=test._statement_fixture_storage.name)
        settings.enable()
        test.addCleanup(settings.disable)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Synthetic independent period controls",
            str(start),
            str(end),
            opening,
            closing,
            total_in,
            total_out,
            count,
        ]
    )
    writer.writerow(["date", "reference", "description", "amount"])
    transactions = list(
        BankTransaction.objects.filter(
            organization=organization, account=account, date__range=(start, end)
        ).order_by("date", "pk")
    )
    for row in transactions:
        writer.writerow(
            [str(row.date), row.reference, row.description, str(row.amount)]
        )
    content = output.getvalue().encode()
    with override_settings(MFA_REQUIRED=False):
        client = Client()
        client.force_login(actor)
        session = client.session
        session["organization_id"] = organization.pk
        session.save()
        result = client.post(
            reverse("bank_import"),
            {
                "account": account.pk,
                "file": SimpleUploadedFile(f"synthetic-{start}-{end}.csv", content),
            },
        )
        test.assertEqual(result.status_code, 302)
        source = StatementImport.objects.get(
            organization=organization,
            account=account,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        mapping = {
            "date": "0",
            "reference": "1",
            "description": "2",
            "amount": "3",
            "number_format": "en",
            "reference_mode": "bank_reference",
            "first_row": 1 if transactions else "",
            "last_row": len(transactions) if transactions else "",
        }
        if not transactions:
            mapping["no_transactions"] = "on"
        preview = client.post(reverse("statement_preview", args=[source.pk]), mapping)
        token = preview.context["token"]
        test.assertTrue(token, preview.context["form"].errors)
        result = client.post(
            reverse("statement_preview", args=[source.pk]),
            {"action": "confirm", "preview_token": token, "confirmed": "yes"},
        )
        test.assertEqual(result.status_code, 302)
    facts = {
        "organization": organization,
        "actor": actor,
        "statement": source,
        "period_start": start,
        "period_end": end,
        "opening_balance": opening,
        "closing_balance": closing,
        "total_in": total_in,
        "total_out": total_out,
        "transaction_count": count,
        "source_order": "ascending",
        "evidence": "Synthetic controls stated independently in the test fixture source",
    }
    preview = preview_statement_period(**facts)
    test.assertEqual(preview["issues"], [])
    return review_statement_period(
        **facts, expected_fingerprint=preview["fingerprint"], confirmed=True
    )
