"""Persisted source recovery, document replay and immutable history contracts."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from core.models import AuditEvent, Membership, Organization
from finance.models import BankTransaction, Invoice, Journal

from . import ad_recovery, services
from .imports import import_ad_csv
from .models import AdDailyObservation, AdObservationRecovery
from .tests_import import csv_file


class AdObservationRecoveryTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic ad recovery")
        self.other = Organization.objects.create(name="Foreign ad recovery")
        for role in ("owner", "director", "marketing", "finance", "auditor"):
            user = get_user_model().objects.create_user(username=f"ad-recovery-{role}")
            Membership.objects.create(organization=self.org, user=user, role=role)
            setattr(self, role, user)
        self.foreign_actor = get_user_model().objects.create_user(
            username="ad-recovery-foreign"
        )
        Membership.objects.create(
            organization=self.other, user=self.foreign_actor, role="owner"
        )
        self.day = timezone.localdate() - timedelta(days=2)
        self.campaign = services.create_campaign(
            organization=self.org,
            actor=self.marketing,
            name="Original campaign",
            channel="meta_ads",
        )
        self.target_campaign = services.create_campaign(
            organization=self.org,
            actor=self.marketing,
            name="Correct campaign",
            channel="google_ads",
        )
        self.ad = services.save_ad_observation(
            organization=self.org,
            actor=self.marketing,
            campaign=self.campaign,
            date=self.day,
            spend="100",
            impressions=1000,
            clicks=10,
            source="Synthetic platform report",
            reference="DOC-AD-ORIGINAL",
        )

    def values(self, observation=None, **changes):
        row = observation or self.ad
        return {
            "organization": self.org,
            "actor": self.marketing,
            "observation": row,
            "expected_version": row.version,
            "expected_fingerprint": ad_recovery.observation_fingerprint(row),
            "request_key": "RECOVERY-1",
            "reason": "Dokumen sumber diperiksa kembali",
            "evidence": "Arsip uji/sumber-asli.csv",
            **changes,
        }

    def finance_counts(self):
        return (
            Journal.objects.count(),
            BankTransaction.objects.count(),
            Invoice.objects.count(),
        )

    def test_withdraw_retains_original_facts_creator_and_finance_without_zero_replacement(
        self,
    ):
        created_at, created_by = self.ad.created_at, self.ad.created_by_id
        original = services._snapshot(self.ad, services.AD_FIELDS)
        finance_before = self.finance_counts()
        decision = ad_recovery.withdraw_ad_observation(**self.values())
        self.ad.refresh_from_db()
        self.assertEqual(services._snapshot(self.ad, services.AD_FIELDS), original)
        self.assertEqual(
            (self.ad.created_at, self.ad.created_by_id, self.ad.version),
            (created_at, created_by, 2),
        )
        self.assertIsNotNone(self.ad.withdrawn_at)
        self.assertEqual(
            AdDailyObservation.objects.filter(withdrawn_at__isnull=True).count(), 0
        )
        self.assertEqual(
            (decision.predecessor_id, decision.successor_id, decision.action),
            (self.ad.pk, None, "withdraw"),
        )
        self.assertEqual(decision.created_by_id, self.marketing.pk)
        event = AuditEvent.objects.get(action="marketing.ad.withdrawn")
        self.assertEqual(event.detail["before"]["spend"], "100.00")
        self.assertIsNone(event.detail["after"])
        self.assertEqual(self.finance_counts(), finance_before)

    def test_replace_campaign_date_and_amount_keeps_document_identity_and_only_one_active_fact(
        self,
    ):
        original = services._snapshot(self.ad, services.AD_FIELDS)
        finance_before = self.finance_counts()
        decision = ad_recovery.replace_ad_observation(
            **self.values(),
            campaign=self.target_campaign,
            date=self.day - timedelta(days=1),
            spend="125",
            source="Correct source",
        )
        self.ad.refresh_from_db()
        new = AdDailyObservation.objects.get(pk=decision.successor_id)
        self.assertEqual(services._snapshot(self.ad, services.AD_FIELDS), original)
        self.assertEqual(
            (new.campaign_id, new.date, new.spend),
            (self.target_campaign.pk, self.day - timedelta(days=1), Decimal(125)),
        )
        self.assertEqual(
            (new.reference, new.source_root_id, new.replaces_id, new.version),
            (self.ad.reference, self.ad.pk, self.ad.pk, 1),
        )
        self.assertEqual(
            list(
                AdDailyObservation.objects.filter(
                    withdrawn_at__isnull=True
                ).values_list("pk", flat=True)
            ),
            [new.pk],
        )
        self.assertEqual(self.finance_counts(), finance_before)
        self.assertEqual(
            AuditEvent.objects.filter(action="marketing.ad.replaced").count(), 1
        )

    def test_successive_replacements_keep_one_root_and_immutable_predecessor_chain(
        self,
    ):
        first = ad_recovery.replace_ad_observation(**self.values(), spend="110")
        second = ad_recovery.replace_ad_observation(
            **self.values(first.successor, request_key="RECOVERY-2"), spend="120"
        )
        third = second.successor
        first.refresh_from_db()
        self.assertEqual(first.successor_id, second.predecessor_id)
        self.assertEqual(
            (third.source_root_id, third.replaces_id, third.reference),
            (self.ad.pk, first.successor_id, self.ad.reference),
        )
        self.assertEqual(
            AdDailyObservation.objects.filter(
                source_root__isnull=True, reference=self.ad.reference
            ).count(),
            1,
        )
        self.assertEqual(
            AdDailyObservation.objects.filter(withdrawn_at__isnull=True).count(), 1
        )
        self.assertEqual(AdObservationRecovery.objects.count(), 2)
        self.assertEqual(
            AdDailyObservation.objects.get(pk=self.ad.pk).spend, Decimal(100)
        )
        self.assertEqual(
            AdDailyObservation.objects.get(pk=first.successor_id).spend, Decimal(110)
        )

    def test_identical_retry_returns_same_decision_before_stale_guards_without_more_audit(
        self,
    ):
        request = self.values()
        decision = ad_recovery.replace_ad_observation(**request, spend="105")
        audits = AuditEvent.objects.count()
        replay = ad_recovery.replace_ad_observation(**request, spend="105.00")
        self.assertEqual(replay.pk, decision.pk)
        self.assertEqual(
            (
                AdDailyObservation.objects.count(),
                AdObservationRecovery.objects.count(),
                AuditEvent.objects.count(),
            ),
            (2, 1, audits),
        )
        for changed in (
            {"evidence": "Different evidence"},
            {"actor": self.owner},
            {"reason": "Different reason"},
            {"expected_version": 2},
            {"expected_fingerprint": "0" * 64},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                ad_recovery.replace_ad_observation(
                    **{**request, **changed}, spend="105"
                )
        with self.assertRaises(ValidationError):
            ad_recovery.withdraw_ad_observation(**request)

    def test_original_exact_request_still_replays_after_later_successor_recovery(self):
        request = self.values()
        first = ad_recovery.replace_ad_observation(**request, spend="110")
        ad_recovery.replace_ad_observation(
            **self.values(first.successor, request_key="NEXT"), spend="120"
        )
        audits = AuditEvent.objects.count()
        retry = ad_recovery.replace_ad_observation(**request, spend="110")
        self.assertEqual(retry.pk, first.pk)
        self.assertEqual(AdObservationRecovery.objects.count(), 2)
        self.assertEqual(AuditEvent.objects.count(), audits)

    def test_new_request_cannot_archive_or_replace_an_already_archived_predecessor(
        self,
    ):
        ad_recovery.withdraw_ad_observation(**self.values())
        audits = AuditEvent.objects.count()
        self.ad.refresh_from_db()
        for operation in (
            ad_recovery.withdraw_ad_observation,
            ad_recovery.replace_ad_observation,
        ):
            with (
                self.subTest(operation=operation.__name__),
                self.assertRaises(ValidationError),
            ):
                operation(**self.values(request_key="OTHER-REQUEST"))
        self.assertEqual(
            (
                AdDailyObservation.objects.count(),
                AdObservationRecovery.objects.count(),
                AuditEvent.objects.count(),
            ),
            (1, 1, audits),
        )

    def test_stale_version_and_source_fingerprint_do_not_change_any_fact(self):
        old_fingerprint = ad_recovery.observation_fingerprint(self.ad)
        current = services.save_ad_observation(
            organization=self.org,
            actor=self.marketing,
            observation=self.ad,
            expected_version=1,
            spend="101",
            reason="Angka sumber diperiksa",
        )
        audits = AuditEvent.objects.count()
        for changes in (
            {"expected_version": 1},
            {"expected_fingerprint": old_fingerprint},
            {"expected_version": True},
            {"expected_fingerprint": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ad_recovery.withdraw_ad_observation(**self.values(current, **changes))
        current.refresh_from_db()
        self.assertEqual(
            (current.version, current.spend, current.withdrawn_at),
            (2, Decimal(101), None),
        )
        self.assertFalse(AdObservationRecovery.objects.exists())
        self.assertEqual(AuditEvent.objects.count(), audits)

    def test_archive_cannot_be_numerically_corrected_or_mutated_by_direct_save_and_flags_expire(
        self,
    ):
        decision = ad_recovery.withdraw_ad_observation(**self.values())
        stored = decision.predecessor
        self.assertNotIn("_service_write", stored.__dict__)
        self.assertNotIn("_recovery_write", stored.__dict__)
        with self.assertRaises(ValidationError):
            services.save_ad_observation(
                organization=self.org,
                actor=self.owner,
                observation=stored,
                expected_version=2,
                reason="Mencoba koreksi arsip",
                spend="0",
            )
        stored.spend = Decimal(0)
        stored._service_write = True
        with self.assertRaises(ValidationError):
            stored.save()
        self.assertNotIn("_service_write", stored.__dict__)
        stored.refresh_from_db()
        self.assertEqual(stored.spend, Decimal(100))

    def test_active_roles_scope_and_inactive_accounts_are_enforced_even_for_retries(
        self,
    ):
        request = self.values()
        for actor in (self.auditor, self.foreign_actor, None):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                ad_recovery.withdraw_ad_observation(**{**request, "actor": actor})
        with self.assertRaises(ValidationError):
            ad_recovery.withdraw_ad_observation(
                **{**request, "organization": self.other, "actor": self.foreign_actor}
            )
        with self.assertRaises(ValidationError):
            ad_recovery.replace_ad_observation(
                **request,
                campaign=services.create_campaign(
                    organization=self.other,
                    actor=self.foreign_actor,
                    name="Foreign",
                    channel="seo",
                ),
            )
        decision = ad_recovery.withdraw_ad_observation(**request)
        self.marketing.is_active = False
        self.marketing.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            ad_recovery.withdraw_ad_observation(**request)
        self.assertEqual(AdObservationRecovery.objects.get().pk, decision.pk)

    def test_all_allowed_writer_roles_can_recover_scoped_sources(self):
        for index, actor in enumerate(
            (self.owner, self.director, self.finance, self.marketing)
        ):
            row = services.save_ad_observation(
                organization=self.org,
                actor=self.marketing,
                campaign=self.campaign,
                date=self.day - timedelta(days=index + 1),
                spend="0",
                source="Bukti nol",
                reference=f"ROLE-DOC-{index}",
            )
            decision = ad_recovery.withdraw_ad_observation(
                **self.values(row, actor=actor, request_key=f"ROLE-REQ-{index}")
            )
            self.assertEqual(decision.created_by_id, actor.pk)

    def test_invalid_request_text_reference_and_payload_are_rejected_without_archival(
        self,
    ):
        for changes in (
            {"request_key": ""},
            {"request_key": "x" * 161},
            {"reason": ""},
            {"reason": "Bad\nreason"},
            {"evidence": ""},
            {"evidence": "x" * 4001},
            {"evidence": "Bad\x00evidence"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ad_recovery.withdraw_ad_observation(**self.values(**changes))
        for fields in (
            {"reference": "FAKE-DOC"},
            {"campaign": None},
            {"spend": True},
            {"spend": "NaN"},
            {"date": "2026-02-31"},
            {"clicks": -1},
            {"organization_id": self.other.pk},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                ad_recovery.replace_ad_observation(**self.values(), **fields)
        self.ad.refresh_from_db()
        self.assertEqual((self.ad.version, self.ad.withdrawn_at), (1, None))
        self.assertFalse(AdObservationRecovery.objects.exists())

    def test_conflicting_successor_rolls_back_archive_version_event_and_audit(self):
        services.save_ad_observation(
            organization=self.org,
            actor=self.marketing,
            campaign=self.target_campaign,
            date=self.day,
            spend="50",
            source="Other total",
            reference="OTHER-TOTAL",
        )
        before_audits = AuditEvent.objects.count()
        with self.assertRaises(ValidationError):
            ad_recovery.replace_ad_observation(
                **self.values(), campaign=self.target_campaign, ad_name="Specific ad"
            )
        self.ad.refresh_from_db()
        self.assertEqual((self.ad.version, self.ad.withdrawn_at), (1, None))
        self.assertEqual(AdDailyObservation.objects.count(), 2)
        self.assertFalse(AdObservationRecovery.objects.exists())
        self.assertEqual(AuditEvent.objects.count(), before_audits)

    def test_total_can_be_replaced_by_detail_and_old_total_does_not_overlap_other_detail(
        self,
    ):
        decision = ad_recovery.replace_ad_observation(
            **self.values(), ad_name="Ad A", spend="60"
        )
        other = services.save_ad_observation(
            organization=self.org,
            actor=self.marketing,
            campaign=self.campaign,
            date=self.day,
            ad_name="Ad B",
            spend="40",
            source="Detail B",
            reference="DETAIL-B",
        )
        active = AdDailyObservation.objects.filter(
            organization=self.org, withdrawn_at__isnull=True
        )
        self.assertCountEqual(
            active.values_list("pk", flat=True), [decision.successor_id, other.pk]
        )
        self.assertEqual(sum(active.values_list("spend", flat=True)), Decimal(100))
        with self.assertRaises(ValidationError):
            services.save_ad_observation(
                organization=self.org,
                actor=self.marketing,
                campaign=self.campaign,
                date=self.day,
                spend="100",
                source="Overlapping total",
                reference="NEW-TOTAL",
            )

    def test_unknown_and_explicit_zero_replacement_values_remain_distinct(self):
        decision = ad_recovery.replace_ad_observation(
            **self.values(), spend=None, impressions=0, clicks=None
        )
        self.assertEqual(
            (
                decision.successor.spend,
                decision.successor.impressions,
                decision.successor.clicks,
            ),
            (None, 0, None),
        )
        with self.assertRaises(ValidationError):
            ad_recovery.replace_ad_observation(
                **self.values(decision.successor, request_key="EMPTY"),
                spend=None,
                impressions=None,
                clicks=None,
            )
        decision.successor.refresh_from_db()
        self.assertIsNone(decision.successor.withdrawn_at)

    def test_audit_failure_rolls_back_both_sides_of_replacement_and_request(self):
        audits = AuditEvent.objects.count()
        with (
            patch(
                "marketing.ad_recovery.services._audit",
                side_effect=ValidationError("Audit unavailable"),
            ),
            self.assertRaises(ValidationError),
        ):
            ad_recovery.replace_ad_observation(**self.values(), spend="110")
        self.ad.refresh_from_db()
        self.assertEqual((self.ad.version, self.ad.withdrawn_at), (1, None))
        self.assertEqual(AdDailyObservation.objects.count(), 1)
        self.assertFalse(AdObservationRecovery.objects.exists())
        self.assertEqual(AuditEvent.objects.count(), audits)

    def test_recovery_event_and_chain_metadata_are_immutable_and_no_bulk_bypass_exists(
        self,
    ):
        decision = ad_recovery.replace_ad_observation(**self.values(), spend="110")
        for obj in (decision, decision.successor):
            self.assertNotIn("_service_write", obj.__dict__)
            self.assertNotIn("_recovery_write", obj.__dict__)
        decision.reason = "Rewritten decision"
        decision._service_write = decision._recovery_write = True
        with self.assertRaises(ValidationError):
            decision.save()
        self.assertNotIn("_recovery_write", decision.__dict__)
        self.assertEqual(
            AdObservationRecovery.objects.get().reason,
            "Dokumen sumber diperiksa kembali",
        )
        with self.assertRaises(ValidationError):
            decision.delete()
        for queryset in (
            AdObservationRecovery.objects.all(),
            AdDailyObservation.objects.all(),
        ):
            with self.assertRaises(ValidationError):
                queryset.update(created_at=timezone.now())
            with self.assertRaises(ValidationError):
                queryset.delete()
        new = decision.successor
        new.reference = "FAKE-REFERENCE"
        with self.assertRaises(ValidationError):
            services._save(new)
        self.assertNotIn("_service_write", new.__dict__)
        new.refresh_from_db()
        self.assertEqual(new.reference, self.ad.reference)

    def csv_row(self, reference=None, spend="100"):
        return [
            str(self.day),
            "",
            "",
            "",
            spend,
            "1000",
            "10",
            "Synthetic platform report",
            reference or self.ad.reference,
        ]

    def ingest(self, *rows):
        return import_ad_csv(
            organization=self.org,
            actor=self.marketing,
            campaign=self.campaign,
            content=csv_file(*rows),
        )

    def test_old_csv_replay_returns_archived_original_without_resurrection_or_new_successor(
        self,
    ):
        decision = ad_recovery.replace_ad_observation(
            **self.values(), spend="110", campaign=self.target_campaign
        )
        audits = AuditEvent.objects.count()
        self.assertEqual(
            self.ingest(self.csv_row()), {"created": 0, "duplicates": 1, "rows": 1}
        )
        self.assertEqual(
            (
                AdDailyObservation.objects.count(),
                AdObservationRecovery.objects.count(),
                AuditEvent.objects.count(),
            ),
            (2, 1, audits),
        )
        self.assertEqual(
            AdDailyObservation.objects.filter(withdrawn_at__isnull=True).get().pk,
            decision.successor_id,
        )
        self.assertIsNotNone(AdDailyObservation.objects.get(pk=self.ad.pk).withdrawn_at)

    def test_changed_archived_csv_document_rejects_whole_import_without_new_audits(
        self,
    ):
        ad_recovery.withdraw_ad_observation(**self.values())
        audits = AuditEvent.objects.count()
        new = self.csv_row(reference="NEW-DOC")
        new[0] = str(self.day - timedelta(days=1))
        with self.assertRaises(ValidationError):
            self.ingest(new, self.csv_row(spend="101"))
        self.assertEqual(
            (
                AdDailyObservation.objects.count(),
                AdObservationRecovery.objects.count(),
                AuditEvent.objects.count(),
            ),
            (1, 1, audits),
        )
        self.assertIsNotNone(AdDailyObservation.objects.get().withdrawn_at)

    def test_archive_can_remove_obsolete_campaign_date_without_rewriting_historical_fact(
        self,
    ):
        self.campaign.actual_end_date = self.day - timedelta(days=1)
        self.campaign._workflow_write = True
        services._save(self.campaign)
        decision = ad_recovery.withdraw_ad_observation(**self.values())
        self.assertEqual(decision.predecessor.date, self.day)
        self.assertIsNotNone(decision.predecessor.withdrawn_at)


class AdRecoveryMigrationTests(TransactionTestCase):
    def test_original_ad_rows_keep_values_identity_and_creation_metadata_with_no_recovery_fabrication(
        self,
    ):
        executor = MigrationExecutor(connection)
        restore = executor.loader.graph.leaf_nodes()
        old_targets = [
            (app, "0008_alter_kpitarget_metric_key" if app == "marketing" else name)
            for app, name in restore
        ]
        executor.migrate(old_targets)
        try:
            old_apps = executor.loader.project_state(old_targets).apps
            org = old_apps.get_model("core", "Organization").objects.create(
                name="Synthetic legacy source"
            )
            actor = old_apps.get_model("auth", "User").objects.create(
                username="legacy-ad-source"
            )
            campaign = old_apps.get_model("marketing", "Campaign").objects.create(
                organization_id=org.pk,
                created_by_id=actor.pk,
                name="Legacy",
                channel="meta_ads",
            )
            observation = old_apps.get_model(
                "marketing", "AdDailyObservation"
            ).objects.create(
                organization_id=org.pk,
                created_by_id=actor.pk,
                campaign_id=campaign.pk,
                date=timezone.localdate(),
                source="Legacy source",
                reference="LEGACY-DOC",
                spend="100",
                impressions=1000,
                clicks=0,
                version=7,
            )
            observation.refresh_from_db()
            old_values = {
                field.attname: getattr(observation, field.attname)
                for field in observation._meta.fields
            }
            executor = MigrationExecutor(connection)
            executor.migrate(restore)
            new_apps = executor.loader.project_state(restore).apps
            current = new_apps.get_model("marketing", "AdDailyObservation").objects.get(
                pk=observation.pk
            )
            self.assertEqual(
                {name: getattr(current, name) for name in old_values}, old_values
            )
            self.assertEqual(
                (current.source_root_id, current.replaces_id, current.withdrawn_at),
                (None, None, None),
            )
            self.assertEqual(
                new_apps.get_model(
                    "marketing", "AdObservationRecovery"
                ).objects.count(),
                0,
            )
        finally:
            MigrationExecutor(connection).migrate(restore)
