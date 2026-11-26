"""Monitor KIS, submit an explicit limit order, or run local research workflows."""
import argparse
import fcntl
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .engine import STARTING_CASH, apply_sample, connect, demo_sample, replay_csv
from .kis import KIS, KISError, NoRedirect
from .live import initialize, live_snapshot, order_limit, reconcile, record_account, record_quotes, submit

ROOT = Path(__file__).resolve().parents[1]
ASSETS = {
    "/": ("prototype/index.html", "text/html; charset=utf-8"),
    "/dashboard.js": ("prototype/dashboard.js", "text/javascript; charset=utf-8"),
}


def snapshot(db):
    account = dict(db.execute("SELECT * FROM account").fetchone())
    decisions = [dict(row) for row in db.execute(
        "SELECT * FROM decisions ORDER BY second DESC LIMIT 120"
    )]
    fills = [dict(row) for row in db.execute("SELECT * FROM fills ORDER BY id DESC LIMIT 10")]
    quotes = {row["symbol"]: row["price"] for row in db.execute(
        "SELECT symbol, price FROM bars WHERE second=(SELECT MAX(second) FROM decisions)"
    )}
    chart = [dict(row) for row in db.execute(
        "SELECT second, price FROM bars WHERE symbol='TQQQ' ORDER BY second DESC LIMIT 120"
    )]
    return dict(account=account, latest=decisions[0] if decisions else None,
                quotes=quotes, fills=fills, chart=list(reversed(chart)),
                starting_cash=STARTING_CASH)


def send_discord(db, webhook, stop, live=False):
    opener = build_opener(NoRedirect())
    table = "live_orders" if live else "fills"
    while not stop.is_set():
        fill = db.execute(f"SELECT * FROM {table} WHERE notification='대기' ORDER BY id LIMIT 1").fetchone()
        if fill is None:
            stop.wait(1)
            continue
        db.execute(f"UPDATE {table} SET notification='결과 불명' WHERE id=?", (fill["id"],))
        side = "매수" if fill["side"] == "BUY" else "매도"
        prefix = "실계좌 체결" if live else "데모 모의 체결"
        average = fill["avg_price"] if live else f"{fill['price'] / 100:.2f}"
        message = (f"[3Q {prefix} #{fill['id']}] {fill['symbol']} {side} "
                   f"{fill['quantity']}주 @ USD {average}")
        request = Request(webhook, data=json.dumps({"content": message}).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=5):
                status = "전송 성공"
        except HTTPError as exc:
            status = f"전송 실패 (HTTP {exc.code})"
            exc.close()
        except (URLError, TimeoutError, OSError):
            status = "결과 불명"
        db.execute(f"UPDATE {table} SET notification=? WHERE id=?", (status, fill["id"]))


