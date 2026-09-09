import hashlib
import ipaddress
import unicodedata
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


def _login_identities(username, remote_addr):
    # Never consume a client-controlled X-Forwarded-For here. The deployment's
    # explicitly trusted proxy normalizes REMOTE_ADDR before Django receives it.
    username = unicodedata.normalize("NFKC", str(username or "")).strip().casefold()[:150]
    try:
        address = str(ipaddress.ip_address(str(remote_addr)))
    except ValueError:
        address = "unknown-peer"
    return (f"login-pair:{address}:{username}", f"login-account:{username}", f"login-ip:{address}")


@transaction.atomic
def allow_login_attempt(*, username, remote_addr):
    """Reserve one attempt; ordinary shared-office traffic has its own quota.

    Account-wide quota limits distributed guessing; the tighter pair quota
    contains one attack without blocking unrelated colleagues on the same IP.
    Uniform return values do not disclose whether the username exists.
    """
    pair, account, address = _login_identities(username, remote_addr)
    # Fixed lock order across all requests, with shared IP always first.
    return (allow_attempt(address, limit=300, seconds=900)
            and allow_attempt(account, limit=50, seconds=900)
            and allow_attempt(pair, limit=10, seconds=900))


@transaction.atomic
def reset_login_attempts(*, username, remote_addr):
    """Successful complete authentication clears account/pair, not IP traffic."""
    pair, account, _ = _login_identities(username, remote_addr)
    keys = [hashlib.sha256(value.encode()).hexdigest() for value in (pair, account)]
    AccessThrottle.objects.filter(key__in=keys).delete()
