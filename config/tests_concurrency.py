"""Real PostgreSQL locking checks; explicitly skipped by the SQLite suite."""
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless

from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from core.throttle import allow_login_attempt


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class PostgreSQLThrottleTests(TransactionTestCase):
    def test_parallel_requests_cannot_exceed_the_pair_quota(self):
        def attempt(_):
            close_old_connections()
            try:
                return allow_login_attempt(username="concurrent-account", remote_addr="192.0.2.55")
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(attempt, range(24)))
        self.assertEqual(sum(results), 10)
