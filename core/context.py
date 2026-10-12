from django.conf import settings

from .modules import MODULES
from .workspaces import owner_setup_needed


def app_context(request):
    organization = getattr(request, "organization", None)
    membership = getattr(request, "membership", None)
    role = membership.role if membership else None
    can_director = role in {"owner", "director", "finance", "auditor"}
    can_marketing = can_director or role == "marketing"
    home_module = {
        "owner": "director",
        "director": "director",
        "marketing": "marketing",
        "operations": "test_operations",
    }.get(role, "finance")
    match = request.resolver_match
    is_director = bool(
        match
        and (
            match.namespace == "director"
            or (match.url_name == "modules" and home_module == "director")
        )
    )
    is_marketing = role == "marketing" or bool(match and match.namespace == "marketing")
    is_operations = role == "operations" or bool(
        match and match.namespace == "operations"
    )
    modules = tuple(
        module
        for module in MODULES
        if (module.key != "director" or can_director)
        and (module.key != "finance" or role not in {"marketing", "operations"})
        and (module.key != "marketing" or can_marketing)
        and (
            module.key != "test_operations"
            or role in {"owner", "finance", "operations"}
        )
        and (module.key != "people" or role in {"owner", "director", "finance"})
    )
    modules = tuple(sorted(modules, key=lambda module: module.key != home_module))
    available_modules = tuple(module for module in modules if module.available)
    primary_module = next(
        (module for module in available_modules if module.key == home_module), None
    )
    return {
        "organization": organization,
        "membership": getattr(request, "membership", None),
        "module_registry": modules,
        "home_module": home_module,
        "primary_module": primary_module,
        "other_modules": tuple(
            module for module in available_modules if module.key != home_module
        ),
        "planned_modules": tuple(module for module in modules if not module.available),
        "is_director_workspace": is_director,
        "is_marketing_workspace": is_marketing,
        "is_operations_workspace": is_operations,
        "can_director": can_director,
        "can_marketing": can_marketing,
        "demo_mode": bool(organization.is_demo) if organization else settings.DEMO_MODE,
        "local_setup_enabled": settings.LOCAL_SETUP_ENABLED and owner_setup_needed(),
        "real_import_available": bool(
            organization
            and not organization.is_demo
            and organization.import_batches.exists()
        ),
        "can_write": bool(
            getattr(request, "membership", None)
            and request.membership.role in {"owner", "finance"}
        ),
    }
