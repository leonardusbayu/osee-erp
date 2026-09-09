from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import AccountSecurity, AuditEvent, Membership, Organization


class OfflineRecoveryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("lost-owner", password="Old-synthetic-passphrase-123!")
        self.org = Organization.objects.create(name="Synthetic recovery company")
        self.other = Organization.objects.create(name="Synthetic second company")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")
        Membership.objects.create(user=self.user, organization=self.other, role="finance")
        AccountSecurity.objects.create(user=self.user, totp_enabled=True, totp_secret="encrypted-synthetic",
                                       recovery_codes=["hashed-synthetic"], session_version=3)
        self.password = "New-synthetic-temporary-passphrase-456!"

    def recover(self, **options):
        output = StringIO()
        with patch("core.management.commands.recover_account.sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value=f"RECOVER {self.org.pk} lost-owner"), \
             patch("core.management.commands.recover_account.getpass.getpass", side_effect=[self.password, self.password]):
            call_command("recover_account", company_id=self.org.pk, username="lost-owner",
                         reason="Synthetic ID check and approval TICKET-123", stdout=output, **options)
        return output.getvalue()

    def test_offline_recovery_invalidates_sessions_and_audits_every_affected_company_without_secrets(self):
        output = self.recover(reset_mfa=True)
        self.user.refresh_from_db()
        security = AccountSecurity.objects.get(user=self.user)
        self.assertTrue(self.user.check_password(self.password))
        self.assertTrue(security.require_password_change)
        self.assertEqual(security.session_version, 4)
        self.assertFalse(security.totp_enabled)
        self.assertEqual(security.totp_secret, "")
        self.assertEqual(security.recovery_codes, [])
        events = list(AuditEvent.objects.filter(action="account.operator_recovery"))
        self.assertEqual({event.organization_id for event in events}, {self.org.pk, self.other.pk})
        self.assertTrue(all(event.actor is None for event in events))
        self.assertNotIn(self.password, output + str([event.detail for event in events]))
        self.assertNotIn("encrypted-synthetic", str([event.detail for event in events]))

    def test_password_only_recovery_preserves_mfa(self):
        self.recover(reset_mfa=False)
        self.assertTrue(AccountSecurity.objects.get(user=self.user).totp_enabled)

    def test_noninteractive_recovery_rejected_without_changes(self):
        with patch("core.management.commands.recover_account.sys.stdin.isatty", return_value=False), self.assertRaises(CommandError):
            call_command("recover_account", company_id=self.org.pk, username="lost-owner", reason="Verified identity TICKET-123")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Old-synthetic-passphrase-123!"))
        self.assertFalse(AuditEvent.objects.exists())

    def test_wrong_company_or_disabled_account_never_reactivates_access(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        with self.assertRaises(CommandError):
            self.recover(reset_mfa=True)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertFalse(AuditEvent.objects.exists())
