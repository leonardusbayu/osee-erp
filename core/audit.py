from .models import AuditEvent

def record(organization, actor, action, obj=None, detail=None):
    return AuditEvent.objects.create(
        organization=organization, actor=actor, action=action,
        object_type=type(obj).__name__ if obj is not None else "",
        object_id=str(obj.pk) if obj is not None else "", detail=detail or {},
    )
