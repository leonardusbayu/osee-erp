import hashlib
import json
from django.db import migrations


def preserve_sources(apps, schema_editor):
    Source = apps.get_model("taxes", "TaxSource")
    Revision = apps.get_model("taxes", "TaxSourceRevision")
    database = schema_editor.connection.alias
    for source in Source.objects.using(database).all():
        data = {name: getattr(source, name) for name in ("slug", "title", "url", "issuer", "article", "summary", "topics", "reviewed_on", "effective_from", "effective_until")}
        for name in ("reviewed_on", "effective_from", "effective_until"):
            data[name] = data[name].isoformat() if data[name] else None
        content_hash = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        Revision.objects.using(database).get_or_create(source=source, content_hash=content_hash, defaults={"snapshot": data})
    # Existing approval flags are preserved exactly; no source or company is approved here.


class Migration(migrations.Migration):
    dependencies = [("taxes", "0004_source_review_publication")]
    operations = [migrations.RunPython(preserve_sources, migrations.RunPython.noop)]
