from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from taxes.models import TaxSourceRevision
from taxes.services import seed_tax_sources
from taxes.knowledge import activate_source_revision


class Command(BaseCommand):
    help = "Stage tax knowledge or explicitly publish one reviewed revision. Never activates company tax rules."

    def add_arguments(self, parser):
        parser.add_argument("--stage", action="store_true")
        parser.add_argument("--activate-revision", type=int)
        parser.add_argument("--reviewer-name")
        parser.add_argument("--review-reference")
        parser.add_argument("--confirm-reviewed", action="store_true")

    def handle(self, *args, **options):
        if options["stage"]:
            seed_tax_sources()
        if options["activate_revision"]:
            try:
                revision = TaxSourceRevision.objects.get(pk=options["activate_revision"])
                activate_source_revision(revision=revision, reviewer_name=options["reviewer_name"],
                    review_reference=options["review_reference"], confirm_reviewed=options["confirm_reviewed"])
            except (TaxSourceRevision.DoesNotExist, ValidationError) as exc:
                raise CommandError(str(exc)) from None
            self.stdout.write(self.style.SUCCESS("Recorded explicit knowledge publication; company tax profiles unchanged."))
        for revision in TaxSourceRevision.objects.select_related("source").order_by("source__slug", "-pk"):
            self.stdout.write(f"{revision.pk}: {revision.source.slug} | {revision.snapshot.get('reviewed_on')} | {revision.content_hash[:12]}")
