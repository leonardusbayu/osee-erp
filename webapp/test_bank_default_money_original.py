"""UNEXECUTED ORIGINAL: staff submits the bank amount already filled by server.

Root archives/promotes/runs this separately. No parser normalization or product
mutation is specified by the oracle; the actual DOM value is posted unchanged.
Borrow fixtures through module aliases so their old tests are not rediscovered.
"""

from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urlsplit

from django import test as django_test
from django.urls import reverse

from finance import services
from finance.models import Allocation, BillPayment
from webapp import test_bank_money_input_original as incoming_original
from webapp import test_bank_payment_money_input_original as outgoing_original


class BankMatchRenderedAmount(HTMLParser):
    """Read the real input and its enclosing CSRF form without interpreting money."""

    def __init__(self, amount_id):
        super().__init__(convert_charrefs=True)
        self.amount_id = amount_id
        self.forms = []
        self.current = None
        self.select_name = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {
                "action": attrs.get("action"),
                "method": attrs.get("method"),
                "amount_values": [],
                "csrf_values": [],
                "selects": {},
            }
        elif self.current is not None and tag == "input":
            if attrs.get("id") == self.amount_id:
                self.current["amount_values"].append(attrs.get("value"))
            if attrs.get("name") == "csrfmiddlewaretoken":
                self.current["csrf_values"].append(attrs.get("value"))
        elif self.current is not None and tag == "select":
            self.select_name = attrs.get("name")
            self.current["selects"][self.select_name] = []
        elif (
            self.current is not None
            and tag == "option"
            and self.select_name is not None
        ):
            self.current["selects"][self.select_name].append(attrs.get("value"))

    def handle_endtag(self, tag):
        if tag == "select":
            self.select_name = None
        elif tag == "form" and self.current is not None:
            self.forms.append(self.current)
            self.current = None
            self.select_name = None


