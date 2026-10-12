"""Browser date controls retain the completion date when work is revised."""

from datetime import date

from django.test import SimpleTestCase
from django.utils import translation

from .performance_forms import ActivitySubmitForm


class ActivityDateFormTests(SimpleTestCase):
    def test_revision_date_renders_as_iso_for_native_date_control(self):
        with translation.override("id"):
            form = ActivitySubmitForm(initial={"occurred_on": date(2026, 10, 2)})
            self.assertIn('type="date"', str(form["occurred_on"]))
            self.assertIn('value="2026-10-02"', str(form["occurred_on"]))

    def test_iso_date_submission_round_trips_during_revision(self):
        with translation.override("id"):
            form = ActivitySubmitForm(
                data={
                    "occurred_on": "2026-10-02",
                    "evidence": "Arsip laporan setelah revisi",
                    "outcome": "Hasil diperbaiki sesuai catatan pemeriksa.",
                }
            )
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data["occurred_on"], date(2026, 10, 2))
            self.assertIn('value="2026-10-02"', str(form["occurred_on"]))
