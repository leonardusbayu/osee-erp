from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from core.access import organization_required
from core.audit import record
from .models import Attachment


@require_GET
@organization_required
def download(request, pk):
    attachment = get_object_or_404(Attachment.objects.select_related("invoice", "bill"),
                                   organization=request.organization, pk=pk)
    source = attachment.invoice if attachment.invoice_id else attachment.bill
    if source is None or source.organization_id != request.organization.pk:
        raise Http404
    try:
        handle = attachment.file.open("rb")
    except FileNotFoundError as exc:
        raise Http404("Berkas dokumen tidak tersedia.") from exc
    try:
        record(request.organization, request.user, "evidence.document.downloaded", obj=attachment)
    except Exception:
        handle.close()
        raise
    response = FileResponse(handle, as_attachment=True, filename=attachment.original_name, content_type=attachment.content_type)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response
