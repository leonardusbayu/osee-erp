"""A clean Marketing-only history must resolve its Director foreign keys."""

from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase


class MarketingMigrationDependencyTests(SimpleTestCase):
    def assert_director_relations_resolve(self, migration):
        loader = MigrationLoader(None)
        loader.graph.validate_consistency()
        loader.graph.ensure_not_cyclic()
        historical_apps = loader.project_state([("marketing", migration)]).apps
        work = historical_apps.get_model("marketing", "WorkActivity")
        self.assertIs(
            work._meta.get_field("decision").remote_field.model,
            historical_apps.get_model("director", "Decision"),
        )
        self.assertIs(
            work._meta.get_field("budget").remote_field.model,
            historical_apps.get_model("director", "Budget"),
        )

    def test_marketing_before_ad_recovery_resolves_director_models(self):
        self.assert_director_relations_resolve("0008_alter_kpitarget_metric_key")

    def test_marketing_with_ad_recovery_resolves_director_models(self):
        self.assert_director_relations_resolve("0009_ad_observation_recovery")