def serve(path, port, webhook, kis=None):
    stop = threading.Event()
    with closing(connect(path)) as db:
        if kis:
            initialize(db, kis)
        else:
            db.execute("UPDATE fills SET notification='재시작으로 취소' WHERE notification='대기'")

    class Handler(BaseHTTPRequestHandler):
        def send_payload(self, payload, content_type):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def trusted_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{port}"

        def do_GET(self):
            if not self.trusted_host():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if self.path == "/api/state":
                with closing(connect(path)) as db:
                    db.execute("BEGIN")
                    state = live_snapshot(db) if kis else snapshot(db)
                self.send_payload(json.dumps(state).encode(), "application/json")
            elif self.path in ASSETS:
                name, content_type = ASSETS[self.path]
                self.send_payload((ROOT / name).read_bytes(), content_type)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if not self.trusted_host() or self.headers.get("Origin") != f"http://127.0.0.1:{port}":
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            actions = {"/api/pause": 1, "/api/resume": 0}
            if kis or self.path not in actions:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            with closing(connect(path)) as db:
                db.execute("UPDATE account SET paused=? WHERE id=1", (actions[self.path],))
            self.send_payload(b"{}", "application/json")

        def log_message(self, *_args):
            pass

    def collect():
        with closing(connect(path)) as db:
            while not stop.is_set():
                second = int(time.time())
                try:
                    apply_sample(db, second, demo_sample(second), bool(webhook))
                except sqlite3.Error:
                    logging.exception("시세 저장 실패")
                stop.wait(max(0.01, second + 1 - time.time()))

    def collect_live():
        with closing(connect(path)) as db:
            next_account = 0
            while not stop.is_set():
                now = time.monotonic()
                errors = []
                try:
                    record_quotes(db, kis)
                except KISError as exc:
                    errors.append(f"시세: {exc}")
                if now >= next_account:
                    next_account = now + 15
                    try:
                        record_account(db, kis)
                        reconcile(db, kis)
                    except KISError as exc:
                        errors.append(f"계좌: {exc}")
                    db.execute("UPDATE live_account SET error=? WHERE id=1", ((" / ".join(errors) or None),))
                elif errors:
                    db.execute("UPDATE live_account SET error=? WHERE id=1", (" / ".join(errors),))
                stop.wait(2 if not errors else 10)

    def notify():
        with closing(connect(path)) as db:
            send_discord(db, webhook, stop, live=bool(kis))

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        workers = [threading.Thread(target=collect_live if kis else collect)]
        if webhook:
            workers.append(threading.Thread(target=notify))
        for worker in workers:
            worker.start()
        print(f"3Q {'실계좌' if kis else '데모'}: http://127.0.0.1:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            for worker in workers:
                worker.join()


@contextmanager
def exclusive(path, suffix):
    with path.with_name(path.name + suffix).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise KISError("Another 3Q process is using this operation") from None
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    data = Path.home() / "Library/Application Support/3Q"
    for name, default in (("serve", "demo.sqlite3"), ("live", "live.sqlite3")):
        run = commands.add_parser(name)
        run.add_argument("--port", type=int, default=8033)
        run.add_argument("--db", type=Path, default=data / default)
        run.add_argument("--discord", action="store_true")
    order = commands.add_parser("order")
    order.add_argument("side", choices=("BUY", "SELL"))
    order.add_argument("symbol", choices=("TQQQ", "SQQQ"))
    order.add_argument("quantity", type=int)
    order.add_argument("limit_usd", type=order_limit)
    order.add_argument("--max-order-usd", required=True, type=order_limit)
    order.add_argument("--max-session-usd", required=True, type=order_limit)
    order.add_argument("--confirm-live", action="store_true", required=True)
    order.add_argument("--db", type=Path, default=data / "live.sqlite3")
    resolve = commands.add_parser("resolve-order")
    resolve.add_argument("id", type=int)
    resolve.add_argument("--verified-at-broker", action="store_true", required=True)
    resolution = resolve.add_mutually_exclusive_group(required=True)
    resolution.add_argument("--broker-id")
    resolution.add_argument("--closed", action="store_true")
    resolve.add_argument("--db", type=Path, default=data / "live.sqlite3")
    replay = commands.add_parser("replay")
    replay.add_argument("csv", type=Path)
    args = parser.parse_args()
    if args.command == "replay":
        try:
            with closing(connect(":memory:")) as db, args.csv.open(encoding="utf-8", newline="") as file:
                print(json.dumps(replay_csv(db, file), indent=2))
        except (OSError, ValueError, TypeError) as exc:
            parser.error(str(exc))
        return
    path = args.db.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        kis = None if args.command == "serve" else KIS()
        if args.command in ("order", "resolve-order"):
            with exclusive(path, ".order.lock"), closing(connect(path)) as db:
                initialize(db, kis)
                if args.command == "order":
                    local, remote = submit(db, kis, args.side, args.symbol, args.quantity,
                                           args.limit_usd, args.max_order_usd, args.max_session_usd)
                    print(f"KIS 접수 · 로컬 주문 #{local} · KIS 주문번호 {remote}")
                elif args.closed:
                    if not db.execute(
                        "UPDATE live_orders SET state='BROKER_CLOSED' "
                        "WHERE id=? AND state IN ('UNKNOWN','ACCEPTED','PARTIAL')", (args.id,),
                    ).rowcount:
                        raise KISError("No unresolved order with that local ID")
                else:
                    number = args.broker_id
                    if not (number.isascii() and number.isdecimal() and 1 <= len(number) <= 20 and int(number)):
                        parser.error("Use the numeric broker order ID verified at KIS")
                    if not db.execute(
                        "UPDATE live_orders SET state='ACCEPTED',broker_id=? WHERE id=? AND state='UNKNOWN'",
                        (number, args.id),
                    ).rowcount:
                        raise KISError("No unknown order with that local ID")
            return
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        webhook = None
        if args.discord:
            webhook = os.environ.get("THREEQ_DISCORD_WEBHOOK", "")
            url = urlsplit(webhook)
            if (url.scheme != "https" or url.netloc != "discord.com"
                    or not url.path.startswith("/api/webhooks/")):
                parser.error("Set THREEQ_DISCORD_WEBHOOK to a Discord incoming webhook URL")
        with exclusive(path, ".lock"):
            serve(path, args.port, webhook, kis)
    except (KISError, ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
