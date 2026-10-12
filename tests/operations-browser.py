"""Run new operations browser journeys only in a fresh synthetic database."""

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, time, timedelta


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--playwright", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = root / ".local" / "operations-qa"
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
        MARKETING_AI_PRIVATE_AGGREGATES_ENABLED="0",
        DIRECTOR_AI_PRIVATE_AGGREGATES_ENABLED="0",
        OPENROUTER_API_KEY="",
        DIRECTOR_OPENROUTER_API_KEY="",
    )
    import django

    django.setup()
    from django.contrib.auth import get_user_model
    from django.contrib.staticfiles.handlers import StaticFilesHandler
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application
    from django.test import Client
    from django.utils import timezone
    from core.models import Membership, Organization
    from finance.models import Party, Product, BankAccount, BankTransaction, Invoice
    from finance.services import reconcile_receipt, report_balances
    from operations import services
    from operations.models import Registration
    from wsgiref.simple_server import make_server, WSGIRequestHandler

    call_command("migrate", verbosity=0)
    org = Organization.objects.create(name="OSEE · Uji pesanan", is_demo=True)
    users = {}
    for role in ("owner", "marketing"):
        user = get_user_model().objects.create_user(username="qa_ops_" + role)
        Membership.objects.create(organization=org, user=user, role=role)
        users[role] = user
        client = Client()
        client.force_login(user)
        session = client.session
        session["organization_id"] = org.pk
        session.save()
        (run / (role + "-state.json")).write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "sessionid",
                            "value": session.session_key,
                            "domain": "127.0.0.1",
                            "path": "/",
                            "httpOnly": True,
                            "secure": False,
                            "sameSite": "Lax",
                        }
                    ],
                    "origins": [],
                }
            ),
            encoding="utf-8",
        )
    day = timezone.localdate() - timedelta(days=1)
    actor = users["owner"]
    party = Party.objects.create(
        organization=org, name="Mitra sintetis", kind="reseller"
    )
    product = Product.objects.create(
        organization=org,
        code="ITP",
        kind="itp",
        name="TOEFL ITP uji",
        default_price="510000",
    )
    order = services.create_order(
        organization=org, actor=actor, buyer=party, date=day, reference="QA-ORDER"
    )
    line = services.add_order_line(
        organization=org, actor=actor, order=order, product=product, quantity=2
    )
    invoice = services.invoice_line(organization=org, actor=actor, line=line, date=day)
    account = BankAccount.objects.create(
        organization=org, name="BNI uji", account_number="SYNTHETIC"
    )
    bank = BankTransaction.objects.create(
        organization=org,
        account=account,
        reference="QA-PAID",
        amount="1020000",
        date=day,
    )
    reconcile_receipt(
        organization=org,
        actor=actor,
        invoice=invoice,
        transaction=bank,
        amount="1020000",
    )
    session = services.create_session(
        organization=org,
        actor=actor,
        reference="Sesi QA",
        product=product,
        starts_at=timezone.make_aware(datetime.combine(day, time(10))),
        capacity=2,
    )
    new_order = services.create_order(
        organization=org, actor=actor, buyer=party, date=day, reference="QA-INTAKE"
    )
    (run / "fixture.json").write_text(
        json.dumps(
            {
                "order": order.pk,
                "line": line.pk,
                "invoice": invoice.pk,
                "session": session.pk,
                "day": str(day),
                "new_order": new_order.pk,
                "product": product.pk,
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
    )
    try:
        result = subprocess.run(
            [args.node, str(root / "tests/operations-browser.cjs")],
            env=env,
            cwd=root,
            timeout=300,
        )
        if result.returncode:
            return result.returncode
        assert (
            Registration.objects.get(external_id="QA-PARTICIPANT").status == "completed"
        )
        assert Registration.objects.get(external_id="QA-CSV").status == "registered"
        assert report_balances(organization=org)["REVENUE"] == -510000
        assert Invoice.objects.count() == 2
        print(
            json.dumps(
                {"passed": True, "persisted_state_verified": True, "evidence": str(run)}
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
