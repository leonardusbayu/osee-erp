"""TOTP enrollment, replay protection and one-use recovery codes."""
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
import pyotp
from django.conf import settings
from django.contrib.auth.hashers import make_password, check_password
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction

from .models import AccountSecurity, Membership


def _active_user(user):
    if not Membership.objects.filter(user_id=user.pk, user__is_active=True).exists():
        raise ValidationError("Akun tidak memiliki akses perusahaan aktif.")


def _cipher():
    key = getattr(settings, "OTP_ENCRYPTION_KEY", "")
    if not key:
        if not settings.DEBUG:
            raise ImproperlyConfigured("Configure the MFA encryption key before using production.")
        path = Path(settings.BASE_DIR) / ".local/mfa-encryption.key"
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            try:
                with path.open("xb") as stream:
                    stream.write(Fernet.generate_key())
                path.chmod(0o600)
            except FileExistsError:
                pass
        key = path.read_bytes().strip()
    return Fernet(key.encode() if isinstance(key, str) else key)


def _secret(record):
    try:
        return _cipher().decrypt(record.totp_secret.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise ValidationError("Kunci verifikasi tidak dapat dibaca. Hubungi administrator deployment.") from exc


@transaction.atomic
def begin_enrollment(user):
    _active_user(user)
    record, _ = AccountSecurity.objects.select_for_update().get_or_create(user=user)
    if record.totp_enabled:
        raise ValidationError("Verifikasi dua langkah sudah aktif.")
    secret = pyotp.random_base32()
    record.totp_secret = _cipher().encrypt(secret.encode()).decode()
    record.totp_last_counter = -1
    record.save()
    return secret


def pending_secret(user):
    record = AccountSecurity.objects.filter(user=user, totp_enabled=False).first()
    return _secret(record) if record and record.totp_secret else None


def _accept_totp(record, code, now=None):
    now = time.time() if now is None else now
    totp = pyotp.TOTP(_secret(record))
    counter = int(now // totp.interval)
    for candidate in [counter, counter - 1, counter + 1]:
        if candidate > record.totp_last_counter and secrets.compare_digest(totp.at(candidate * totp.interval), str(code).strip()):
            record.totp_last_counter = candidate
            return True
    return False


@transaction.atomic
def confirm_enrollment(user, code):
    _active_user(user)
    record = AccountSecurity.objects.select_for_update().get(user=user)
    if record.totp_enabled or not record.totp_secret or not _accept_totp(record, code):
        raise ValidationError("Kode tidak valid atau sudah dipakai. Tunggu kode baru lalu coba lagi.")
    codes = [secrets.token_hex(6) for _ in range(8)]
    record.recovery_codes = [make_password(code) for code in codes]
    record.totp_enabled = True
    record.session_version += 1
    record.save()
    return codes, record.session_version


@transaction.atomic
def verify_challenge(user, code):
    if not isinstance(code, str) or len(code) > 20:
        return False
    if not Membership.objects.filter(user_id=user.pk, user__is_active=True).exists():
        return False
    record = AccountSecurity.objects.select_for_update().filter(user=user, user__is_active=True, totp_enabled=True).first()
    if not record:
        return False
    accepted = _accept_totp(record, code)
    if not accepted:
        for index, hashed in enumerate(record.recovery_codes):
            if check_password(str(code).strip().lower(), hashed):
                record.recovery_codes = record.recovery_codes[:index] + record.recovery_codes[index + 1:]
                accepted = True
                break
    if accepted:
        record.save()
        return record.session_version
    return False


@transaction.atomic
def disable_mfa(user, code):
    _active_user(user)
    record = AccountSecurity.objects.select_for_update().get(user=user)
    if not record.totp_enabled or verify_challenge(user, code) is False:
        raise ValidationError("Kode verifikasi atau pemulihan tidak valid.")
    record.refresh_from_db()
    record.totp_secret = ""
    record.totp_enabled = False
    record.recovery_codes = []
    record.totp_last_counter = -1
    record.session_version += 1
    record.save()
    return record.session_version


class AccountSecurityMiddleware:
    """Invalidate revoked MFA sessions and enforce recovered-password rotation."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.contrib.auth import logout
        from django.shortcuts import redirect
        if request.user.is_authenticated:
            security = AccountSecurity.objects.filter(user=request.user).first()
            if security:
                if request.session.get("auth_security_version", 1) != security.session_version:
                    logout(request)
                    return redirect("login")
                if security.totp_enabled and request.session.get("mfa_version") != security.session_version:
                    logout(request)
                    return redirect("login")
                if security.require_password_change and request.path not in {"/settings/password/", "/logout/"}:
                    return redirect("password_change")
            if getattr(settings, "MFA_REQUIRED", False) and not (security and security.totp_enabled):
                if request.path not in {"/settings/security/", "/settings/password/", "/logout/"}:
                    return redirect("security_settings")
        return self.get_response(request)
