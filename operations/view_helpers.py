from django.shortcuts import render

from core.access import require_role


def _access(request):
    require_role(request, "owner", "finance", "operations")


def _finance_access(request):
    require_role(request, "owner", "finance")


def _form_page(request, form, title, cancel, description="", action="Simpan"):
    return render(
        request,
        "app/form.html",
        {
            "form": form,
            "title": title,
            "cancel_url": cancel,
            "description": description,
            "action_label": action,
            "active_nav": "operations",
        },
    )


def _error(form, exc):
    for message in exc.messages:
        form.add_error(None, message)
