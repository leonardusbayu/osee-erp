"""Readiness is separate from process liveness and never returns infrastructure detail."""
import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse
from django.views.decorators.http import require_GET


def check_readiness():
    """Require a reachable, migrated DB and usable private persistent storage."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        if cursor.fetchone()[0] != 1:
            raise RuntimeError("database check failed")
    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        raise RuntimeError("pending database migrations")
    directory = Path(settings.MEDIA_ROOT)
    if not directory.is_dir():
        raise RuntimeError("private storage unavailable")
    token = secrets.token_bytes(32)
    with NamedTemporaryFile(prefix=".readiness-", dir=directory, mode="w+b") as probe:
        probe.write(token)
        probe.flush()
        probe.seek(0)
        if probe.read() != token:
            raise RuntimeError("private storage check failed")


@require_GET
def readiness_response(request):
    try:
        check_readiness()
    except Exception:
        response = JsonResponse({"status": "not_ready"}, status=503)
    else:
        response = JsonResponse({"status": "ready"})
    response["Cache-Control"] = "no-store"
    return response
