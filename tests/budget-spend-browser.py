"""Private persisted browser acceptance for budget spending; synthetic sources only."""

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
output_root = ROOT / ".local/budget-spend-browser"
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
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application
from django.test import override_settings
from django.utils import timezone
from core.models import Membership, Organization
from director import services as planning
from director.spend_models import (
    BudgetBillLink,
    BudgetPaymentMatch,
    BudgetSpend,
    BudgetSpendMovement,
)
from director.spend_services import spend_position
from finance import tests_budget_link
from finance.models import BankAccount, BankTransaction
from finance.models import Journal, Party, PrepaymentRelease
from finance.tests_tax_money import TaxMoneyFixtures
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


class Fixture(TaxMoneyFixtures):
    bill = tests_budget_link.BillBudgetLinkTests.bill
    no_withholding = tests_budget_link.BillBudgetLinkTests.no_withholding
    payment = tests_budget_link.BillBudgetLinkTests.payment


def journal_identity(org):
    return [
        {
            "id": row.pk,
            "date": str(row.date),
            "source_key": row.source_key,
            "lines": [
                (account, str(debit), str(credit))
                for account, debit, credit in row.lines.order_by("pk").values_list(
                    "account", "debit", "credit"
                )
            ],
        }
        for row in Journal.objects.filter(organization=org).order_by("pk")
    ]


def fixture(prefix, kind):
    helper = Fixture()
    helper.org = Organization.objects.create(name=f"Synthetic {prefix} spend browser")
    credentials = {}
    actors = {}
    for role in ("owner", "finance", "marketing", "auditor"):
        password = secrets.token_urlsafe(24)
        actor = get_user_model().objects.create_user(
            username=f"{prefix}-{role}", password=password
        )
        Membership.objects.create(organization=helper.org, user=actor, role=role)
        credentials[role] = {"username": actor.username, "password": password}
        actors[role] = actor
    helper.owner = helper.actor = actors["owner"]
    helper.supplier = Party.objects.create(
        organization=helper.org, name="Synthetic campaign supplier", kind="supplier"
    )
    helper.account = BankAccount.objects.create(
        organization=helper.org, name="Synthetic BNI", account_number=prefix
    )
    helper.day = date(2026, 9, 10)
    budget = planning.create_budget(
        organization=helper.org,
        actor=actors["finance"],
        title="Synthetic campaign spending",
        year=2026,
        month=9,
        channel="meta_ads",
        amount="80000000",
    )
    budget = planning.transition_budget(
        organization=helper.org,
        actor=actors["finance"],
        budget=budget,
        action="submit",
        expected_version=budget.version,
    )
    budget = planning.transition_budget(
        organization=helper.org,
        actor=helper.owner,
        budget=budget,
        action="approve",
        expected_version=budget.version,
        notes="Synthetic authorized spending limit",
    )
    entry = planning.add_budget_entry(
        organization=helper.org,
        actor=actors["finance"],
        budget=budget,
        kind="open_commitment" if kind in ("manual", "multi") else "reservation",
        amount="80000000",
        date=date(2026, 9, 8),
        description="Synthetic original campaign spending",
        reference="SYN-ORIGIN",
        idempotency_key="SYN-ORIGIN",
    )
    if kind == "manual":
        planning.record_budget_payment(
            organization=helper.org,
            actor=actors["finance"],
            entry=entry,
            amount="30000000",
            date=date(2026, 9, 9),
            description="Synthetic reported campaign payment",
            reference="SYN-ORIGINAL-REPORT",
            idempotency_key="SYN-ORIGINAL-REPORT",
        )
    data = {
        "organization": helper.org.pk,
        "credentials": credentials,
        "budget": budget.pk,
        "entry": entry.pk,
        "kind": kind,
        "original_entries": list(
            budget.entries.order_by("pk").values(
                "id", "kind", "amount", "date", "settles_id", "voided_at"
            )
        ),
    }
    if kind != "manual":
        bill = helper.bill(
            amount="80000000" if kind == "multi" else "60000000",
            prepaid=kind != "multi",
            approved=kind != "stale",
        )
        data.update(bill=bill.pk, bill_number=bill.number)
        if kind == "prepaid":
            helper.no_withholding(bill)
            first = helper.payment(
                bill, "20000000", day=date(2026, 9, 17), reference="SYN-PAY-20"
            )
            second = helper.payment(
                bill, "15000000", day=date(2026, 9, 25), reference="SYN-PAY-15"
            )
            data.update(first_payment=first.pk, second_payment=second.pk)
    data["original_journals"] = journal_identity(helper.org)
    data["original_bank_count"] = BankTransaction.objects.filter(
        organization=helper.org
    ).count()
    return data


