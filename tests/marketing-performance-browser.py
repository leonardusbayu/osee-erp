"""Run UI checks in a new synthetic database; never modify the company database.

Usage: python tests/marketing-performance-browser.py --node PATH --playwright PATH
Screenshots/results stay under .local/marketing-performance-qa/<unique run>.
"""

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
    output = root / ".local" / "marketing-performance-qa"
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
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.staticfiles.handlers import StaticFilesHandler
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application
    from django.test import Client
    from django.utils import timezone
    from core.models import Membership, Organization
    from finance.models import Invoice, Journal
    from marketing import activities, kpi, services
    from marketing.activity_models import WorkActivity
    from marketing.kpi_models import KpiReview, KpiTarget
    from wsgiref.simple_server import make_server, WSGIRequestHandler

    assert (
        Path(settings.DATABASES["default"]["NAME"]).resolve()
        == (run / "qa.sqlite3").resolve()
    )
    call_command("migrate", verbosity=0)
    org = Organization.objects.create(name="OSEE · Data uji KPI", is_demo=True)
    users = {}
    for role, membership_role in (
        ("owner", "owner"),
        ("marketing", "marketing"),
        ("replacement", "marketing"),
    ):
        user = get_user_model().objects.create_user(username=f"qa_{role}")
        Membership.objects.create(organization=org, user=user, role=membership_role)
        users[role] = user
        client = Client()
        client.force_login(user)
        session = client.session
        session["organization_id"] = org.pk
        session.save()
        (run / f"{role}-state.json").write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": settings.SESSION_COOKIE_NAME,
                            "value": session.session_key,
                            "domain": "127.0.0.1",
                            "path": "/",
                            "expires": -1,
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
    today = timezone.localdate()
    end = today.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    member = services.create_member(
        organization=org, actor=users["owner"], name="Ayu · uji KPI", discipline="sales"
    )
    from marketing.workflow import configure_member

    configure_member(
        organization=org,
        actor=users["owner"],
        member=member,
        expected_version=member.version,
        account=users["marketing"],
        active=True,
        reason="Map synthetic execution login",
    )
    replacement_member = services.create_member(
        organization=org,
        actor=users["owner"],
        name="Bima · uji alih tugas",
        discipline="sales",
    )
    configure_member(
        organization=org,
        actor=users["owner"],
        member=replacement_member,
        expected_version=replacement_member.version,
        account=users["replacement"],
        active=True,
        reason="Map synthetic replacement login",
    )
    for i in range(2):
        received = timezone.make_aware(datetime.combine(start, time(9, i)))
        services.create_lead(
            organization=org,
            actor=users["marketing"],
            reference=f"QA-KPI-{i}",
            name=f"Prospek QA {i}",
            owner=member,
            channel="sales",
            received_at=received,
            first_response_at=received + timedelta(minutes=15),
        )
    target = kpi.create_target(
        organization=org,
        actor=users["marketing"],
        title="KPI contoh terisolasi",
        metric_key="leads",
        target="4",
        member=member,
        channel="sales",
        period_start=start,
        period_end=end,
        rationale="Angka uji, bukan target perusahaan",
    )
    target = kpi.activate_target(
        organization=org,
        actor=users["owner"],
        target=target,
        expected_version=1,
        reason="Uji alur persetujuan",
    )
    from marketing.tests_kpi import certify_lead_sources

    certify_lead_sources(org, users["owner"], target)
    activity = activities.create_activity(
        organization=org,
        actor=users["owner"],
        title="Pekerjaan contoh terisolasi",
        owner=member,
        channel="sales",
        kind="follow_up",
        due_date=today,
        description="Data sintetis untuk QA",
    )
    (run / "fixture.json").write_text(
        json.dumps(
            {
                "target": target.pk,
                "activity": activity.pk,
                "member": member.pk,
                "replacement_member": replacement_member.pk,
                "start": str(start),
                "end": str(end),
                "today": str(today),
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
            [args.node, str(root / "tests" / "marketing-performance-browser.cjs")],
            env=env,
            cwd=root,
            timeout=300,
        )
        if result.returncode:
            return result.returncode
        saved = KpiTarget.objects.get(title="Target dari browser")
        assert saved.status == "active"
        assert KpiReview.objects.get(target=saved).actual == 2
        work = WorkActivity.objects.get(title="Tugas dari browser")
        assert work.status == "accepted" and work.reviewed_by_id == users["owner"].pk
        assert work.evidence == "Arsip revisi QA #2"
        handed = WorkActivity.objects.get(pk=activity.pk)
        assert (
            handed.owner_id == member.pk
            and handed.current_owner_id == replacement_member.pk
        )
        assert handed.evidence == "Pengajuan owner untuk uji penarikan"
        assignment = handed.assignment_events.get()
        assert (
            assignment.created_by_id == users["owner"].pk
            and assignment.member_id == replacement_member.pk
        )
        assert assignment.previous_member_id == member.pk
        assert not Invoice.objects.exists() and not Journal.objects.exists()
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
