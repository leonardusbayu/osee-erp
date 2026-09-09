"""Offline account recovery: server access is a privileged authentication boundary."""
import getpass
import sys
import warnings

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.audit import record
from core.models import AccountSecurity, Membership


class Command(BaseCommand):
    help = "Recover an existing active account after offline identity verification (interactive server operator only)."

    def add_arguments(self, parser):
        parser.add_argument("--company-id", type=int, required=True)
        parser.add_argument("--username", required=True)
        parser.add_argument("--reason", required=True, help="Identity-verification and approval reference; do not include secrets.")
        parser.add_argument("--reset-mfa", action="store_true", help="Explicitly clear lost MFA enrollment and recovery codes.")

    def handle(self, *args, **options):
        if not sys.stdin.isatty():
            raise CommandError("An interactive private terminal is required; passwords cannot be passed as arguments or piped input.")
        reason = options["reason"].strip()
        if not 15 <= len(reason) <= 500:
            raise CommandError("Record a 15–500 character identity-verification reason or approval reference, without secrets.")
        member = Membership.objects.select_related("user", "organization").filter(
            organization_id=options["company_id"], user__username=options["username"], user__is_active=True).first()
        if member is None:
            raise CommandError("An active account membership for that exact company and username was not found.")
        organizations = list(Membership.all_objects.filter(user=member.user).values_list("organization_id", flat=True).distinct())
        self.stdout.write(f"Target: {member.user.get_username()} | company {member.organization_id}: {member.organization.name}")
        self.stdout.write(f"This resets a GLOBAL account used in {len(organizations)} company membership(s) and invalidates existing sessions.")
        if options["reset_mfa"]:
            self.stdout.write("MFA enrollment and recovery codes will also be cleared. Production requires enrollment again.")
        expected = f"RECOVER {member.organization_id} {member.user.get_username()}"
        confirmation = input(f"After independently verifying identity and approval, type '{expected}': ")
        if confirmation != expected:
            raise CommandError("Recovery cancelled; no account was changed.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("Temporary password (not displayed): ")
                confirmation_password = getpass.getpass("Repeat temporary password: ")
        except (EOFError, getpass.GetPassWarning) as exc:
            raise CommandError("Secure password input is unavailable; no account was changed.") from exc
        if password != confirmation_password:
            raise CommandError("Passwords differ; no account was changed.")
        try:
            validate_password(password, user=member.user)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        with transaction.atomic():
            user = get_user_model().objects.select_for_update().get(pk=member.user_id)
            if (not user.is_active or user.get_username() != options["username"]
                    or not Membership.objects.filter(pk=member.pk, user=user).exists()):
                raise CommandError("Account access changed during verification; recovery cancelled.")
            user.set_password(password)
            user.save(update_fields=["password"])
            security, _ = AccountSecurity.objects.select_for_update().get_or_create(user=user)
            security.require_password_change = True
            security.session_version += 1
            if options["reset_mfa"]:
                security.totp_secret = ""
                security.totp_enabled = False
                security.totp_last_counter = -1
                security.recovery_codes = []
            security.save()
            # A global identity recovery affects all memberships, including
            # historical/revoked ones. Never imply an app owner authenticated it.
            memberships = Membership.all_objects.filter(user=user).select_related("organization")
            for affected in memberships:
                record(affected.organization, None, "account.operator_recovery", obj=affected,
                       detail={"operator_os_user": getpass.getuser(), "reason": reason,
                               "requested_company_id": member.organization_id,
                               "target_user_id": user.pk, "mfa_reset": bool(options["reset_mfa"]),
                               "must_change_password": True, "session_version": security.session_version})
        self.stdout.write(self.style.SUCCESS("Account recovered. Communicate the temporary password through the approved private channel; never include it in the audit log."))
