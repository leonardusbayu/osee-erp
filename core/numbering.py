from django.db import transaction
from .models import DocumentCounter

@transaction.atomic
def next_number(organization, kind, year):
    counter, _ = DocumentCounter.objects.get_or_create(organization=organization, kind=kind, year=year)
    counter = DocumentCounter.objects.select_for_update().get(pk=counter.pk)
    counter.value += 1
    counter.save(update_fields=["value"])
    return f"{kind}-{year}-{counter.value:05d}"
