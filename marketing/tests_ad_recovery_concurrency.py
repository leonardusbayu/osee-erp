"""Actual PostgreSQL races share the marketing Organization lock."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from core.models import AuditEvent, Membership, Organization

from . import ad_recovery, services
from .models import AdDailyObservation, AdObservationRecovery


@skipUnless(connection.vendor == "postgresql", "Requires actual PostgreSQL row locks")
class ConcurrentAdRecoveryTests(TransactionTestCase):
    def setUp(self):
        self.assertTrue(connection.settings_dict["NAME"].startswith("test_"))
        self.org = Organization.objects.create(name="Synthetic ad recovery races")
        self.actor = get_user_model().objects.create_user(username="ad-recovery-race")
        Membership.objects.create(
            organization=self.org, user=self.actor, role="marketing"
        )
        self.day = timezone.localdate()
        self.campaign = services.create_campaign(
            organization=self.org,
            actor=self.actor,
            name="One original source",
            channel="meta_ads",
        )
        self.ad = services.save_ad_observation(
            organization=self.org,
            actor=self.actor,
            campaign=self.campaign,
            date=self.day,
            spend="100",
            source="Synthetic source",
            reference="RACE-DOC",
        )
        self.fingerprint = ad_recovery.observation_fingerprint(self.ad)

    def request(self, **changes):
        return {
            "organization": self.org.pk,
            "actor": self.actor,
            "observation": self.ad.pk,
            "expected_version": 1,
            "expected_fingerprint": self.fingerprint,
            "request_key": "RACE-REQUEST",
            "reason": "Periksa dokumen untuk race uji",
            "evidence": "Synthetic archive/race",
            **changes,
        }

    def race(self, *actions):
        barrier = Barrier(len(actions))

        def execute(action):
            close_old_connections()
            try:
                self.assertTrue(
                    connections["default"].settings_dict["NAME"].startswith("test_")
                )
                barrier.wait(timeout=10)
                try:
                    row = action()
                    return {
                        "status": "committed",
                        "pk": row.pk,
                        "type": type(row).__name__,
                    }
                except ValidationError as exc:
                    return {"status": "rejected", "messages": exc.messages}
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(actions)) as workers:
            futures = [workers.submit(execute, action) for action in actions]
            return [future.result(timeout=30) for future in futures]

    def test_identical_request_race_commits_one_decision_and_returns_it_to_both_callers(
        self,
    ):
        results = self.race(
            *[
                lambda: ad_recovery.replace_ad_observation(
                    **self.request(), spend="120"
                )
                for _ in range(2)
            ]
        )
        self.assertEqual([row["status"] for row in results], ["committed", "committed"])
        self.assertEqual(results[0]["pk"], results[1]["pk"])
        self.assertEqual(
            (AdObservationRecovery.objects.count(), AdDailyObservation.objects.count()),
            (1, 2),
        )
        self.assertEqual(
            AuditEvent.objects.filter(action="marketing.ad.replaced").count(), 1
        )
        self.assertEqual(
            AdDailyObservation.objects.filter(withdrawn_at__isnull=True).get().spend,
            Decimal(120),
        )
        self.assertEqual(AdDailyObservation.objects.get(pk=self.ad.pk).version, 2)

    def test_competing_replacements_accept_one_successor_and_preserve_original_document(
        self,
    ):
        results = self.race(
            *[
                lambda index=index: ad_recovery.replace_ad_observation(
                    **self.request(request_key=f"RACE-{index}"), spend=str(120 + index)
                )
                for index in range(2)
            ]
        )
        self.assertCountEqual(
            [row["status"] for row in results], ["committed", "rejected"]
        )
        winner = next(
            index for index, row in enumerate(results) if row["status"] == "committed"
        )
        decision = AdObservationRecovery.objects.get()
        new = AdDailyObservation.objects.filter(withdrawn_at__isnull=True).get()
        self.assertEqual(
            (new.spend, new.replaces_id, new.source_root_id, new.reference),
            (Decimal(120 + winner), self.ad.pk, self.ad.pk, self.ad.reference),
        )
        self.assertEqual(decision.successor_id, new.pk)
        self.assertEqual(
            AuditEvent.objects.filter(action="marketing.ad.replaced").count(), 1
        )

    def test_withdraw_against_replace_cannot_leave_two_decisions_or_active_archived_root(
        self,
    ):
        results = self.race(
            lambda: ad_recovery.withdraw_ad_observation(
                **self.request(request_key="WITHDRAW")
            ),
            lambda: ad_recovery.replace_ad_observation(
                **self.request(request_key="REPLACE"), spend="120"
            ),
        )
        self.assertCountEqual(
            [row["status"] for row in results], ["committed", "rejected"]
        )
        winner = next(
            index for index, row in enumerate(results) if row["status"] == "committed"
        )
        decision = AdObservationRecovery.objects.get()
        self.assertEqual(decision.action, "withdraw" if winner == 0 else "replace")
        self.assertIsNotNone(AdDailyObservation.objects.get(pk=self.ad.pk).withdrawn_at)
        self.assertEqual(
            AdDailyObservation.objects.filter(withdrawn_at__isnull=True).count(),
            0 if winner == 0 else 1,
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                action__in=("marketing.ad.withdrawn", "marketing.ad.replaced")
            ).count(),
            1,
        )

    def test_numeric_correction_against_replacement_keeps_one_current_result_and_one_audit(
        self,
    ):
        results = self.race(
            lambda: services.save_ad_observation(
                organization=self.org.pk,
                actor=self.actor,
                observation=self.ad.pk,
                expected_version=1,
                spend="110",
                reason="Numeric correction race",
            ),
            lambda: ad_recovery.replace_ad_observation(**self.request(), spend="120"),
        )
        self.assertCountEqual(
            [row["status"] for row in results], ["committed", "rejected"]
        )
        winner = next(
            index for index, row in enumerate(results) if row["status"] == "committed"
        )
        current = AdDailyObservation.objects.filter(withdrawn_at__isnull=True).get()
        self.assertEqual(current.spend, Decimal("110" if winner == 0 else "120"))
        self.assertEqual(AdObservationRecovery.objects.count(), 0 if winner == 0 else 1)
        self.assertEqual(
            AuditEvent.objects.filter(
                action__in=("marketing.ad.corrected", "marketing.ad.replaced")
            ).count(),
            1,
        )
        original = AdDailyObservation.objects.get(pk=self.ad.pk)
        self.assertEqual(
            (original.version, bool(original.withdrawn_at)), (2, winner == 1)
        )

    def test_competing_sources_for_one_new_active_grain_rollback_loser_archival(self):
        second_campaign = services.create_campaign(
            organization=self.org,
            actor=self.actor,
            name="Second source",
            channel="google_ads",
        )
        target = services.create_campaign(
            organization=self.org,
            actor=self.actor,
            name="Shared correct target",
            channel="meta_ads",
        )
        second = services.save_ad_observation(
            organization=self.org,
            actor=self.actor,
            campaign=second_campaign,
            date=self.day,
            spend="80",
            source="Second source",
            reference="SECOND-RACE-DOC",
        )
        sources = (self.ad, second)
        results = self.race(
            *[
                lambda index=index, source=source: ad_recovery.replace_ad_observation(
                    **self.request(
                        observation=source.pk,
                        expected_fingerprint=ad_recovery.observation_fingerprint(
                            source
                        ),
                        request_key=f"SHARED-GRAIN-{index}",
                    ),
                    campaign=target.pk,
                )
                for index, source in enumerate(sources)
            ]
        )
        self.assertCountEqual(
            [row["status"] for row in results], ["committed", "rejected"]
        )
        winner = next(
            index for index, row in enumerate(results) if row["status"] == "committed"
        )
        source_winner, source_loser = sources[winner], sources[1 - winner]
        source_winner.refresh_from_db()
        source_loser.refresh_from_db()
        self.assertIsNotNone(source_winner.withdrawn_at)
        self.assertEqual((source_loser.withdrawn_at, source_loser.version), (None, 1))
        self.assertEqual(
            AdDailyObservation.objects.filter(withdrawn_at__isnull=True).count(), 2
        )
        self.assertEqual(
            AdDailyObservation.objects.filter(
                campaign=target, withdrawn_at__isnull=True
            ).count(),
            1,
        )
        self.assertEqual(AdObservationRecovery.objects.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(action="marketing.ad.replaced").count(), 1
        )
