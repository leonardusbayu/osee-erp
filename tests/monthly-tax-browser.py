"""Isolated monthly workflow browser proof; no company DB or providers."""

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
output_root = ROOT / ".local/monthly-tax-browser"
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
from finance.models import BankAccount, BankTransaction, Invoice, Party, Product
from finance import services as finance
from taxes.models import MonthlyTaxSourcePackage, TaxObligation
from taxes.monthly_sources import build_monthly_sources
from taxes.monthly_source_workflow import package_status, prepare_monthly_package
from taxes.tests_monthly_packages import MonthlySourcePackageTests
from wsgiref.simple_server import make_server, WSGIRequestHandler

call_command("migrate", verbosity=0)


def fixture(prefix, regime, sale=False):
    helper = MonthlySourcePackageTests()
    helper.org = Organization.objects.create(name=f"Synthetic {prefix} monthly browser")
    credentials = {}
    for role in ("owner", "finance", "marketing"):
        password = secrets.token_urlsafe(24)
        actor = get_user_model().objects.create_user(
            username=f"{prefix}-{role}", password=password
        )
        Membership.objects.create(organization=helper.org, user=actor, role=role)
        credentials[role] = {"username": actor.username, "password": password}
        setattr(helper, role, actor)
    helper.profile(regime)
    proof = helper.evidence()
    result = {
        "organization": helper.org.pk,
        "credentials": credentials,
        "evidence": proof.pk,
    }
    if sale:
        party = Party.objects.create(
            organization=helper.org, name="Synthetic reseller", kind="reseller"
        )
        product = Product.objects.create(
            organization=helper.org,
            code="ITP",
            name="Synthetic ITP",
            kind="itp",
            default_price="500000",
        )
        account = BankAccount.objects.create(
            organization=helper.org, name="Synthetic bank", account_number=prefix
        )
        invoice = finance.create_invoice(
            organization=helper.org,
            actor=helper.owner,
            party=party,
            product=product,
            quantity=1,
            date=date(2026, 9, 10),
            due_date=date(2026, 9, 10),
            service_date=date(2026, 9, 10),
        )
        finance.issue_invoice(
            organization=helper.org, actor=helper.owner, invoice=invoice
        )
        finance.import_bank_csv(
            organization=helper.org,
            actor=helper.owner,
            account=account,
            content="date,reference,description,amount\n2026-09-10,BROWSER-SOURCE,Synthetic,500000\n",
        )
        finance.reconcile_receipt(
            organization=helper.org,
            actor=helper.owner,
            invoice=invoice,
            transaction=BankTransaction.objects.get(organization=helper.org),
            amount="500000",
        )
        finance.record_delivery(
            organization=helper.org,
            actor=helper.owner,
            invoice=invoice,
            date=date(2026, 9, 10),
            evidence="Synthetic attendance for monthly browser",
        )
        facts = build_monthly_sources(helper.org, "2026-09")
        candidate = next(row for row in facts["rows"] if row.get("proposed_inclusion"))
        result.update(
            invoice=invoice.pk,
            party=party.pk,
            product=product.pk,
            rows=facts["rows"],
            candidate=candidate["row_id"],
        )
    return helper, result


fixtures, helpers = {}, {}
for width in (1440, 390):
    final_helper, final_data = fixture(f"final-{width}", "final", sale=True)
    normal_helper, normal_data = fixture(f"normal-{width}", "normal")
    correction_helper, correction_data = fixture(
        f"correction-{width}", "final", sale=True
    )
    fixtures[str(width)] = {
        "final": final_data,
        "normal": normal_data,
        "correction": correction_data,
    }
    helpers[width] = (final_helper, normal_helper, correction_helper)
foreign, foreign_data = fixture("foreign", "normal")
foreign_package = prepare_monthly_package(
    organization=foreign.org, actor=foreign.finance, period="2026-09"
)
(OUT / "fixture.json").write_text(
    json.dumps({"fixtures": fixtures, "foreign_package": foreign_package.pk}),
    encoding="utf-8",
)


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


