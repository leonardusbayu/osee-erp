"""Local migration/setup gates; demo access never opens real company data."""
from .models import Membership, Organization


def real_import_exists():
    from imports.models import ImportBatch
    return ImportBatch.objects.filter(organization__is_demo=False).exists()


def pending_company():
    candidates = Organization.objects.filter(is_demo=False, import_batches__isnull=False).exclude(
        pk__in=Membership.objects.values("organization_id")
    ).distinct()
    return candidates.first() if candidates.count() == 1 else None


def owner_setup_needed():
    return not Membership.objects.filter(organization__is_demo=False, role="owner").exists()
