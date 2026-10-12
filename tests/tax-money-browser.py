"""Private synthetic DB and ephemeral browser server; never company data."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--node", required=True)
parser.add_argument("--playwright", required=True)
parser.add_argument(
    "--chrome", default="C:/Program Files/Google/Chrome/Application/chrome.exe"
)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[1]
output_root = ROOT / ".local/tax-money-browser"
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
)
sys.path.insert(0, str(ROOT))
os.environ["DJANGO_SQLITE_PATH"] = str(OUT / "browser.sqlite3")
os.environ["DJANGO_MEDIA_ROOT"] = str(OUT / "media")
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
assert not Path(os.environ["DJANGO_SQLITE_PATH"]).exists(), (
    "Use a fresh private QA directory"
)


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
from datetime import date
from decimal import Decimal
from core.models import Membership, Organization
from finance.models import BankAccount, Party, Product, OwnTaxRecognition, OwnTaxPayment
from finance import services as finance
from finance.tests_tax_money import TaxMoneyFixtures
from wsgiref.simple_server import make_server, WSGIRequestHandler

call_command("migrate", verbosity=0)


def fixture(prefix):
    helper = TaxMoneyFixtures()
    helper.org = Organization.objects.create(name=f"Synthetic {prefix} tax browser")
    credentials = {}
    for role in ("owner", "finance", "marketing"):
        password = secrets.token_urlsafe(24)
        actor = get_user_model().objects.create_user(
            username=f"{prefix}-{role}", password=password
        )
        Membership.objects.create(organization=helper.org, user=actor, role=role)
        credentials[role] = {"username": actor.username, "password": password}
        if role == "owner":
            helper.actor = helper.owner = actor
    helper.party = Party.objects.create(
        organization=helper.org, name="Synthetic mitra", kind="reseller"
    )
    helper.product = Product.objects.create(
        organization=helper.org,
        code="ITP",
        name="Synthetic ITP",
        kind="itp",
        default_price=Decimal(500000),
    )
    helper.account = BankAccount.objects.create(
        organization=helper.org, name="Synthetic BNI", account_number=prefix
    )
    helper.day = date(2026, 9, 10)
    return helper, credentials


normal, normal_credentials = fixture("net")
normal.profile()
invoice = finance.issue_invoice(
    organization=normal.org, actor=normal.actor, invoice=normal.invoice()
)
credit = normal.approved(invoice=invoice)
normal.credit(credit)
net_bank = normal.movement("490000", reference="BROWSER-NET")
late_invoice = finance.issue_invoice(
    organization=normal.org, actor=normal.actor, invoice=normal.invoice()
)
late_obligation = normal.approved(invoice=late_invoice)
late_bank = normal.movement("490000", reference="BROWSER-OLD-CASH")
late_allocation = finance.reconcile_receipt(
    organization=normal.org,
    actor=normal.actor,
    invoice=late_invoice,
    transaction=late_bank,
    amount="490000",
)
normal.credit(late_obligation)
original_cash_journal = finance.Journal.objects.get(
    organization=normal.org, source_key=f"allocation:{late_allocation.pk}"
)
original_cash_lines = list(
    original_cash_journal.lines.order_by("pk").values_list("account", "debit", "credit")
)
final, final_credentials = fixture("final")
final_obligation = final.final_obligation()
first_bank = final.movement("-2000", reference="BROWSER-TAX-PART1")
second_bank = final.movement("-3000", reference="BROWSER-TAX-PART2")
official = final.evidence()
data = {
    "normal_credentials": normal_credentials,
    "final_credentials": final_credentials,
    "invoice": invoice.pk,
    "credit": credit.pk,
    "net_bank": net_bank.pk,
    "late_invoice": late_invoice.pk,
    "late_credit": late_obligation.pk,
    "late_allocation": late_allocation.pk,
    "final_obligation": final_obligation.pk,
    "first_bank": first_bank.pk,
    "second_bank": second_bank.pk,
    "official_evidence": official.pk,
    "day": str(final.day),
}
(OUT / "fixture.json").write_text(json.dumps(data), encoding="utf-8")


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


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
        timeout=150,
    )
finally:
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)
invoice.refresh_from_db()
recognized = OwnTaxRecognition.objects.get(organization=final.org)
assert invoice.paid_amount == Decimal(500000) and invoice.outstanding_amount == 0
late_invoice.refresh_from_db()
assert (
    late_invoice.paid_amount == Decimal(500000) and late_invoice.outstanding_amount == 0
)
assert finance.report_balances(organization=normal.org)["BANK"] == Decimal(980000)
assert late_bank.unallocated_amount == 0 and late_allocation.amount == Decimal(490000)
assert (
    list(
        original_cash_journal.lines.order_by("pk").values_list(
            "account", "debit", "credit"
        )
    )
    == original_cash_lines
)
assert (
    recognized.remaining_amount == 0
    and OwnTaxPayment.objects.filter(organization=final.org).count() == 2
)
assert final_obligation.verifications.filter(kind="payment").exists()
metadata = {
    "company_data_used": False,
    "invoice_settled": str(invoice.paid_amount),
    "net_cash_per_invoice": "490000",
    "combined_net_cash": "980000",
    "late_certificate_cash_added": "0",
    "late_invoice_settled": str(late_invoice.paid_amount),
    "own_tax_remaining": str(recognized.remaining_amount),
    "official_payment_verified": True,
    "source_before": before,
    "source_after": source_identity(),
    "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    "browser_script_sha256": hashlib.sha256(
        Path(__file__).with_suffix(".cjs").read_bytes()
    ).hexdigest(),
    "exit_code": result.returncode,
    "evidence": str(OUT),
}
(OUT / "database-verified.json").write_text(
    json.dumps(metadata, indent=2), encoding="utf-8"
)
print(json.dumps(metadata))
raise SystemExit(result.returncode)
