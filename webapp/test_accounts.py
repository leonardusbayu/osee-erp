import time
from unittest.mock import patch

import pyotp
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings, Client
from django.urls import reverse

from core import mfa
from core.account_services import update_member_access, reset_member_password
from core.models import AccountSecurity, AuditEvent, Membership, Organization


PASSWORD = "Synthetic!Account-128-zQ"


@override_settings(MFA_REQUIRED=False, OTP_ENCRYPTION_KEY=Fernet.generate_key().decode(), PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AccountControlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Synthetic Account QA")
        cls.other = Organization.objects.create(name="Synthetic Other Account QA")
        for role in ["owner", "finance", "marketing"]:
            user = get_user_model().objects.create_user(username=f"account-{role}", password=PASSWORD)
            member = Membership.objects.create(organization=cls.org, user=user, role=role)
            setattr(cls, role, user)
            setattr(cls, role + "_member", member)

    def setUp(self):
        self.client.force_login(self.owner)

    def test_owner_revokes_only_selected_company_and_history_stays(self):
        Membership.objects.create(organization=self.other, user=self.finance, role="owner")
        client = Client()
        client.force_login(self.finance)
        session = client.session
        session["organization_id"] = self.org.pk
        session.save()
        response = self.client.post(reverse("member_access", args=[self.finance_member.pk]), {"role": "finance", "reason": "Employment ended"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get(reverse("sales")).status_code, 403)
        self.assertFalse(Membership.objects.filter(pk=self.finance_member.pk).exists())
        self.assertFalse(Membership.all_objects.get(pk=self.finance_member.pk).is_active)
        self.assertTrue(Membership.objects.filter(organization=self.other, user=self.finance).exists())
        self.assertTrue(AuditEvent.objects.filter(action="organization.member.access_updated").exists())

    def test_last_owner_self_demotion_and_nonowner_attempts_fail(self):
        with self.assertRaises(ValidationError):
            update_member_access(organization=self.org, actor=self.owner, membership_id=self.owner_member.pk, role="finance", is_active=True, reason="Attempt")
        with self.assertRaises(PermissionDenied):
            update_member_access(organization=self.org, actor=self.finance, membership_id=self.owner_member.pk, role="finance", is_active=True, reason="Attempt")
        other_member = Membership.objects.create(organization=self.other, user=self.marketing, role="owner")
        self.assertEqual(self.client.post(reverse("member_access", args=[other_member.pk]), {"role": "finance", "reason": "Forged"}).status_code, 404)

    def test_reactivation_keeps_same_membership_and_role(self):
        update_member_access(organization=self.org, actor=self.owner, membership_id=self.finance_member.pk, role="finance", is_active=False, reason="Leave")
        update_member_access(organization=self.org, actor=self.owner, membership_id=self.finance_member.pk, role="finance", is_active=True, reason="Return")
        self.assertTrue(Membership.objects.filter(pk=self.finance_member.pk, role="finance").exists())

    def test_password_reset_kills_old_sessions_and_requires_rotation(self):
        old = Client()
        old.force_login(self.finance)
        new_password = "Temporary-Synthetic-198!"
        reset_member_password(organization=self.org, actor=self.owner, membership_id=self.finance_member.pk, password=new_password, reason="Verified staff identity")
        self.assertEqual(old.get(reverse("sales")).status_code, 302)
        new = Client()
        response = new.post(reverse("login"), {"username": self.finance.username, "password": new_password})
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(new.get(reverse("sales")), reverse("password_change"), fetch_redirect_response=False)
        self.assertNotIn(new_password, str(list(AuditEvent.objects.values_list("detail", flat=True))))

    def test_cross_company_shared_account_password_cannot_be_reset(self):
        Membership.objects.create(organization=self.other, user=self.finance, role="finance")
        with self.assertRaises(ValidationError):
            reset_member_password(organization=self.org, actor=self.owner, membership_id=self.finance_member.pk, password="Temp-other-129!", reason="Attempt")

    def test_enrollment_encrypts_secret_and_rejects_code_replay(self):
        secret = mfa.begin_enrollment(self.finance)
        state = AccountSecurity.objects.get(user=self.finance)
        self.assertNotIn(secret, state.totp_secret)
        code = pyotp.TOTP(secret).now()
        codes, version = mfa.confirm_enrollment(self.finance, code)
        self.assertIs(mfa.verify_challenge(self.finance, code), False)
        self.assertEqual(mfa.verify_challenge(self.finance, codes[0]), version)
        self.assertIs(mfa.verify_challenge(self.finance, codes[0]), False)
        state.refresh_from_db()
        self.assertEqual(len(state.recovery_codes), 7)
        self.assertNotIn(codes[1], str(state.recovery_codes))

    def test_password_alone_cannot_open_mfa_account(self):
        secret = mfa.begin_enrollment(self.finance)
        codes, _ = mfa.confirm_enrollment(self.finance, pyotp.TOTP(secret).now())
        client = Client()
        response = client.post(reverse("login"), {"username": self.finance.username, "password": PASSWORD, "next": "/sales/"})
        self.assertRedirects(response, reverse("mfa_challenge"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", client.session)
        self.assertEqual(client.get(reverse("sales")).status_code, 302)
        self.assertRedirects(client.post(reverse("mfa_challenge"), {"code": codes[0]}), "/sales/", fetch_redirect_response=False)
        self.assertEqual(client.get(reverse("sales")).status_code, 200)

    def test_revoked_membership_invalidates_pending_mfa(self):
        secret = mfa.begin_enrollment(self.finance)
        codes, _ = mfa.confirm_enrollment(self.finance, pyotp.TOTP(secret).now())
        client = Client()
        client.post(reverse("login"), {"username": self.finance.username, "password": PASSWORD})
        update_member_access(organization=self.org, actor=self.owner, membership_id=self.finance_member.pk, role="finance", is_active=False, reason="Revocation")
        self.assertRedirects(client.post(reverse("mfa_challenge"), {"code": codes[0]}), reverse("login"), fetch_redirect_response=False)

    @override_settings(MFA_REQUIRED=True)
    def test_required_mfa_routes_user_to_enrollment(self):
        self.assertRedirects(self.client.get(reverse("sales")), reverse("security_settings"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("security_settings")).status_code, 200)

    def test_tax_profile_get_does_not_create_record(self):
        from taxes.models import TaxProfile
        self.assertFalse(TaxProfile.objects.filter(organization=self.org).exists())
        self.assertEqual(self.client.get(reverse("tax_profile_setup")).status_code, 200)
        self.assertFalse(TaxProfile.objects.filter(organization=self.org).exists())
