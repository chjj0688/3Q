"""SQLite-backed paper account driven by complete one-second quote samples."""
import csv
import math
import sqlite3
from datetime import datetime, timezone

SYMBOLS = ("QQQ", "TQQQ", "SQQQ")
STARTING_CASH = 1_000_000
WINDOW = 20
THRESHOLD = 0.001

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    second INTEGER NOT NULL, symbol TEXT NOT NULL, price INTEGER NOT NULL,
    PRIMARY KEY (symbol, second)
);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1), cash INTEGER NOT NULL,
    symbol TEXT, quantity INTEGER NOT NULL, entry INTEGER, paused INTEGER NOT NULL
);
INSERT OR IGNORE INTO account VALUES (1, 1000000, NULL, 0, NULL, 0);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, second INTEGER NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL, quantity INTEGER NOT NULL, price INTEGER NOT NULL,
    reason TEXT NOT NULL, notification TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    second INTEGER PRIMARY KEY, signal TEXT NOT NULL, reason TEXT NOT NULL,
    equity INTEGER NOT NULL
);
"""


def connect(path):
    db = sqlite3.connect(path, isolation_level=None, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def apply_sample(db, second, prices, notify=False):
    """Accept one complete UTC second. Prices and balances are integer USD cents."""
    if set(prices) != set(SYMBOLS) or any(type(p) is not int or p <= 0 for p in prices.values()):
        raise ValueError("Each sample must contain positive QQQ, TQQQ and SQQQ prices in cents")

    db.execute("BEGIN IMMEDIATE")
    try:
        previous = db.execute("SELECT MAX(second) FROM decisions").fetchone()[0]
        if previous is not None and second <= previous:
            db.rollback()
            return False
        db.executemany("INSERT INTO bars VALUES (?, ?, ?)",
                       [(second, symbol, prices[symbol]) for symbol in SYMBOLS])
        recent = db.execute(
            "SELECT second, price FROM bars WHERE symbol = 'QQQ' ORDER BY second DESC LIMIT ?",
            (WINDOW,),
        ).fetchall()
        account = db.execute("SELECT * FROM account").fetchone()
        cash, symbol, quantity, entry = (account[key] for key in ("cash", "symbol", "quantity", "entry"))
        signal = "HOLD"
        if account["paused"]:
            reason = "모의 주문 일시정지"
        elif len(recent) < WINDOW or recent[0]["second"] - recent[-1]["second"] != WINDOW - 1:
            reason = "연속된 1초봉 20개 대기"
        else:
            average = sum(row["price"] for row in recent) / WINDOW
            change = prices["QQQ"] / average - 1
            signal = "TQQQ" if change > THRESHOLD else "SQQQ" if change < -THRESHOLD else "CASH"
            reason = f"QQQ 20초 평균 대비 {change:+.2%}"
            notification = "대기" if notify else "미설정"
            if symbol and symbol != signal:
                db.execute(
                    "INSERT INTO fills VALUES (NULL, ?, ?, 'SELL', ?, ?, ?, ?)",
                    (second, symbol, quantity, prices[symbol], reason, notification),
                )
                cash += quantity * prices[symbol]
                symbol, quantity, entry = None, 0, None
            if symbol is None and signal in ("TQQQ", "SQQQ"):
                price = prices[signal]
                shares = (cash // 4) // price
                if shares:
                    db.execute(
                        "INSERT INTO fills VALUES (NULL, ?, ?, 'BUY', ?, ?, ?, ?)",
                        (second, signal, shares, price, reason, notification),
                    )
                    cash -= shares * price
                    symbol, quantity, entry = signal, shares, price
        equity = cash + (quantity * prices[symbol] if symbol else 0)
        db.execute("UPDATE account SET cash=?, symbol=?, quantity=?, entry=? WHERE id=1",
                   (cash, symbol, quantity, entry))
        db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?)", (second, signal, reason, equity))
        db.commit()
        return True
    except BaseException:
        db.rollback()
        raise


def demo_sample(second):
    wave = math.sin(second / 24)
    return {"QQQ": round(58000 + wave * 800),
            "TQQQ": round(4850 + wave * 200),
            "SQQQ": round(3900 - wave * 160)}


def replay_csv(db, file):
    """Replay pre-normalized, UTC-aligned quote samples without external side effects."""
    reader = csv.DictReader(file)
    required = {"timestamp", *SYMBOLS}
    if reader.fieldnames is None or set(reader.fieldnames) != required:
        raise ValueError("CSV columns must be timestamp,QQQ,TQQQ,SQQQ")
    previous = None
    peak = STARTING_CASH
    max_drawdown = 0.0
    samples = 0
    for row in reader:
        stamp = datetime.fromisoformat(row["timestamp"])
        if stamp.utcoffset() is None or stamp.microsecond:
            raise ValueError(f"CSV line {reader.line_num}: expected timezone and whole seconds")
        second = int(stamp.astimezone(timezone.utc).timestamp())
        if previous is not None and second <= previous:
            raise ValueError(f"CSV line {reader.line_num}: timestamps must increase")
        apply_sample(db, second, {symbol: int(row[symbol]) for symbol in SYMBOLS})
        equity = db.execute("SELECT equity FROM decisions WHERE second=?", (second,)).fetchone()[0]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1 - equity / peak)
        previous = second
        samples += 1
    if not samples:
        raise ValueError("CSV has no samples")
    fills = db.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    return {"samples": samples, "fills": fills, "equity_cents": equity,
            "return_pct": round(100 * (equity / STARTING_CASH - 1), 4),
            "max_drawdown_pct": round(100 * max_drawdown, 4)}