# Exercise production error rendering, rather than DEBUG's technical URL list.
# Database/provider/transport settings remain those of this isolated local fixture.
error_rendering = override_settings(DEBUG=False)
error_rendering.enable()
server = make_server(
    "127.0.0.1", 0, StaticFilesHandler(get_wsgi_application()), handler_class=Quiet
)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
env = os.environ.copy()
env.update(
    OSEE_QA_URL=f"http://127.0.0.1:{server.server_port}",
    OSEE_QA_DIR=str(OUT),
    OSEE_PLAYWRIGHT_PATH=args.playwright,
    OSEE_CHROME_PATH=args.chrome,
)
try:
    result = subprocess.run(
        [args.node, str(Path(__file__).with_suffix(".cjs"))], env=env, timeout=300
    )
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    error_rendering.disable()

verified = []
if result.returncode == 0:
    for width, (final_helper, normal_helper, correction_helper) in helpers.items():
        first = MonthlyTaxSourcePackage.objects.get(
            organization=final_helper.org, period=date(2026, 9, 1), revision=1
        )
        second = MonthlyTaxSourcePackage.objects.get(
            organization=final_helper.org, period=date(2026, 9, 1), revision=2
        )
        approval = first.approval
        obligation = TaxObligation.objects.get(
            organization=final_helper.org, source_package=first
        )
        assert Decimal(approval.result["taxable_turnover"]) == Decimal(540000)
        assert obligation.base == Decimal(540000) and obligation.amount == Decimal(2700)
        assert (
            obligation.source_package_id == first.pk and obligation.status == "review"
        )
        assert (
            first.package_hash != second.package_hash and second.previous_id == first.pk
        )
        assert package_status(organization=final_helper.org, package=first)["stale"]
        assert not package_status(organization=final_helper.org, package=second)[
            "stale"
        ]
        assert obligation.source_evidence_id == approval.external_review.evidence_id
        assert len(approval.decision["adjustments"]) == 1 and Decimal(
            approval.decision["adjustments"][0]["amount"]
        ) == Decimal(40000)
        normal = MonthlyTaxSourcePackage.objects.get(
            organization=normal_helper.org, period=date(2026, 9, 1)
        )
        assert normal.approval.result["taxable_turnover"] is None
        assert not normal.obligations.exists()
        nil = MonthlyTaxSourcePackage.objects.get(
            organization=final_helper.org, period=date(2026, 8, 1)
        )
        assert Decimal(nil.approval.result["taxable_turnover"]) == 0
        assert final_helper.org.pk != foreign.org.pk
        assert (
            Invoice.objects.filter(
                organization=final_helper.org, date=date(2026, 9, 20), status="issued"
            ).count()
            == 1
        )
        old_review = MonthlyTaxSourcePackage.objects.get(
            organization=correction_helper.org, period=date(2026, 9, 1), revision=1
        )
        corrected = MonthlyTaxSourcePackage.objects.get(
            organization=correction_helper.org, period=date(2026, 9, 1), revision=2
        )
        assert (
            corrected.previous_id == old_review.pk
            and corrected.snapshot == old_review.snapshot
        )
        assert (
            corrected.source_hash == old_review.source_hash
            and corrected.package_hash == old_review.package_hash
        )
        assert old_review.approval.result["taxable_turnover"] == "500000"
        assert corrected.approval.result["taxable_turnover"] == "540000"
        assert package_status(organization=correction_helper.org, package=old_review)[
            "superseded"
        ]
        assert (
            "Synthetic browser corrected gross contract classification"
            in package_status(organization=correction_helper.org, package=corrected)[
                "revision_reason"
            ]
        )
        old_draft = TaxObligation.objects.get(source_package=old_review)
        new_draft = TaxObligation.objects.get(source_package=corrected)
        assert old_draft.status == "withdrawn" and old_draft.base == Decimal(500000)
        assert (
            new_draft.status == "review"
            and new_draft.base == Decimal(540000)
            and new_draft.amount == Decimal(2700)
        )
        assert (
            new_draft.source_evidence_id
            == corrected.approval.external_review.evidence_id
        )
        assert (
            TaxObligation.objects.filter(organization=correction_helper.org)
            .exclude(status="withdrawn")
            .count()
            == 1
        )
        verified.append(
            {
                "width": width,
                "approved_package": first.pk,
                "source_hash": first.source_hash,
                "draft": obligation.pk,
                "base": str(obligation.base),
                "tax": str(obligation.amount),
                "new_revision": second.revision,
                "corrected_package": corrected.pk,
                "withdrawn_draft": old_draft.pk,
                "replacement_draft": new_draft.pk,
            }
        )
metadata = {
    "company_data_used": False,
    "provider_calls": False,
    "persisted_state_verified": result.returncode == 0,
    "verified": verified,
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
