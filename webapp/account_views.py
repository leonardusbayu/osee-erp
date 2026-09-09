import base64
import io
import time

import pyotp
import qrcode
from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.forms import SetPasswordForm
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from core.access import organization_required, require_role
from core.account_services import update_member_access, reset_member_password
from core.audit import record
from core.models import AccountSecurity, Membership
from core import mfa
from core.throttle import allow_attempt
from .views import _form_page, _form_error


class MemberAccessForm(forms.Form):
    role = forms.ChoiceField(label="Peran dalam tim", choices=[("", "Pertahankan peran saat ini")] + Membership.TEAM_ROLES, required=False)
    is_active = forms.BooleanField(label="Akses perusahaan aktif", required=False)
    reason = forms.CharField(label="Alasan perubahan", max_length=500, widget=forms.Textarea(attrs={"rows": 3}))


class MemberResetForm(SetPasswordForm):
    reason = forms.CharField(label="Alasan pemulihan dan verifikasi identitas", max_length=500, widget=forms.Textarea(attrs={"rows": 3}))


@require_http_methods(["GET", "POST"])
@organization_required
def member_access(request, pk):
    require_role(request, "owner")
    member = get_object_or_404(Membership.all_objects.select_related("user"), pk=pk, organization=request.organization)
    form = MemberAccessForm(request.POST or None, initial={"role": member.role if member.role in dict(Membership.TEAM_ROLES) else "", "is_active": member.is_active})
    if request.method == "POST" and form.is_valid():
        try:
            update_member_access(organization=request.organization, actor=request.user, membership_id=member.pk,
                                 role=form.cleaned_data["role"] or member.role, is_active=form.cleaned_data["is_active"], reason=form.cleaned_data["reason"])
            messages.success(request, "Akses diperbarui. Hak akses baru berlaku pada permintaan berikutnya.")
            return redirect("settings")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, f"Kelola akses: {member.user.get_full_name() or member.user.username}",
                      "Nonaktifkan akses saat anggota keluar dari perusahaan. Riwayat transaksinya tetap disimpan.", "settings", "settings")


@require_http_methods(["GET", "POST"])
@organization_required
def member_password_reset(request, pk):
    require_role(request, "owner")
    member = get_object_or_404(Membership.objects.select_related("user"), pk=pk, organization=request.organization)
    form = MemberResetForm(member.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            reset_member_password(organization=request.organization, actor=request.user, membership_id=member.pk,
                                  password=form.cleaned_data["new_password1"], reason=form.cleaned_data["reason"])
            messages.success(request, "Sandi sementara disimpan. Bagikan melalui saluran internal yang aman. Anggota wajib menggantinya setelah masuk; sesi lama telah dibatalkan. MFA tetap berlaku.")
            return redirect("settings")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Pulihkan kata sandi anggota", "Pastikan identitas anggota terlebih dahulu. Sistem tidak mengirim sandi lewat email; MFA tidak dinonaktifkan.", "settings", "settings", "Simpan sandi sementara")


def _pending_login(request):
    pending = request.session.get("pending_login", {})
    if not pending or time.time() - pending.get("at", 0) > 300:
        request.session.pop("pending_login", None)
        return None
    user = get_user_model().objects.filter(pk=pending.get("user_id"), is_active=True).first()
    if not user or user.get_session_auth_hash() != pending.get("auth_hash") or not Membership.objects.filter(user=user).exists():
        request.session.pop("pending_login", None)
        return None
    return user


@require_http_methods(["GET", "POST"])
def mfa_challenge(request):
    user = _pending_login(request)
    if not user:
        return redirect("login")
    error = ""
    if request.method == "POST":
        if not allow_attempt(f"mfa:{user.pk}", limit=10):
            return render(request, "registration/mfa.html", {"error": "Terlalu banyak percobaan. Coba lagi dalam 15 menit."}, status=429)
        try:
            version = mfa.verify_challenge(user, request.POST.get("code", ""))
        except ValidationError:
            version = False
        if version is not False:
            destination = request.session["pending_login"].get("next", "/")
            if not url_has_allowed_host_and_scheme(destination, {request.get_host()}, require_https=request.is_secure()):
                destination = "/"
            request.session.pop("pending_login", None)
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            request.session["mfa_version"] = version
            request.session["auth_security_version"] = version
            return redirect(destination)
        error = "Kode tidak valid, sudah digunakan, atau kedaluwarsa. Gunakan kode baru atau kode pemulihan."
    return render(request, "registration/mfa.html", {"error": error})


@require_http_methods(["GET", "POST"])
@organization_required
def security_settings(request):
    error = ""
    codes = None
    secret = None
    if request.method == "POST":
        action = request.POST.get("action")
        if not allow_attempt(f"mfa-settings:{request.user.pk}", limit=15):
            error = "Terlalu banyak percobaan. Coba lagi dalam 15 menit."
        else:
            try:
                if action == "begin":
                    if not request.user.check_password(request.POST.get("password", "")):
                        raise ValidationError("Kata sandi saat ini tidak benar.")
                    secret = mfa.begin_enrollment(request.user)
                    request.session["mfa_enrollment_at"] = time.time()
                elif action == "confirm":
                    if time.time() - request.session.get("mfa_enrollment_at", 0) > 600:
                        raise ValidationError("Penyiapan kedaluwarsa. Mulai lagi dengan kata sandi Anda.")
                    codes, version = mfa.confirm_enrollment(request.user, request.POST.get("code", ""))
                    request.session["mfa_version"] = version
                    request.session["auth_security_version"] = version
                    request.session.pop("mfa_enrollment_at", None)
                    record(request.organization, request.user, "account.mfa_enabled")
                elif action == "disable":
                    from django.conf import settings
                    if getattr(settings, "MFA_REQUIRED", False):
                        raise ValidationError("Verifikasi dua langkah diwajibkan untuk lingkungan ini.")
                    if not request.user.check_password(request.POST.get("password", "")):
                        raise ValidationError("Kata sandi saat ini tidak benar.")
                    version = mfa.disable_mfa(request.user, request.POST.get("code", ""))
                    request.session["mfa_version"] = version
                    request.session["auth_security_version"] = version
                    record(request.organization, request.user, "account.mfa_disabled")
                    messages.success(request, "Verifikasi dua langkah dinonaktifkan.")
                    return redirect("security_settings")
                else:
                    raise ValidationError("Tindakan tidak dikenal.")
            except ValidationError as exc:
                error = " ".join(exc.messages)
    security = AccountSecurity.objects.filter(user=request.user).first()
    if not (security and security.totp_enabled) and time.time() - request.session.get("mfa_enrollment_at", 0) < 600:
        secret = secret or mfa.pending_secret(request.user)
    qr = ""
    if secret:
        uri = pyotp.TOTP(secret).provisioning_uri(name=request.user.username, issuer_name="OSEE Workspace")
        output = io.BytesIO()
        qrcode.make(uri).save(output, format="PNG")
        qr = base64.b64encode(output.getvalue()).decode()
    from django.conf import settings
    return render(request, "app/security_settings.html", {"security": security, "secret": secret, "qr": qr, "recovery_codes": codes, "error": error, "mfa_required": getattr(settings, "MFA_REQUIRED", False), "active_nav": "settings"})


urlpatterns = [
    path("settings/team/<int:pk>/access/", member_access, name="member_access"),
    path("settings/team/<int:pk>/password/", member_password_reset, name="member_password_reset"),
    path("settings/security/", security_settings, name="security_settings"),
    path("login/verify/", mfa_challenge, name="mfa_challenge"),
]
