from django.test import SimpleTestCase

from taxes.chat import classify_topic


class TaxChatTopicTests(SimpleTestCase):
    def test_deadline_intent_precedes_regime_keywords_without_misrouting_rule_dates(
        self,
    ):
        cases = (
            ("Kapan bayar pajak final?", "deadline"),
            ("Tanggal berapa bayar PP 55?", "deadline"),
            ("Kapan lapor pajak masa?", "deadline"),
            ("Kapan aturan PP 20/2026 mulai berlaku?", "final"),
            ("Batas waktu fasilitas PP 55?", "final"),
            ("Apa beda PP 23 dan PP 55?", "difference"),
        )
        for question, expected_topic in cases:
            with self.subTest(question=question):
                self.assertEqual(classify_topic(question), expected_topic)
