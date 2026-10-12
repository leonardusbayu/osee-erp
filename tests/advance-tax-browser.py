"""Private browser acceptance for advance-backed late tax credits; no providers."""

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
output_root = ROOT / ".local/advance-tax-browser"
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
from datetime import date
from decimal import Decimal
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.wsgi import get_wsgi_application
from django.test import override_settings
from core.models import Membership, Organization
from finance.models import (
    AccountingPeriod,
    AdvanceApplication,
    BankAccount,
    BankPosting,
    BankTransaction,
    CustomerAdvance,
    CustomerTaxSettlement,
    Invoice,
    Journal,
    Party,
    Product,
)
from finance import services as finance
from finance.tests_tax_money import TaxMoneyFixtures
from taxes import workflow as tax
from taxes.knowledge import publish_catalog
from taxes.services import SOURCE_CATALOG
from wsgiref.simple_server import make_server, WSGIRequestHandler

call_command("migrate", verbosity=0)
publish_catalog(
    SOURCE_CATALOG,
    date(2026, 10, 9),
    reviewed_by="Synthetic browser reviewer",
    review_reference="Synthetic private source publication only",
)


def fixture(prefix, *, proof=True):
    helper = TaxMoneyFixtures()
    helper.org = Organization.objects.create(
        name=f"Synthetic {prefix} advance tax browser"
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
    helper.party = Party.objects.create(
        organization=helper.org,
        name="Synthetic customer using advance",
        kind="customer",
    )
    helper.product = Product.objects.create(
        organization=helper.org,
        code="ITP",
        name="Synthetic ITP",
        kind="itp",
        default_price="500000",
    )
    helper.account = BankAccount.objects.create(
        organization=helper.org, name="Synthetic BNI", account_number=prefix
    )
    helper.day = date(2026, 9, 10)
    bank = helper.movement(
        "490000", day=date(2026, 9, 1), reference="SYN-ORIGINAL-ADVANCE-CASH"
    )
    invoice = finance.issue_invoice(
        organization=helper.org, actor=helper.actor, invoice=helper.invoice()
    )
    context = helper.pph23_context(
        direction="receivable", tax_point_date=date(2026, 9, 1), invoice=invoice
    )
    context["tax_point_basis"] = "paid"
    obligation = tax.prepare_obligation(
        organization=helper.org,
        actor=helper.actor,
        period="2026-09",
        rule_code="pph23_service_2",
        base="500000",
        direction="receivable",
        source_invoice=invoice,
        source_reference=invoice.number,
        source_evidence=helper.evidence(),
        rule_reference="Synthetic received decision for exact original advance date",
        object_code="24-104-99",
        note="Synthetic reviewed tax point; no automatic advance classification",
        **context,
    )
    tax.approve_tax_obligation(
        obligation,
        helper.actor,
        external_review=helper.external(obligation, {"action": "approve_obligation"}),
    )
    if proof:
        helper.credit(obligation)
    return {
        "organization": helper.org.pk,
        "credentials": credentials,
        "party": helper.party.pk,
        "bank": bank.pk,
        "invoice": invoice.pk,
        "invoice_number": invoice.number,
        "obligation": obligation.pk,
    }


fixtures = {
    str(width): {
        "normal": fixture(f"advance-{width}"),
        "missing": fixture(f"missing-{width}", proof=False),
    }
    for width in (1440, 390)
}
foreign = fixture("foreign")


def save_fixtures():
    (OUT / "fixture.json").write_text(
        json.dumps({"fixtures": fixtures, "foreign": foreign}), encoding="utf-8"
    )


def lines(journal):
    return [
        (account, str(debit), str(credit))
        for account, debit, credit in journal.lines.order_by("pk").values_list(
            "account", "debit", "credit"
        )
    ]


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


save_fixtures()
result_code = 0
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
        for stage in ("prepare", "credit"):
            browser_env["OSEE_QA_STAGE"] = stage
            result = subprocess.run(
                [args.node, str(Path(__file__).with_suffix(".cjs"))],
                env=browser_env,
                timeout=240,
            )
            result_code = result.returncode
            if result_code:
                break
            if stage == "prepare":
                for data in fixtures.values():
                    f = data["normal"]
                    application = AdvanceApplication.objects.get(
                        organization_id=f["organization"]
                    )
                    advance = application.advance
                    f.update(
                        application=application.pk,
                        advance=advance.pk,
                        original_journals={
                            str(item.pk): lines(item)
                            for item in (advance.journal, application.journal)
                        },
                        journal_count=Journal.objects.filter(
                            organization_id=f["organization"]
                        ).count(),
                    )
                    # Private fixture closes the original month only after the
                    # public UI created the original bank/application journals.
                    AccountingPeriod.objects.update_or_create(
                        organization_id=f["organization"],
                        year=2026,
                        month=9,
                        defaults={"closed": True},
                    )
                save_fixtures()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

persisted = []
if result_code == 0:
    for width, data in fixtures.items():
        f = data["normal"]
        org = Organization.objects.get(pk=f["organization"])
        application = AdvanceApplication.objects.get(pk=f["application"])
        advance = CustomerAdvance.objects.get(pk=f["advance"])
        invoice = Invoice.objects.get(pk=f["invoice"])
        row = CustomerTaxSettlement.objects.get(organization=org)
        assert (
            row.source_advance_application_id == application.pk
            and row.source_allocation_id is None
            and row.bank_posting_id is None
        )
        assert (row.cash_amount, row.tax_amount, row.gross_amount) == (
            Decimal(0),
            Decimal(10000),
            Decimal(10000),
        )
        assert (
            row.date == date(2026, 10, 1)
            and row.evidence
            == "Synthetic exact applied advance and verified certificate"
        )
        assert (invoice.paid_amount, invoice.outstanding_amount) == (
            Decimal(500000),
            Decimal(0),
        )
        assert advance.remaining_amount == 0 and application.amount == Decimal(490000)
        assert finance.report_balances(organization=org)["BANK"] == Decimal(490000)
        assert (
            BankTransaction.objects.filter(organization=org).count()
            == BankPosting.objects.filter(organization=org).count()
            == 1
        )
        assert (
            Journal.objects.filter(organization=org).count() == f["journal_count"] + 1
        )
        assert lines(row.journal) == [
            ("TAX_RECEIVABLE", "10000.00", "0.00"),
            ("AR", "0.00", "10000.00"),
        ]
        assert row.verification.obligation_id == f["obligation"]
        for pk, original in f["original_journals"].items():
            assert lines(Journal.objects.get(pk=pk)) == [
                tuple(item) for item in original
            ]
        assert (
            application.advance.bank_posting.transaction.date
            < invoice.date
            == application.date
        )
        assert AccountingPeriod.objects.get(organization=org, year=2026, month=9).closed
        assert not CustomerTaxSettlement.objects.filter(
            organization_id=data["missing"]["organization"]
        ).exists()
        persisted.append(
            {
                "width": width,
                "original_cash": "490000",
                "applied_advance": "490000",
                "new_cash": "0",
                "new_credit": "10000",
                "invoice_settled": "500000",
                "original_journals_unchanged": True,
                "one_credit_after_retry": True,
                "old_month_remains_closed": True,
                "exact_certificate_and_application": True,
            }
        )
browser_results = {"flows": [], "pages": [], "errors": []}
for stage in ("prepare", "credit"):
    path = OUT / f"browser-{stage}.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in browser_results:
            browser_results[key].extend(data[key])
(OUT / "browser-results.json").write_text(
    json.dumps(browser_results, indent=2), encoding="utf-8"
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
    "exit_code": result_code,
    "evidence": str(OUT),
}
(OUT / "database-verified.json").write_text(
    json.dumps(metadata, indent=2), encoding="utf-8"
)
print(json.dumps(metadata))
raise SystemExit(result_code)
