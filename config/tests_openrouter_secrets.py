import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase


class OpenRouterSecretSettingsTests(SimpleTestCase):
    def test_api_keys_load_from_mounted_secret_files(self):
        with TemporaryDirectory() as directory:
            tax_key = Path(directory, "tax-key")
            director_key = Path(directory, "director-key")
            tax_key.write_text("synthetic-tax-key\n", encoding="utf-8")
            director_key.write_text("synthetic-director-key\n", encoding="utf-8")
            env = os.environ.copy()
            for name in (
                "OPENROUTER_API_KEY",
                "DIRECTOR_OPENROUTER_API_KEY",
                "OPENROUTER_API_KEY_FILE",
                "DIRECTOR_OPENROUTER_API_KEY_FILE",
                "POSTGRES_DB",
            ):
                env.pop(name, None)
            env.update(
                DJANGO_READ_DOT_ENV="0",
                DJANGO_DEBUG="1",
                DJANGO_SECRET_KEY="synthetic-deployment-test-key",
                DJANGO_SQLITE_PATH=str(Path(directory, "db.sqlite3")),
                DJANGO_MEDIA_ROOT=str(Path(directory, "media")),
                OPENROUTER_API_KEY_FILE=str(tax_key),
                DIRECTOR_OPENROUTER_API_KEY_FILE=str(director_key),
            )
            script = """\
import json
import config.settings as settings

print(
    json.dumps(
        [settings.OPENROUTER_API_KEY, settings.DIRECTOR_OPENROUTER_API_KEY]
    )
)
"""
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                check=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(
                json.loads(result.stdout.strip()),
                ["synthetic-tax-key", "synthetic-director-key"],
            )
