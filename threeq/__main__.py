"""Monitor KIS, submit an explicit limit order, or run local research workflows."""
import argparse
import fcntl
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from contextlib import ExitStack, closing, contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .engine import STARTING_CASH, apply_sample, connect, demo_sample, replay_csv
from .keychain import CredentialError, Keychain, VARIABLES, configure
from .kis import KIS, KISError, NoRedirect
from .live import SCHEMA as LIVE_SCHEMA
from .live import bind_account, initialize, live_snapshot, market_snapshot, order_limit, reconcile, record_account, record_quotes, submit
from .releases import release_status

ROOT = Path(__file__).resolve().parents[1]
ASSETS = {
    "/": ("prototype/index.html", "text/html; charset=utf-8"),
    "/dashboard.js": ("prototype/dashboard.js", "text/javascript; charset=utf-8"),
    "/mobile.js": ("prototype/mobile.js", "text/javascript; charset=utf-8"),
    "/app.webmanifest": ("prototype/app.webmanifest", "application/manifest+json"),
    "/service-worker.js": ("prototype/service-worker.js", "text/javascript; charset=utf-8"),
    "/icon-192.png": ("prototype/icon-192.png", "image/png"),
    "/icon-512.png": ("prototype/icon-512.png", "image/png"),
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


def serve(path, port, webhook, live=False, mobile_origin=None, mobile_port=8034):
    mobile_auth = None
    if mobile_origin:
        from .mobile import AuthError, MobileAuth
        mobile_auth = MobileAuth(path, mobile_origin)
    stop = threading.Event()
    reload_credentials = threading.Event()
    allow_settings = live and sys.platform == "darwin" and not any(os.environ.get(key) for key in VARIABLES)
    with closing(connect(path)) as db:
        if live:
            db.executescript(LIVE_SCHEMA)
        else:
            db.execute("UPDATE fills SET notification='재시작으로 취소' WHERE notification='대기'")

    class Handler(BaseHTTPRequestHandler):
        mobile = False

        def send_payload(self, payload, content_type, cookie=None):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(payload)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(payload)

        def trusted_host(self):
            host = self.headers.get("Host")
            if self.mobile:
                return host in (mobile_auth.rp_id, f"127.0.0.1:{mobile_port}")
            return host == f"127.0.0.1:{port}"

        def mobile_session(self):
            return mobile_auth.session_role(self.headers.get("Cookie", ""))

        def json_body(self, maximum=32768):
            if self.headers.get_content_type() != "application/json":
                raise ValueError
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= maximum:
                raise ValueError
            self.connection.settimeout(5)
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError
            return value

        def do_GET(self):
            if not self.trusted_host():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if self.path == "/api/state":
                role = self.mobile_session() if self.mobile else "admin"
                if not role:
                    self.send_error(HTTPStatus.UNAUTHORIZED)
                    return
                with closing(connect(path)) as db:
                    db.execute("BEGIN")
                    if role == "viewer":
                        state = dict(mode="market", **market_snapshot(db))
                    else:
                        state = live_snapshot(db) if live else snapshot(db)
                        if live:
                            state["settings_available"] = allow_settings
                            state["mobile_enabled"] = bool(mobile_auth) and not self.mobile
                self.send_payload(json.dumps(state).encode(), "application/json")
            elif self.path == "/api/mobile/preferences" and self.mobile:
                self.mobile_preferences()
            elif self.path == "/api/mobile/devices" and mobile_auth and not self.mobile:
                self.send_payload(json.dumps(mobile_auth.devices()).encode(), "application/json")
            elif self.path == "/api/release" and live and not self.mobile:
                self.send_payload(json.dumps(release_status()).encode(), "application/json")
            elif self.path in ASSETS:
                name, content_type = ASSETS[self.path]
                payload = (ROOT / name).read_bytes()
                if self.path == "/" and self.mobile:
                    payload = payload.replace(b"<body>", b'<body data-mobile="true">', 1)
                self.send_payload(payload, content_type)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self):
            origin = mobile_auth.origin if self.mobile else f"http://127.0.0.1:{port}"
            if not self.trusted_host() or self.headers.get("Origin") != origin:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if self.mobile:
                if self.path == "/api/mobile/preferences":
                    self.mobile_preferences(write=True)
                elif self.path == "/api/settings/kis":
                    if allow_settings and self.mobile_session() == "owner":
                        self.save_kis_settings()
                    else:
                        self.send_error(HTTPStatus.FORBIDDEN)
                else:
                    self.handle_mobile_auth()
                return
            if mobile_auth and self.path in ("/api/mobile/pair", "/api/mobile/revoke"):
                try:
                    form = self.json_body(maximum=4096)
                    payload = {}
                    if self.path == "/api/mobile/pair":
                        role = form.get("role")
                        code = mobile_auth.issue_pairing(role, form.get("label", ""))
                        payload = {"code": code, "origin": mobile_auth.origin, "role": role}
                    else:
                        mobile_auth.revoke(form.get("id"))
                    self.send_payload(json.dumps(payload).encode(), "application/json")
                except (ValueError, OSError):
                    self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if mobile_auth and self.path == "/api/mobile/reset":
                mobile_auth.reset()
                self.send_payload(b"{}", "application/json")
                return
            if self.path == "/api/stop" and live:
                self.send_payload(b"{}", "application/json")
                self.server.shutdown()
                return
            if self.path == "/api/settings/kis" and allow_settings:
                self.save_kis_settings()
                return
            actions = {"/api/pause": 1, "/api/resume": 0}
            if live or self.path not in actions:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            with closing(connect(path)) as db:
                db.execute("UPDATE account SET paused=? WHERE id=1", (actions[self.path],))
            self.send_payload(b"{}", "application/json")

        def mobile_preferences(self, write=False):
            try:
                form = self.json_body(maximum=1024) if write else None
                preferences = mobile_auth.preferences(self.headers.get("Cookie", ""), form)
                self.send_payload(json.dumps(preferences).encode(), "application/json")
            except PermissionError:
                self.send_error(HTTPStatus.UNAUTHORIZED)
            except (ValueError, OSError):
                self.send_error(HTTPStatus.BAD_REQUEST)
            except sqlite3.Error:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)

        def handle_mobile_auth(self):
            if self.path == "/api/mobile/logout":
                cookie = mobile_auth.logout(self.headers.get("Cookie", ""))
                self.send_payload(b"{}", "application/json", cookie)
                return
            paths = {
                "/api/mobile/register/options": ("register", False),
                "/api/mobile/register/verify": ("register", True),
                "/api/mobile/login/options": ("login", False),
                "/api/mobile/login/verify": ("login", True),
            }
            if self.path not in paths:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            kind, verify = paths[self.path]
            try:
                form = self.json_body()
                if verify:
                    cookie = mobile_auth.verify(kind, form)
                    self.send_payload(b"{}", "application/json", cookie)
                else:
                    options = mobile_auth.options(kind, form)
                    self.send_payload(json.dumps(options).encode(), "application/json")
            except AuthError as exc:
                self.send_error(HTTPStatus.UNAUTHORIZED, str(exc))
            except (ValueError, OSError):
                self.send_error(HTTPStatus.BAD_REQUEST)
            except sqlite3.Error:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)

        def save_kis_settings(self):
            try:
                form = self.json_body(maximum=4096)
                candidate = KIS(tuple(form[key] for key in ("key", "secret", "account", "product")))
            except (ValueError, KeyError, TypeError, KISError, OSError):
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid KIS settings")
                return
            try:
                with closing(connect(path)) as db:
                    db.execute("BEGIN IMMEDIATE")
                    bind_account(db, candidate)
                    Keychain().write((candidate.key, candidate.secret, candidate.cano, candidate.product))
                    db.commit()
            except KISError:
                self.send_error(HTTPStatus.CONFLICT, "Database linked to a different account")
                return
            except (CredentialError, OSError, sqlite3.Error):
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not save settings")
                return
            reload_credentials.set()
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
            kis = None
            while not stop.is_set():
                if kis is None or reload_credentials.is_set():
                    reload_credentials.clear()
                    try:
                        kis = KIS()
                        initialize(db, kis)
                    except KISError:
                        kis = None
                        reload_credentials.wait(5)
                        continue
                    next_account = 0
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
                reload_credentials.wait(2 if not errors else 10)

    def notify():
        with closing(connect(path)) as db:
            send_discord(db, webhook, stop, live=live)

    class MobileHandler(Handler):
        mobile = True

    with ExitStack() as stack:
        server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", port), Handler))
        if mobile_auth:
            mobile_server = stack.enter_context(
                ThreadingHTTPServer(("127.0.0.1", mobile_port), MobileHandler)
            )
            mobile_thread = threading.Thread(target=mobile_server.serve_forever)
            mobile_thread.start()
            stack.callback(mobile_thread.join)
            stack.callback(mobile_server.shutdown)
        workers = [threading.Thread(target=collect_live if live else collect)]
        if webhook:
            workers.append(threading.Thread(target=notify))
        for worker in workers:
            worker.start()
        print(f"3Q {'실계좌' if live else '데모'}: http://127.0.0.1:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            reload_credentials.set()
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
    commands.add_parser("configure-kis", help="save production KIS credentials to macOS Keychain")
    data = Path.home() / "Library/Application Support/3Q"
    for name, default in (("serve", "demo.sqlite3"), ("live", "live.sqlite3")):
        run = commands.add_parser(name)
        run.add_argument("--port", type=int, default=8033)
        run.add_argument("--db", type=Path, default=data / default)
        run.add_argument("--discord", action="store_true")
    run.add_argument("--mobile-origin", help="trusted Tailscale Serve HTTPS origin for phones")
    run.add_argument("--mobile-port", type=int, default=8034, help="loopback port for the mobile proxy")
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
    if args.command == "configure-kis":
        try:
            configure()
        except CredentialError as exc:
            parser.error(str(exc))
        print("KIS 실계좌 설정을 macOS 키체인에 저장했습니다.")
        return
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
        if args.command in ("order", "resolve-order"):
            kis = KIS()
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
        mobile_origin = getattr(args, "mobile_origin", None)
        mobile_port = getattr(args, "mobile_port", 8034)
        if mobile_origin and (not 1 <= mobile_port <= 65535 or mobile_port == args.port):
            parser.error("Set a valid mobile port distinct from the admin port")
        with exclusive(path, ".lock"):
            serve(path, args.port, webhook, live=args.command == "live",
                  mobile_origin=mobile_origin, mobile_port=mobile_port)
    except (KISError, ValueError, OSError, ImportError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
