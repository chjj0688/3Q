import io
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from threeq.engine import apply_sample, connect, replay_csv


def quotes(qqq):
    return {"QQQ": qqq, "TQQQ": 5000, "SQQQ": 4000}


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)

    def test_gap_prevents_entry_and_duplicate_does_not_change_state(self):
        for second in range(20):
            apply_sample(self.db, second, quotes(50_000))
        self.assertTrue(apply_sample(self.db, 21, quotes(60_000)))
        self.assertFalse(apply_sample(self.db, 21, quotes(40_000)))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bars").fetchone()[0], 63)

    def test_switch_and_pause_preserve_account_on_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account.sqlite3"
            with closing(connect(path)) as db:
                for second in range(19):
                    apply_sample(db, second, quotes(50_000))
                apply_sample(db, 19, quotes(60_000), notify=True)
                db.execute("UPDATE account SET paused=1")
                apply_sample(db, 20, quotes(40_000))
            with closing(connect(path)) as db:
                self.assertEqual(tuple(db.execute(
                    "SELECT cash, symbol, quantity, paused FROM account"
                ).fetchone()), (750_000, "TQQQ", 50, 1))
                self.assertEqual(db.execute("SELECT notification FROM fills").fetchone()[0], "대기")
                db.execute("UPDATE account SET paused=0")
                apply_sample(db, 21, quotes(40_000))
                fills = db.execute("SELECT side, symbol FROM fills ORDER BY id").fetchall()
                self.assertEqual([tuple(fill) for fill in fills],
                                 [("BUY", "TQQQ"), ("SELL", "TQQQ"), ("BUY", "SQQQ")])

    def test_replay_reports_equity_and_no_external_side_effect(self):
        contents = ["timestamp,QQQ,TQQQ,SQQQ"]
        for second in range(21):
            stamp = datetime.fromtimestamp(second, timezone.utc).isoformat()
            contents.append(f"{stamp},{50_000 if second < 19 else 60_000},{4900 if second == 20 else 5000},4000")
        summary = replay_csv(self.db, io.StringIO("\n".join(contents)))
        self.assertEqual(summary, dict(samples=21, fills=1, equity_cents=995_000,
                                       return_pct=-0.5, max_drawdown_pct=0.5))
        self.assertEqual(self.db.execute("SELECT notification FROM fills").fetchone()[0], "미설정")

    def test_replay_rejects_timestamps_without_timezone(self):
        csv = io.StringIO("timestamp,QQQ,TQQQ,SQQQ\n2026-11-25T09:30:00,50000,5000,4000\n")
        with self.assertRaisesRegex(ValueError, "timezone"):
            replay_csv(self.db, csv)


if __name__ == "__main__":
    unittest.main()
