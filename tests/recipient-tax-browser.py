"""Persisted recipient workflows in private synthetic databases; no providers."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading

parser = argparse.ArgumentParser()
parser.add_argument("--node", required=True)
parser.add_argument("--playwright", required=True)
parser.add_argument(
    "--chrome", default="C:/Program Files/Google/Chrome/Application/chrome.exe"
)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[1]
output_root = ROOT / ".local/recipient-tax-browser"
output_root.mkdir(parents=True, exist_ok=True)
OUT = Path(tempfile.mkdtemp(prefix="run-", dir=output_root))
for key in list(os.environ):
    if key.startswith(
        (
            "POSTGRES_",
            "DJANGO_",
            "OPENROUTER_",
            "DIRECTOR_OPENROUTER_",
            "DIRECTOR_AI_",
            "MARKETING_AI_",
            "OTP_ENCRYPTION_",
        )
    ):
        os.environ.pop(key)
os.environ.update(
    DJANGO_READ_DOT_ENV="0",
    DJANGO_DEBUG="1",
    DJANGO_SECRET_KEY=secrets.token_urlsafe(60),
    DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1,testserver",
    OSEE_DEMO_MODE="0",
    OSEE_LOCAL_SETUP="0",
    OSEE_MFA_REQUIRED="0",
    PYTHONIOENCODING="utf-8",
    OPENROUTER_API_KEY="",
    DIRECTOR_OPENROUTER_API_KEY="",
    MARKETING_OPENROUTER_API_KEY="",
    OPENROUTER_PRIVATE_CONTEXT_ENABLED="0",
    DIRECTOR_AI_PRIVATE_AGGREGATES_ENABLED="0",
    MARKETING_AI_PRIVATE_AGGREGATES_ENABLED="0",
    DJANGO_SQLITE_PATH=str(OUT / "browser.sqlite3"),
    DJANGO_MEDIA_ROOT=str(OUT / "media"),
    DJANGO_SETTINGS_MODULE="config.settings",
)
assert not Path(os.environ["DJANGO_SQLITE_PATH"]).exists()
sys.path.insert(0, str(ROOT))


def source_identity():
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
        )
        .decode()
        .split("\0")
    )
    files = [
        (
            name,
            hashlib.sha256(
                (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest(),
        )
        for name in sorted(set(paths))
        if name
        and not name.startswith((".codex-remote-attachments/", "docs/"))
        and (ROOT / name).is_file()
    ]
    return hashlib.sha256(json.dumps(files, separators=(",", ":")).encode()).hexdigest()


before = source_identity()
import django

django.setup()
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.wsgi import get_wsgi_application
from django.test import override_settings
from datetime import date
from decimal import Decimal
from core.models import Membership, Organization
from finance.models import (
    BankAccount,
    BankTransaction,
    BankPosting,
    CustomerTaxSettlement,
    Invoice,
    Journal,
    Party,
    Product,
)
from finance import services as finance
from finance.tests_tax_money import TaxMoneyFixtures
from taxes.models import (
    RecipientTaxIdentity,
    RecipientIdentityApproval,
    TaxObligation,
    TaxVerification,
    BillTaxDecision,
)
from taxes.recipient_identity import resolve_recipient_identity
from taxes.knowledge import publish_catalog
from taxes.services import SOURCE_CATALOG
from taxes.tests_workflow import TaxReleaseWorkflowTests
from taxes.workflow import digest, subject_snapshot
from wsgiref.simple_server import make_server, WSGIRequestHandler

call_command("migrate", verbosity=0)
publish_catalog(
    SOURCE_CATALOG,
    date(2026, 10, 9),
    reviewed_by="Synthetic browser source reviewer",
    review_reference="Synthetic private source publication only",
)


def fixture(prefix, *, final=False):
    helper = TaxMoneyFixtures() if final else TaxReleaseWorkflowTests()
    helper.org = Organization.objects.create(
        name=f"Synthetic {prefix} recipient browser"
    )
    credentials = {}
    for role in ("owner", "finance", "marketing", "auditor", "reviewer"):
        password = secrets.token_urlsafe(24)
        actor = get_user_model().objects.create_user(
            username=f"{prefix}-{role}", password=password
        )
        Membership.objects.create(organization=helper.org, user=actor, role=role)
        credentials[role] = {"username": actor.username, "password": password}
        if role == "owner":
            helper.owner = helper.actor = actor
    proof = helper.evidence()
    result = {
        "organization": helper.org.pk,
        "company_name": helper.org.name,
        "credentials": credentials,
        "evidence": proof.pk,
    }
    if final:
        helper.final_profile()
        return result
    supplier = Party.objects.create(
        organization=helper.org,
        name="Synthetic supplier without NPWP",
        kind="supplier",
        tax_id="UNREVIEWED-FREE-PARTY-ID",
    )
    pending = Party.objects.create(
        organization=helper.org, name="Synthetic unresolved supplier", kind="supplier"
    )
    customer = Party.objects.create(
        organization=helper.org,
        name="Synthetic customer withholding OSEE",
        kind="customer",
    )
    product = Product.objects.create(
        organization=helper.org,
        code="ITP",
        name="Synthetic ITP",
        kind="itp",
        default_price="500000",
    )
    invoice = finance.create_invoice(
        organization=helper.org,
        actor=helper.owner,
        party=customer,
        product=product,
        quantity=1,
        date=date(2026, 9, 10),
    )
    finance.issue_invoice(organization=helper.org, actor=helper.owner, invoice=invoice)
    account = BankAccount.objects.create(
        organization=helper.org, name="Synthetic BNI", account_number=prefix
    )
    finance.import_bank_csv(
        organization=helper.org,
        actor=helper.owner,
        account=account,
        content="date,reference,description,amount\n2026-09-10,SYN-RECIPIENT-NET,Synthetic net customer receipt,490000\n",
    )
    bank = BankTransaction.objects.get(
        organization=helper.org, account=account, reference="SYN-RECIPIENT-NET"
    )
    bills = []
    for party, reference, amount in (
        (supplier, "SYN-POSITIVE-BILL", "500000"),
        (pending, "SYN-NO-WITHHOLD-BILL", "250000"),
    ):
        bill = finance.create_bill(
            organization=helper.org,
            actor=helper.owner,
            supplier=party,
            amount=amount,
            date=date(2026, 9, 10),
            service_date=date(2026, 9, 10),
            supplier_reference=reference,
        )
        finance.approve_bill(organization=helper.org, actor=helper.owner, bill=bill)
        bills.append(bill.pk)
    result.update(
        supplier=supplier.pk,
        supplier_name=supplier.name,
        pending=pending.pk,
        pending_name=pending.name,
        customer=customer.pk,
        customer_name=customer.name,
        invoice=invoice.pk,
        net_bank=bank.pk,
        positive_bill=bills[0],
        nil_bill=bills[1],
    )
    (OUT / f"identity-{prefix}.pdf").write_bytes(bytes(proof.content))
    result["pdf_path"] = str(OUT / f"identity-{prefix}.pdf")
    return result


fixtures = {
    str(width): {
        "normal": fixture(f"recipient-{width}"),
        "final": fixture(f"final-{width}", final=True),
    }
    for width in (1440, 390)
}
(OUT / "fixture.json").write_text(json.dumps({"fixtures": fixtures}), encoding="utf-8")


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


with override_settings(DEBUG=False):
    server = make_server(
        "127.0.0.1", 0, StaticFilesHandler(get_wsgi_application()), handler_class=Quiet
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    browser_env = os.environ.copy()
    browser_env.update(
        OSEE_QA_URL=f"http://127.0.0.1:{server.server_port}",
        OSEE_QA_DIR=str(OUT),
        OSEE_PLAYWRIGHT_PATH=args.playwright,
        OSEE_CHROME_PATH=args.chrome,
    )
    try:
        result = subprocess.run(
            [args.node, str(Path(__file__).with_suffix(".cjs"))],
            env=browser_env,
            timeout=300,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

persisted = []
if result.returncode == 0:
    for width, fixture_data in fixtures.items():
        normal, final = fixture_data["normal"], fixture_data["final"]
        organization = Organization.objects.get(pk=normal["organization"])
        identities = RecipientTaxIdentity.objects.filter(organization=organization)
        assert identities.count() == 4
        assert (
            RecipientIdentityApproval.objects.filter(organization=organization).count()
            == 4
        )
        own = identities.get(recipient_party__isnull=True)
        supplier = identities.get(recipient_party_id=normal["supplier"])
        assert own.approval.factor == 1 and supplier.approval.factor == 2
        revisions = list(
            identities.filter(recipient_party_id=normal["pending"]).order_by("revision")
        )
        assert len(revisions) == 2 and revisions[1].previous_id == revisions[0].pk
        assert (
            revisions[0].effective_from
            == revisions[1].effective_from
            == date(2025, 5, 22)
        )
        assert (
            revisions[0].approval.factor is None and revisions[1].approval.factor == 1
        )
        assert (
            resolve_recipient_identity(
                organization=organization,
                recipient_party=normal["pending"],
                tax_point_date=date(2026, 9, 10),
            ).pk
            == revisions[1].pk
        )
        payable = TaxObligation.objects.get(
            organization=organization, direction="payable", source_bill__isnull=True
        )
        customer = TaxObligation.objects.get(
            organization=organization, direction="receivable"
        )
        assert payable.base == customer.base == Decimal(500000)
        assert (payable.rate, payable.amount) == (Decimal("0.04"), Decimal(20000))
        assert (customer.rate, customer.amount) == (Decimal("0.02"), Decimal(10000))
        assert (
            payable.recipient_identity_id == supplier.pk
            and customer.recipient_identity_id == own.pk
        )
        assert (
            customer.source_invoice_id == normal["invoice"]
            and customer.withholder_party_id == normal["customer"]
        )
        assert payable.external_review.subject_digest == digest(
            {
                "subject": subject_snapshot(payable),
                "decision": {"action": "approve_obligation"},
            }
        )
        assert supplier.approval.identity_fingerprint == supplier.fingerprint
        proof = TaxVerification.objects.get(obligation=customer)
        assert proof.certificate_snapshot["recipient_reference"] == own.number
        assert proof.certificate_snapshot["withholder_reference"] == "3234567890123456"
        assert proof.certificate_snapshot["tax_point_date"] == "2026-09-10"
        settlement = CustomerTaxSettlement.objects.get(organization=organization)
        invoice = Invoice.objects.get(pk=normal["invoice"])
        assert (
            settlement.cash_amount,
            settlement.tax_amount,
            settlement.gross_amount,
        ) == (Decimal(490000), Decimal(10000), Decimal(500000))
        assert (
            settlement.verification_id == proof.pk
            and settlement.bank_posting.transaction_id == normal["net_bank"]
        )
        assert (
            invoice.paid_amount == Decimal(500000) and invoice.outstanding_amount == 0
        )
        assert finance.report_balances(organization=organization)["BANK"] == Decimal(
            490000
        )
        assert (
            BankPosting.objects.filter(
                organization=organization, transaction_id=normal["net_bank"]
            ).count()
            == 1
        )
        assert (
            Journal.objects.filter(
                organization=organization, pk=settlement.journal_id
            ).count()
            == 1
        )
        bank_entries = list(
            settlement.journal.lines.filter(account="BANK").values_list(
                "debit", "credit"
            )
        )
        assert bank_entries == [(Decimal(490000), Decimal(0))]
        positive = BillTaxDecision.objects.get(bill_id=normal["positive_bill"])
        assert (
            positive.obligation.recipient_identity_id == supplier.pk
            and positive.amount == Decimal(20000)
        )
        assert BillTaxDecision.objects.get(bill_id=normal["nil_bill"]).amount == 0
        final_row = TaxObligation.objects.get(organization_id=final["organization"])
        assert (
            final_row.tax_type == "final_turnover"
            and final_row.amount == Decimal(5000)
            and final_row.recipient_identity_id is None
        )
        persisted.append(
            {
                "width": width,
                "identity_reports": 4,
                "same_date_unresolved_recovery": True,
                "payable_amount": str(payable.amount),
                "customer_credit": str(customer.amount),
                "final_amount": str(final_row.amount),
                "exact_identity_report_and_certificate_bindings": True,
                "net_bank_cash": "490000",
                "invoice_settled": "500000",
                "tax_credit": "10000",
                "single_settlement_and_bank_journal_after_retry": True,
            }
        )
metadata = {
    "company_data_used": False,
    "provider_http_used": False,
    "source_before": before,
    "source_after": source_identity(),
    "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    "browser_script_sha256": hashlib.sha256(
        Path(__file__).with_suffix(".cjs").read_bytes()
    ).hexdigest(),
    "persisted": persisted,
    "exit_code": result.returncode,
    "evidence": str(OUT),
}
(OUT / "database-verified.json").write_text(
    json.dumps(metadata, indent=2), encoding="utf-8"
)
print(json.dumps(metadata))
raise SystemExit(result.returncode)