fixtures = {
    str(width): {
        kind: fixture(f"spend-{width}-{kind}", kind)
        for kind in ("manual", "prepaid", "multi", "stale")
    }
    for width in (1440, 390)
}
foreign = fixture("spend-foreign", "manual")
(OUT / "fixture.json").write_text(
    json.dumps({"fixtures": fixtures, "foreign": foreign}, default=str),
    encoding="utf-8",
)


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


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
        result = subprocess.run(
            [args.node, str(Path(__file__).with_suffix(".cjs"))],
            env=browser_env,
            timeout=300,
        )
        result_code = result.returncode
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

persisted = []
if result_code == 0:
    for width, groups in fixtures.items():
        for kind, data in groups.items():
            org = Organization.objects.get(pk=data["organization"])
            spend = BudgetSpend.objects.get(
                organization=org, origin_entry_id=data["entry"]
            )
            row = spend_position(
                organization=org, spend=spend, as_of=timezone.localdate()
            )
            expected = {
                "manual": (
                    "23000000.00",
                    "55000000.00",
                    "0.00",
                    "78000000.00",
                    "0.00",
                    "31000000.00",
                ),
                "prepaid": (
                    "20000000.00",
                    "40000000.00",
                    "20000000.00",
                    "80000000.00",
                    "35000000.00",
                    "10000000.00",
                ),
                "multi": (
                    "20000000.00",
                    "60000000.00",
                    "0.00",
                    "80000000.00",
                    "0.00",
                    "0.00",
                ),
                "stale": (
                    "0.00",
                    "60000000.00",
                    "20000000.00",
                    "80000000.00",
                    "0.00",
                    "0.00",
                ),
            }[kind]
            actual = tuple(
                row[key]
                for key in (
                    "incurred",
                    "open_commitments",
                    "reservations",
                    "consumed",
                    "verified_cash",
                    "unmatched_reported_payments",
                )
            )
            assert row["current"] and actual == expected, (
                width,
                kind,
                actual,
                expected,
            )
            original_entries = list(
                spend.budget.entries.order_by("pk").values(
                    "id", "kind", "amount", "date", "settles_id", "voided_at"
                )
            )
            assert original_entries == data["original_entries"]
            assert (
                BankTransaction.objects.filter(organization=org).count()
                == data["original_bank_count"]
            )
            journals = journal_identity(org)
            if kind in ("manual", "multi"):
                assert journals == data["original_journals"]
            else:
                original_ids = {row["id"] for row in data["original_journals"]}
                assert [row for row in journals if row["id"] in original_ids] == data[
                    "original_journals"
                ]
                assert len(journals) == len(data["original_journals"]) + 1
            if kind == "manual":
                assert (
                    BudgetSpendMovement.objects.filter(
                        spend=spend, kind="convert", reverse_of__isnull=True
                    ).count()
                    == 3
                )
                assert (
                    BudgetSpendMovement.objects.filter(
                        spend=spend, kind="reverse"
                    ).count()
                    == 1
                )
                assert not BudgetBillLink.objects.filter(spend=spend).exists()
            if kind == "prepaid":
                assert (
                    PrepaymentRelease.objects.filter(
                        organization=org, bill_id=data["bill"], amount="20000000"
                    ).count()
                    == 1
                )
                assert BudgetPaymentMatch.objects.filter(spend=spend).count() == 2
                assert (
                    BudgetPaymentMatch.objects.filter(
                        spend=spend, voided_at__isnull=True
                    ).count()
                    == 1
                )
                assert row["payments"] == "45000000.00"
            if kind == "multi":
                link = BudgetBillLink.objects.get(spend=spend)
                assert link.voided_at is not None and len(link.coverage) == 2
                assert sorted(item["amount"] for item in link.coverage) == [
                    "20000000.00",
                    "60000000.00",
                ]
            if kind == "stale":
                assert (
                    BudgetBillLink.objects.filter(
                        spend=spend, voided_at__isnull=True
                    ).count()
                    == 1
                )
            persisted.append(
                {
                    "width": int(width),
                    "kind": kind,
                    "canonical": {
                        key: row[key]
                        for key in (
                            "incurred",
                            "open_commitments",
                            "reservations",
                            "consumed",
                            "reported_payments",
                            "unmatched_reported_payments",
                            "verified_cash",
                            "payments",
                        )
                    },
                    "original_entries_unchanged": True,
                    "original_journals_unchanged": True,
                    "bank_count_unchanged": True,
                    "source_identity": spend.content_hash,
                    "version": spend.version,
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
    "exit_code": result_code,
    "evidence": str(OUT),
}
(OUT / "database-verified.json").write_text(
    json.dumps(metadata, indent=2), encoding="utf-8"
)
print(json.dumps(metadata))
raise SystemExit(result_code)
