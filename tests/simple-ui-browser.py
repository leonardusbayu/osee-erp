"""Check shared navigation and role-based login in a disposable synthetic workspace.

Usage: python tests/simple-ui-browser.py --node PATH --playwright PATH
No company database, local configuration, or provider key is loaded.
"""

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--playwright", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = root / ".local" / "simple-ui-qa"
    output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    sys.path.insert(0, str(root))
    os.environ.update(
        DJANGO_SETTINGS_MODULE="config.settings",
        DJANGO_READ_DOT_ENV="0",
        DJANGO_DEBUG="1",
        POSTGRES_DB="",
        DJANGO_SQLITE_PATH=str(run / "qa.sqlite3"),
        DJANGO_MEDIA_ROOT=str(run / "media"),
        DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost,testserver",
        OSEE_MFA_REQUIRED="0",
        OSEE_LOCAL_SETUP="0",
        OSEE_DEMO_MODE="0",
        DJANGO_SECRET_KEY=secrets.token_urlsafe(60),
        DJANGO_SECRET_KEY_FILE="",
        OTP_ENCRYPTION_KEY="",
        OTP_ENCRYPTION_KEY_FILE="",
        OPENROUTER_API_KEY="",
        DIRECTOR_OPENROUTER_API_KEY="",
        MARKETING_OPENROUTER_API_KEY="",
        OPENROUTER_PRIVATE_CONTEXT_ENABLED="0",
        MARKETING_AI_PRIVATE_AGGREGATES_ENABLED="0",
        DIRECTOR_AI_PRIVATE_AGGREGATES_ENABLED="0",
    )
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.staticfiles.handlers import StaticFilesHandler
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application
    from django.utils import timezone
    from core.models import Membership, Organization
    from finance.models import (
        BankAccount,
        BankTransaction,
        Invoice,
        Journal,
        Party,
        Product,
    )
    from finance.services import create_invoice, issue_invoice
    from marketing import services, workflow
    from wsgiref.simple_server import make_server, WSGIRequestHandler

    assert (
        Path(settings.DATABASES["default"]["NAME"]).resolve()
        == (run / "qa.sqlite3").resolve()
    )
    assert not settings.OPENROUTER_API_KEY and not settings.DIRECTOR_OPENROUTER_API_KEY
    call_command("migrate", verbosity=0)
    organization = Organization.objects.create(
        name="OSEE · Uji tampilan sederhana", is_demo=True
    )
    password = secrets.token_urlsafe(32)
    users = {}
    for role in ("owner", "finance", "marketing"):
        user = get_user_model().objects.create_user(
            username="qa_ui_" + role,
            password=password,
            first_name={
                "owner": "Pemilik QA",
                "finance": "Finance QA",
                "marketing": "Marketing QA",
            }[role],
        )
        Membership.objects.create(organization=organization, user=user, role=role)
        users[role] = user
    member = services.create_member(
        organization=organization,
        actor=users["owner"],
        name="Staf Marketing QA",
        discipline="sales",
    )
    workflow.configure_member(
        organization=organization,
        actor=users["owner"],
        member=member,
        expected_version=member.version,
        account=users["marketing"],
        active=True,
        reason="Hubungan akun sintetis untuk pemeriksaan tampilan",
    )
    lead = services.create_lead(
        organization=organization,
        actor=users["marketing"],
        reference="QA-UI-LEAD",
        name="Calon pelanggan uji",
        owner=member,
        channel="sales",
        received_at=timezone.now(),
        next_follow_up=timezone.localdate(),
    )
    overdue = services.create_lead(
        organization=organization,
        actor=users["marketing"],
        reference="QA-UI-LATE",
        name="Janji terlambat uji",
        owner=member,
        channel="sales",
        received_at=timezone.now() - timedelta(days=3),
        next_follow_up=timezone.localdate() - timedelta(days=1),
    )
    appointment = services.create_lead(
        organization=organization,
        actor=users["marketing"],
        reference="QA-UI-APPOINTMENT",
        name="Calon pelanggan dengan jam janji",
        owner=member,
        channel="sales",
        received_at=timezone.now() - timedelta(hours=1),
    )
    appointment_at = (timezone.localtime() + timedelta(days=2)).replace(
        hour=11, minute=45, second=0, microsecond=0
    )
    workflow.record_contact(
        organization=organization,
        actor=users["marketing"],
        lead=appointment,
        expected_version=appointment.version,
        occurred_at=timezone.now(),
        method="whatsapp",
        outcome="responded",
        summary="Kontak sintetis untuk janji",
        evidence="Referensi bukti sintetis",
        first_response=True,
        followup_action="set",
        next_due_at=appointment_at,
    )
    account = BankAccount.objects.create(
        organization=organization,
        name="BNI · Data uji",
        account_number="QA-ONLY-NOT-A-REAL-ACCOUNT",
    )
    BankTransaction.objects.create(
        organization=organization,
        account=account,
        reference="QA-UI-BANK",
        description="Pembayaran contoh untuk pemeriksaan tampilan",
        date=timezone.localdate(),
        amount="510000",
    )
    BankTransaction.objects.create(
        organization=organization,
        account=account,
        reference="QA-UI-OUT",
        description="Pengeluaran uji tanpa tagihan yang siap",
        date=timezone.localdate(),
        amount="-100000",
    )
    party = Party.objects.create(
        organization=organization, name="Pembeli sintetis QA", kind="customer"
    )
    product = Product.objects.create(
        organization=organization,
        code="QA-UI-ITP",
        name="Produk uji QA",
        kind="itp",
        default_price="500000",
    )
    invoice = create_invoice(
        organization=organization,
        actor=users["finance"],
        party=party,
        product=product,
        quantity=1,
        date=timezone.localdate(),
        due_date=timezone.localdate(),
        service_date=timezone.localdate(),
    )
    issue_invoice(organization=organization, actor=users["finance"], invoice=invoice)
    initial_journals = Journal.objects.count()
    (run / "fixture.json").write_text(
        json.dumps(
            {
                "roles": {role: user.get_username() for role, user in users.items()},
                "organization": organization.name,
                "lead": lead.pk,
                "lead_name": lead.name,
                "overdue": overdue.pk,
                "appointment": appointment.pk,
                "appointment_date": appointment_at.date().isoformat(),
                "invoice": invoice.pk,
                "account": account.pk,
                "today": timezone.localdate().isoformat(),
            }
        ),
        encoding="utf-8",
    )

    class Quiet(WSGIRequestHandler):
        def log_message(self, *args):
            pass

    server = make_server(
        "127.0.0.1", 0, StaticFilesHandler(get_wsgi_application()), handler_class=Quiet
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    env.update(
        OSEE_QA_URL=f"http://127.0.0.1:{server.server_port}",
        OSEE_QA_DIR=str(run),
        OSEE_PLAYWRIGHT_PATH=args.playwright,
        OSEE_QA_PASSWORD=password,
    )
    try:
        result = subprocess.run(
            [args.node, str(root / "tests" / "simple-ui-browser.cjs")],
            cwd=root,
            env=env,
            timeout=300,
        )
        if result.returncode:
            return result.returncode
        assert (
            Invoice.objects.count() == 1 and Journal.objects.count() == initial_journals
        )
        assert not invoice.allocations.exists(), (
            "Rejected matching must not allocate money"
        )
        assert BankTransaction.objects.count() == 2, (
            "Statement preview must not import before confirmation"
        )
        appointment.refresh_from_db()
        assert appointment.next_follow_up_at == appointment_at, (
            "Summary edit must retain the exact appointment"
        )
        assert appointment.notes == "Catatan terbaru dari uji browser"
        print(
            json.dumps(
                {"passed": True, "company_data_untouched": True, "evidence": str(run)}
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