@django_test.override_settings(
    MFA_REQUIRED=False,
    OPENROUTER_API_KEY="",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class BankMatchDefaultMoneyInputOriginalDraft(django_test.TestCase):
    def rendered_form(self, probe, case, *, amount_id, document_name, document):
        before = probe.money_state(case)
        page = case["client"].get(reverse("bank") + "?status=unmatched")
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context["bank_source_held"])
        self.assertEqual(page.context["membership"].role, "finance")
        self.assertTrue(probe.finance.is_active)
        available = page.context[
            "open_invoices" if document_name == "invoice" else "open_bills"
        ]
        self.assertIn(document.pk, [row.pk for row in available])
        transaction = next(
            row for row in page.context["transactions"] if row.pk == case["bank"].pk
        )
        self.assertTrue(
            transaction.can_match
            if document_name == "invoice"
            else transaction.can_match_bill
        )
        parser = BankMatchRenderedAmount(amount_id)
        parser.feed(page.content.decode(page.charset))
        parser.close()
        matches = [form for form in parser.forms if form["amount_values"]]
        self.assertEqual(len(matches), 1)
        form = matches[0]
        self.assertEqual(form["method"].lower(), "post")
        action = urlsplit(form["action"])
        self.assertFalse(action.scheme or action.netloc or action.fragment)
        self.assertEqual(action.path, case["url"])
        self.assertEqual(len(form["amount_values"]), 1)
        self.assertIsInstance(form["amount_values"][0], str)
        self.assertTrue(form["amount_values"][0])
        self.assertEqual(len(form["csrf_values"]), 1)
        self.assertTrue(form["csrf_values"][0])
        self.assertIn(str(document.pk), form["selects"][document_name])
        self.assertEqual(
            probe.money_state(case), before, "GET leaves money and authority unchanged"
        )
        probe.assert_sources_current(case)
        return form

    def test_unchanged_server_default_receipt_settles_exact_fifty_thousand(self):
        probe = incoming_original.BankMatchMoneyInputOriginalDraft(methodName="runTest")
        probe.setUp()
        self.addCleanup(probe.doCleanups)
        case = probe.fixture("default-incoming")
        form = self.rendered_form(
            probe,
            case,
            amount_id=f"amount-{case['bank'].pk}",
            document_name="invoice",
            document=case["invoice"],
        )
        raw = form["amount_values"][0]
        before = probe.money_state(case)
        denied = case["client"].post(
            form["action"], {"invoice": str(case["invoice"].pk), "amount": raw}
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(probe.money_state(case), before)
        response = case["client"].post(
            form["action"],
            {
                "invoice": str(case["invoice"].pk),
                "amount": raw,
                "csrfmiddlewaretoken": form["csrf_values"][0],
            },
        )
        self.assertRedirects(
            response,
            reverse("bank") + "?status=unmatched",
            fetch_redirect_response=False,
        )
        allocation = Allocation.objects.get(
            organization=case["org"], transaction=case["bank"], invoice=case["invoice"]
        )
        self.assertEqual(allocation.amount, Decimal(50000))
        self.assertEqual(Allocation.objects.filter(organization=case["org"]).count(), 1)
        self.assertFalse(BillPayment.objects.filter(organization=case["org"]).exists())
        self.assertEqual(
            probe.balances(case),
            (Decimal(50000), Decimal(0), Decimal(-50000), Decimal(0), Decimal(0)),
        )
        self.assertEqual(
            (
                case["invoice"].paid_amount,
                case["invoice"].outstanding_amount,
                case["bank"].unallocated_amount,
            ),
            (Decimal(50000), Decimal(0), Decimal(0)),
        )
        probe.assert_sources_current(case)
        after = probe.money_state(case)
        replay = services.reconcile_receipt(
            organization=case["org"],
            actor=probe.finance,
            transaction=case["bank"],
            invoice=case["invoice"],
            amount=Decimal("50000.00"),
        )
        self.assertEqual(replay.pk, allocation.pk)
        self.assertEqual(probe.money_state(case), after)
        probe.assert_sources_current(case)

    def test_unchanged_server_default_supplier_payment_settles_exact_fifty_thousand(
        self,
    ):
        probe = outgoing_original.BankPaymentMoneyInputOriginalDraft(
            methodName="runTest"
        )
        probe.setUp()
        self.addCleanup(probe.doCleanups)
        case = probe.fixture("default-outgoing")
        form = self.rendered_form(
            probe,
            case,
            amount_id=f"bill-amount-{case['bank'].pk}",
            document_name="bill",
            document=case["bill"],
        )
        raw = form["amount_values"][0]
        before = probe.money_state(case)
        denied = case["client"].post(
            form["action"], {"bill": str(case["bill"].pk), "amount": raw}
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(probe.money_state(case), before)
        response = case["client"].post(
            form["action"],
            {
                "bill": str(case["bill"].pk),
                "amount": raw,
                "csrfmiddlewaretoken": form["csrf_values"][0],
            },
        )
        self.assertRedirects(
            response,
            reverse("bank") + "?status=unmatched",
            fetch_redirect_response=False,
        )
        payment = BillPayment.objects.get(
            organization=case["org"], transaction=case["bank"], bill=case["bill"]
        )
        self.assertEqual(payment.amount, Decimal(50000))
        self.assertEqual(
            BillPayment.objects.filter(organization=case["org"]).count(), 1
        )
        self.assertFalse(Allocation.objects.filter(organization=case["org"]).exists())
        self.assertEqual(
            probe.balances(case),
            (Decimal(0), Decimal(0), Decimal(-50000), Decimal(50000), Decimal(0)),
        )
        self.assertEqual(
            (
                case["bill"].paid_amount,
                case["bill"].outstanding_amount,
                case["bank"].unallocated_amount,
            ),
            (Decimal(50000), Decimal(0), Decimal(0)),
        )
        probe.assert_sources_current(case)
        after = probe.money_state(case)
        replay = services.reconcile_bill_payment(
            organization=case["org"],
            actor=probe.finance,
            transaction=case["bank"],
            bill=case["bill"],
            amount=Decimal("50000.00"),
        )
        self.assertEqual(replay.pk, payment.pk)
        self.assertEqual(probe.money_state(case), after)
        probe.assert_sources_current(case)
