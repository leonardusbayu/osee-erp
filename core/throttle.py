import hashlib
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import AccessThrottle

@transaction.atomic
def allow_attempt(identity, limit=20, seconds=900):
    key = hashlib.sha256(identity.encode()).hexdigest()
    now = timezone.now()
    row, _ = AccessThrottle.objects.get_or_create(key=key, defaults={"window_start": now})
    row = AccessThrottle.objects.select_for_update().get(pk=row.pk)
    if now - row.window_start >= timedelta(seconds=seconds):
        row.window_start = now
        row.attempts = 0
    if row.attempts >= limit:
        return False
    row.attempts += 1
    row.save(update_fields=["window_start", "attempts"])
    return True
