import unittest
from datetime import datetime
from unittest.mock import Mock

from threeq.engine import connect
from threeq.kis import KIS, KISError, KISRejected
from threeq.live import NY, initialize, live_snapshot, reconcile, record_account, record_quotes, submit


class LiveTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)
        self.kis = Mock(cano="12345678", product="01")
        self.kis.order.return_value = "00001234"
        initialize(self.db, self.kis)
        self.now = datetime(2026, 11, 25, 10, tzinfo=NY)

    def submit(self, quantity=2, max_session=30_000):
        return submit(self.db, self.kis, "BUY", "TQQQ", quantity, 5000, 20_000,
                      max_session, self.now)

    def test_acceptance_then_partial_then_filled(self):
        self.assertEqual(self.submit(), (1, "00001234"))
        self.assertEqual(live_snapshot(self.db)["orders"][0]["state"], "ACCEPTED")
        trade = {"odno": "00001234", "pdno": "TQQQ", "sll_buy_dvsn_cd": "02",
                 "ft_ord_qty": "2", "ft_ccld_qty": "1", "ft_ccld_unpr3": "49.95"}
        self.kis.executions.return_value = [trade]
        reconcile(self.db, self.kis)
        partial = live_snapshot(self.db)["orders"][0]
        self.assertEqual((partial["state"], partial["filled"], partial["notification"]),
                         ("PARTIAL", 1, None))
        trade["ft_ccld_qty"] = "2"
        reconcile(self.db, self.kis)
        full = live_snapshot(self.db)["orders"][0]
        self.assertEqual((full["state"], full["filled"], full["notification"]),
                         ("FILLED", 2, "대기"))
        with self.assertRaisesRegex(KISError, "session USD limit"):
            self.submit(max_session=15_000)
        self.kis.order.assert_called_once()

    def test_unknown_order_persists_and_prevents_resubmit(self):
        self.kis.order.side_effect = KISError("request timed out")
        with self.assertRaisesRegex(KISError, "result unknown"):
            self.submit()
        self.assertEqual(live_snapshot(self.db)["orders"][0]["state"], "UNKNOWN")
        with self.assertRaisesRegex(KISError, "Unresolved live order"):
            self.submit()
        self.kis.order.assert_called_once()

    def test_order_rejection_and_configured_limit(self):
        with self.assertRaisesRegex(KISError, "per-order USD limit"):
            self.submit(quantity=5)
        self.kis.order.assert_not_called()
        self.kis.order.side_effect = KISRejected("rejected")
        with self.assertRaises(KISRejected):
            self.submit()
        self.assertEqual(live_snapshot(self.db)["orders"][0]["state"], "REJECTED")

    def test_switching_broker_account_requires_separate_database(self):
        self.kis.cano = "87654321"
        with self.assertRaisesRegex(KISError, "different KIS account"):
            initialize(self.db, self.kis)

    def test_account_and_quotes_are_sourced_from_broker(self):
        self.kis.holdings.return_value = [{"ovrs_pdno": "TQQQ", "ovrs_cblc_qty": "2"}]
        self.kis.quote.side_effect = [58000, 5000, 3900]
        record_quotes(self.db, self.kis)
        record_account(self.db, self.kis)
        state = live_snapshot(self.db)
        self.assertEqual(state["quotes"], {"QQQ": 58000, "TQQQ": 5000, "SQQQ": 3900})
        self.assertEqual(state["holdings"], [{"symbol": "TQQQ", "quantity": "2"}])

    def test_production_limit_order_uses_documented_fields(self):
        kis = KIS.__new__(KIS)
        kis.cano, kis.product = "12345678", "01"
        kis.trade_api = Mock(return_value=({"output": {"ODNO": "01234"}}, ""))
        self.assertEqual(kis.order("SELL", "SQQQ", 3, 4910), "01234")
        args, kwargs = kis.trade_api.call_args
        self.assertEqual(args[1], "TTTT1006U")
        self.assertEqual(kwargs["body"]["ORD_DVSN"], "00")
        self.assertEqual(kwargs["body"]["OVRS_ORD_UNPR"], "49.10")


if __name__ == "__main__":
    unittest.main()
