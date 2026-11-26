"""Production account monitor and explicit, auditable limit orders."""
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from zoneinfo import ZoneInfo

from .kis import KISError, KISRejected, SYMBOLS

NY = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    identity TEXT NOT NULL, updated INTEGER, holdings TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS live_quotes (
    second INTEGER NOT NULL, symbol TEXT NOT NULL, price INTEGER NOT NULL,
    PRIMARY KEY (symbol, second)
);
CREATE TABLE IF NOT EXISTS live_orders (
    id INTEGER PRIMARY KEY, session TEXT NOT NULL, sent INTEGER NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
    limit_price INTEGER NOT NULL, state TEXT NOT NULL, broker_id TEXT,
    filled INTEGER NOT NULL DEFAULT 0, avg_price TEXT, notification TEXT
);
"""


def initialize(db, kis):
    identity = sha256(f"{kis.cano}:{kis.product}".encode()).hexdigest()
    db.executescript(SCHEMA)
    db.execute("INSERT OR IGNORE INTO live_account (id, identity) VALUES (1, ?)", (identity,))
    if db.execute("SELECT identity FROM live_account").fetchone()[0] != identity:
        raise KISError("Live database is already bound to a different KIS account")


def order_limit(value):
    try:
        price = Decimal(value) * 100
        if not price.is_finite() or not 1 <= price <= 100_000_000 or price != int(price):
            raise ValueError
        return int(price)
    except (InvalidOperation, ValueError):
        raise ValueError("Use a positive USD amount with at most two decimals") from None


def submit(db, kis, side, symbol, quantity, limit, max_order, max_session, now=None):
    now = now or datetime.now(NY)
    if (now.weekday() >= 5 or not (9 * 60 + 30 <= now.hour * 60 + now.minute < 16 * 60)):
        raise KISError("Order submission is limited to weekday 09:30–16:00 New York time")
    if side not in ("BUY", "SELL") or symbol not in ("TQQQ", "SQQQ") or quantity < 1 or limit < 1:
        raise KISError("Invalid order")
    notional = quantity * limit
    if notional > max_order:
        raise KISError("Order exceeds configured per-order USD limit")
    # Authenticate and reconcile before recording an intent. The intent then survives any
    # timeout or interruption while the broker may have received the request.
    kis.authenticate()
    reconcile(db, kis)
    session = now.strftime("%Y%m%d")
    db.execute("BEGIN IMMEDIATE")
    try:
        pending = db.execute(
            "SELECT id FROM live_orders WHERE state IN ('UNKNOWN','ACCEPTED','PARTIAL') LIMIT 1"
        ).fetchone()
        if pending:
            raise KISError(f"Unresolved live order #{pending[0]}; reconcile with KIS before another submission")
        used = db.execute(
            "SELECT COALESCE(SUM(quantity*limit_price), 0) FROM live_orders "
            "WHERE session=? AND state <> 'REJECTED'", (session,),
        ).fetchone()[0]
        if used + notional > max_session:
            raise KISError("Order exceeds configured session USD limit")
        order_id = db.execute(
            "INSERT INTO live_orders (session,sent,symbol,side,quantity,limit_price,state) "
            "VALUES (?,?,?,?,?,?,'UNKNOWN')",
            (session, int(now.timestamp()), symbol, side, quantity, limit),
        ).lastrowid
        db.commit()
    except BaseException:
        db.rollback()
        raise
    try:
        number = kis.order(side, symbol, quantity, limit)
    except KISRejected:
        db.execute("UPDATE live_orders SET state='REJECTED' WHERE id=?", (order_id,))
        raise
    except KISError as exc:
        raise KISError(f"Order #{order_id} result unknown: {exc}") from None
    db.execute("UPDATE live_orders SET broker_id=?, state='ACCEPTED' WHERE id=?",
               (number, order_id))
    return order_id, number


def reconcile(db, kis):
    pending = db.execute(
        "SELECT * FROM live_orders WHERE state IN ('ACCEPTED', 'PARTIAL') ORDER BY id"
    ).fetchall()
    if not pending:
        return
    for order in pending:
        day = datetime.strptime(order["session"], "%Y%m%d")
        end = (day + timedelta(days=1)).strftime("%Y%m%d")
        rows = kis.executions(order["session"], end)
        matches = [
            row for row in rows
            if str(row.get("odno", "")).strip().lstrip("0") == order["broker_id"].lstrip("0")
            and row.get("pdno") == order["symbol"]
            and row.get("sll_buy_dvsn_cd") == {"BUY": "02", "SELL": "01"}[order["side"]]
        ]
        if len(matches) != 1:
            continue
        row = matches[0]
        try:
            ordered = int(row["ft_ord_qty"])
            filled = int(row["ft_ccld_qty"])
            average = Decimal(row["ft_ccld_unpr3"]) if filled else None
        except (KeyError, ValueError, TypeError, InvalidOperation):
            continue
        if ordered != order["quantity"] or not order["filled"] <= filled <= ordered:
            continue
        if average is not None and (not average.is_finite() or average <= 0):
            continue
        state = "FILLED" if filled == ordered else "PARTIAL" if filled else "ACCEPTED"
        notification = "대기" if state == "FILLED" else None
        db.execute(
            "UPDATE live_orders SET state=?,filled=?,avg_price=?,notification=? WHERE id=? AND state IN ('ACCEPTED','PARTIAL') AND filled <= ?",
            (state, filled, None if average is None else str(average), notification, order["id"], filled),
        )


def record_quotes(db, kis):
    quotes = [(symbol, kis.quote(symbol)) for symbol in SYMBOLS]
    second = int(time.time())
    db.execute("BEGIN IMMEDIATE")
    try:
        db.executemany("INSERT OR IGNORE INTO live_quotes VALUES (?, ?, ?)",
                       [(second, symbol, price) for symbol, price in quotes])
        db.commit()
    except BaseException:
        db.rollback()
        raise


def record_account(db, kis):
    rows = kis.holdings()
    holdings = [
        {"symbol": row["ovrs_pdno"], "quantity": row["ovrs_cblc_qty"]}
        for row in rows if "ovrs_pdno" in row and "ovrs_cblc_qty" in row
    ]
    if rows and len(holdings) != len(rows):
        raise KISError("KIS holdings fields could not be confirmed")
    db.execute("UPDATE live_account SET updated=?, holdings=? WHERE id=1",
               (int(time.time()), json.dumps(holdings)))


def live_snapshot(db):
    account = db.execute("SELECT updated, holdings, error FROM live_account").fetchone()
    latest = db.execute("SELECT MAX(second) FROM live_quotes").fetchone()[0]
    quotes = dict(db.execute("SELECT symbol, price FROM live_quotes WHERE second=?", (latest,)))
    chart = [dict(row) for row in db.execute(
        "SELECT second,price FROM live_quotes WHERE symbol='TQQQ' ORDER BY second DESC LIMIT 120"
    )]
    orders = [dict(row) for row in db.execute("SELECT * FROM live_orders ORDER BY id DESC LIMIT 10")]
    return dict(mode="live", account_updated=account["updated"],
                holdings=json.loads(account["holdings"] or "[]"), error=account["error"],
                latest=latest, quotes=quotes, chart=list(reversed(chart)), orders=orders)
