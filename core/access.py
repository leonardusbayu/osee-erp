from functools import wraps
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from .models import Membership

def organization_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        memberships = Membership.objects.select_related("organization").filter(user=request.user)
        selected = request.session.get("organization_id")
        membership = memberships.filter(organization_id=selected).first() if selected else memberships.first()
        if not membership:
            raise PermissionDenied("Akun ini belum memiliki akses perusahaan.")
        request.organization = membership.organization
        request.membership = membership
        # Marketing accounts are deliberately outside the Finance/Tax workspace.
        # Director and Marketing routes apply their own page/action permissions.
        match = request.resolver_match
        if membership.role == "marketing" and match and match.namespace not in {"director", "marketing"} and match.url_name not in {"modules", "password_change"}:
            raise PermissionDenied("Akun marketing tidak memiliki akses ke workspace keuangan.")
        return view(request, *args, **kwargs)
    return wrapped

def require_role(request, *roles):
    if request.membership.role not in roles:
        raise PermissionDenied("Peran akun Anda tidak dapat melakukan tindakan ini.")
