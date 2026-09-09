from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.db import OperationalError
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from whitenoise.middleware import WhiteNoiseMiddleware
from waitress.proxy_headers import proxy_headers_middleware

from core.throttle import allow_login_attempt, reset_login_attempts
from config.health import readiness_response


class SecurityDeploymentTests(TestCase):
    def test_one_attacker_does_not_exhaust_another_account_on_the_same_office_ip(self):
        for _ in range(20):
            allow_login_attempt(username="attacker", remote_addr="192.0.2.10")
        self.assertTrue(allow_login_attempt(username="colleague", remote_addr="192.0.2.10"))
        self.assertFalse(allow_login_attempt(username="attacker", remote_addr="192.0.2.10"))

    def test_pair_limit_resets_after_complete_success_but_ip_abuse_remains_bounded(self):
        for _ in range(10):
            self.assertTrue(allow_login_attempt(username="alice", remote_addr="192.0.2.10"))
        self.assertFalse(allow_login_attempt(username="alice", remote_addr="192.0.2.10"))
        reset_login_attempts(username="alice", remote_addr="192.0.2.10")
        self.assertTrue(allow_login_attempt(username="alice", remote_addr="192.0.2.10"))
        for index in range(288):
            self.assertTrue(allow_login_attempt(username=f"user{index}", remote_addr="192.0.2.10"))
        self.assertFalse(allow_login_attempt(username="newuser", remote_addr="192.0.2.10"))

    def test_distributed_guessing_has_an_account_limit(self):
        for index in range(50):
            self.assertTrue(allow_login_attempt(username="ALIce", remote_addr=f"192.0.2.{index + 1}"))
        self.assertFalse(allow_login_attempt(username="alice", remote_addr="198.51.100.1"))

    def test_readiness_checks_db_and_private_storage_and_never_leaks_errors(self):
        request = RequestFactory().get("/ready/")
        with TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            self.assertEqual(readiness_response(request).status_code, 200)
            self.assertEqual(list(Path(directory).iterdir()), [])
            with patch("config.health.connection.cursor", side_effect=OperationalError("secret DB address")):
                response = readiness_response(request)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(b"secret", response.content)
            with patch("config.health.MigrationExecutor") as executor:
                executor.return_value.migration_plan.return_value = [object()]
                self.assertEqual(readiness_response(request).status_code, 503)
        with override_settings(MEDIA_ROOT=Path(directory) / "missing"):
            self.assertEqual(readiness_response(request).status_code, 503)

    def test_collected_static_is_served_with_debug_disabled_and_private_paths_are_absent(self):
        with TemporaryDirectory() as directory:
            Path(directory, "app.css").write_text("body { color: black; }", encoding="utf-8")
            with override_settings(DEBUG=False, STATIC_ROOT=directory, WHITENOISE_AUTOREFRESH=False, WHITENOISE_USE_FINDERS=False):
                app = WhiteNoiseMiddleware(lambda request: HttpResponse(status=404))
                response = app(RequestFactory().get("/static/app.css", secure=True))
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"color", b"".join(response.streaming_content))
                # Direct middleware invocation has no test-client signal guard;
                # close the file without emitting request_finished in a DB test.
                response.file_to_stream.close()
                self.assertEqual(app(RequestFactory().get("/media/private.pdf", secure=True)).status_code, 404)

    def test_waitress_only_normalizes_headers_from_exact_trusted_proxy(self):
        captured = {}
        def application(environ, start_response):
            captured.update(environ)
            start_response("200 OK", [])
            return [b"ok"]
        app = proxy_headers_middleware(application, trusted_proxy="172.30.77.10",
                                       trusted_proxy_count=1,
                                       trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"},
                                       clear_untrusted=True)
        for peer, scheme, remote in [("172.30.77.10", "https", "203.0.113.7"),
                                     ("192.0.2.8", "http", "192.0.2.8")]:
            environ = {"REMOTE_ADDR": peer, "HTTP_X_FORWARDED_PROTO": "https", "HTTP_X_FORWARDED_FOR": "203.0.113.7",
                       "wsgi.url_scheme": "http", "SERVER_PORT": "8000", "SERVER_NAME": "app", "wsgi.input": BytesIO()}
            list(app(environ, lambda *args: None))
            self.assertEqual(captured["wsgi.url_scheme"], scheme)
            self.assertEqual(captured["REMOTE_ADDR"], remote)
