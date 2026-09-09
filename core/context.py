from django.conf import settings
from .modules import MODULES
from .workspaces import owner_setup_needed

def app_context(request):
    organization = getattr(request, "organization", None)
    membership = getattr(request, "membership", None)
    role = membership.role if membership else None
    can_director = role in {"owner", "director", "finance", "auditor"}
    can_marketing = can_director or role == "marketing"
    is_director = bool(request.resolver_match and request.resolver_match.namespace == "director")
    is_marketing = role == "marketing" or bool(request.resolver_match and request.resolver_match.namespace == "marketing")
    modules = tuple(module for module in MODULES if (module.key != "director" or can_director) and (module.key != "finance" or role != "marketing") and (module.key != "marketing" or can_marketing))
    return {
        "organization": organization,
        "membership": getattr(request, "membership", None),
        "module_registry": modules,
        "is_director_workspace": is_director,
        "is_marketing_workspace": is_marketing,
        "can_director": can_director,
        "can_marketing": can_marketing,
        "demo_mode": bool(organization.is_demo) if organization else settings.DEMO_MODE,
        "local_setup_enabled": settings.LOCAL_SETUP_ENABLED and owner_setup_needed(),
        "real_import_available": bool(organization and not organization.is_demo and organization.import_batches.exists()),
        "can_write": bool(getattr(request, "membership", None) and request.membership.role in {"owner", "finance"}),
    }
