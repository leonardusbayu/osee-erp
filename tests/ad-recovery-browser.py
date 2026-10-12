"""Private advertising recovery browser acceptance; synthetic database only."""

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
output_root = ROOT / ".local/ad-recovery-browser"
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
from core.models import AuditEvent, Membership, Organization
from finance.models import BankTransaction, Journal
from marketing import services
from marketing.ad_sources import active_ad_observations
from marketing.coverage import declaration_current
from marketing.models import AdDailyObservation, AdObservationRecovery, SourceCoverage
from wsgiref.simple_server import make_server, WSGIRequestHandler

call_command("migrate", verbosity=0)


def finance_identity(org):
    return {
        "journals": list(
            Journal.objects.filter(organization=org)
            .order_by("pk")
            .values_list("pk", "source_key")
        ),
        "bank_rows": list(
            BankTransaction.objects.filter(organization=org)
            .order_by("pk")
            .values_list("pk", "amount")
        ),
    }


def fixture(prefix, kind):
    org = Organization.objects.create(name=f"Synthetic {prefix} ad recovery")
    actors, credentials = {}, {}
    for role in ("owner", "finance", "marketing", "auditor"):
        password = secrets.token_urlsafe(24)
        actor = get_user_model().objects.create_user(
            username=f"{prefix}-{role}", password=password
        )
        Membership.objects.create(organization=org, user=actor, role=role)
        actors[role] = actor
        credentials[role] = {"username": actor.username, "password": password}
    original = services.create_campaign(
        organization=org,
        actor=actors["owner"],
        name="Synthetic original campaign",
        channel="meta_ads",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )
    corrected = services.create_campaign(
        organization=org,
        actor=actors["owner"],
        name="Synthetic correct campaign",
        channel="google_ads",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )
    return {
        "organization": org.pk,
        "credentials": credentials,
        "original_campaign": original.pk,
        "corrected_campaign": corrected.pk,
        "kind": kind,
        "finance_before": finance_identity(org),
    }


fixtures = {
    str(width): {
        "replace": fixture(f"ad-{width}-replace", "replace"),
        "withdraw": fixture(
            f"ad-{width}-withdraw", "zero" if width == 1440 else "unknown"
        ),
    }
    for width in (1440, 390)
}
foreign = fixture("ad-foreign", "foreign")
foreign_org = Organization.objects.get(pk=foreign["organization"])
foreign_actor = get_user_model().objects.get(
    username=foreign["credentials"]["owner"]["username"]
)
foreign_ad = services.save_ad_observation(
    organization=foreign_org,
    actor=foreign_actor,
    campaign=foreign["original_campaign"],
    date="2026-01-10",
    spend="999999",
    source="FOREIGN PRIVATE AD SOURCE",
    reference="FOREIGN PRIVATE AD DOCUMENT",
)
foreign["observation"] = foreign_ad.pk
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
        result_code = subprocess.run(
            [args.node, str(Path(__file__).with_suffix(".cjs"))],
            env=browser_env,
            timeout=420,
        ).returncode
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

persisted, verification_error = [], None
if result_code == 0:
    try:
        for width, groups in fixtures.items():
            for kind, data in groups.items():
                org = Organization.objects.get(pk=data["organization"])
                original = AdDailyObservation.objects.get(
                    organization=org,
                    source_root__isnull=True,
                    reference="SYNTHETIC-ORIGINAL-DOCUMENT",
                )
                command = AdObservationRecovery.objects.get(
                    organization=org, predecessor=original
                )
                assert original.withdrawn_at is not None
                assert (
                    original.created_by.username
                    == data["credentials"]["marketing"]["username"]
                )
                assert (
                    original.campaign_id == data["original_campaign"]
                    and str(original.date) == "2026-01-10"
                )
                assert finance_identity(org) == data["finance_before"]
                assert (
                    AdObservationRecovery.objects.filter(organization=org).count() == 1
                )
                coverage = list(SourceCoverage.objects.filter(organization=org))
                if kind == "replace":
                    child = command.successor
                    assert (
                        command.action == "replace"
                        and child
                        and child.reference == original.reference
                    )
                    assert (
                        child.source_root_id == original.pk
                        and child.replaces_id == original.pk
                    )
                    assert (
                        child.campaign_id == data["corrected_campaign"]
                        and str(child.date) == "2026-01-11"
                    )
                    assert (
                        child.ad_name == "Actual ad A"
                        and child.audience == "Actual audience"
                        and child.region == "Jakarta"
                    )
                    assert str(original.spend) == str(child.spend) == "110000.00"
                    assert original.version == 3 and child.version == 1
                    assert command.request_payload["expected_version"] == 2
                    assert active_ad_observations(organization=org).count() == 2
                    assert len(coverage) == 2 and all(
                        not declaration_current(row) for row in coverage
                    )
                    assert (
                        AuditEvent.objects.filter(
                            organization=org, action="marketing.ad.corrected"
                        ).count()
                        == 1
                    )
                else:
                    assert command.action == "withdraw" and command.successor is None
                    assert (
                        original.version == 2
                        and not active_ad_observations(organization=org).exists()
                    )
                    if data["kind"] == "zero":
                        assert str(original.spend) == "0.00" and original.clicks == 0
                        assert len(coverage) == 1 and not declaration_current(
                            coverage[0]
                        )
                    else:
                        assert original.spend is None and original.impressions == 1000
                        assert not coverage
                persisted.append(
                    {
                        "width": int(width),
                        "flow": kind,
                        "source_kind": data["kind"],
                        "original_id": original.pk,
                        "successor_id": command.successor_id,
                        "original_reference": original.reference,
                        "history_retained": True,
                        "original_creator_retained": True,
                        "finance_unchanged": True,
                        "active_source_count": active_ad_observations(
                            organization=org
                        ).count(),
                        "saved_decision_count": 1,
                        "coverage_reconfirmation_needed": True,
                    }
                )
        assert not AdObservationRecovery.objects.filter(
            organization=foreign_org
        ).exists()
    except Exception as error:
        verification_error = str(error)
        result_code = 1
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
    "verification_error": verification_error,
    "exit_code": result_code,
    "evidence": str(OUT),
}
(OUT / "database-verified.json").write_text(
    json.dumps(metadata, indent=2), encoding="utf-8"
)
print(json.dumps(metadata))
raise SystemExit(result_code)
