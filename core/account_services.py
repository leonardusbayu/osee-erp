"""Company access changes and account recovery, with active-owner authorization."""
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .audit import record
from .models import AccountSecurity, Membership, Organization


def authorize_owner(organization, actor):
    if not Membership.objects.filter(organization=organization, user_id=actor.pk, user__is_active=True, role="owner").exists():
        raise PermissionDenied("Hanya Owner/Direktur aktif yang dapat mengelola akses tim.")


@transaction.atomic
def update_member_access(*, organization, actor, membership_id, role, is_active, reason):
    Organization.objects.select_for_update().get(pk=organization.pk)
    authorize_owner(organization, actor)
    target = Membership.all_objects.select_for_update().select_related("user").get(pk=membership_id, organization=organization)
    if (role not in dict(Membership.TEAM_ROLES) and role != target.role) or not isinstance(is_active, bool) or not reason.strip():
        raise ValidationError("Pilih peran tim yang tersedia dan isi alasan perubahan.")
    if target.user_id == actor.pk and (not is_active or role != "owner"):
        raise ValidationError("Anda tidak dapat mencabut atau menurunkan akses Anda sendiri.")
    if target.role == "owner" and target.is_active and (not is_active or role != "owner"):
        if not Membership.objects.filter(organization=organization, role="owner", user__is_active=True).exclude(pk=target.pk).exists():
            raise ValidationError("Perusahaan harus memiliki setidaknya satu Owner/Direktur aktif.")
    before = {"role": target.role, "is_active": target.is_active}
    target.role, target.is_active = role, is_active
    target.save(update_fields=["role", "is_active"])
    record(organization, actor, "organization.member.access_updated", obj=target,
           detail={"before": before, "after": {"role": role, "is_active": is_active}, "reason": reason.strip()[:500]})
    return target


@transaction.atomic
def reset_member_password(*, organization, actor, membership_id, password, reason):
    Organization.objects.select_for_update().get(pk=organization.pk)
    authorize_owner(organization, actor)
    member = Membership.objects.select_for_update().get(pk=membership_id, organization=organization)
    if member.user_id == actor.pk:
        raise ValidationError("Gunakan menu Ubah kata sandi untuk akun Anda sendiri.")
    if Membership.objects.filter(user_id=member.user_id).exclude(organization=organization).exists():
        raise ValidationError("Akun ini digunakan di perusahaan lain. Gunakan pemulihan akun oleh administrator deployment.")
    if not reason.strip():
        raise ValidationError("Catat alasan pemulihan dan cara Anda memastikan identitas pemilik akun.")
    user = get_user_model().objects.select_for_update().get(pk=member.user_id, is_active=True)
    validate_password(password, user=user)
    user.set_password(password)
    user.save(update_fields=["password"])
    security, _ = AccountSecurity.objects.select_for_update().get_or_create(user=user)
    security.require_password_change = True
    security.session_version += 1
    security.save()
    record(organization, actor, "account.password_reset", obj=member, detail={"reason": reason.strip()[:500], "must_change_password": True})
    return member
