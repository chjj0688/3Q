"""Passkey registration and login for the separate mobile HTTPS origin."""
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from hashlib import sha256
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from webauthn import (
    base64url_to_bytes, generate_authentication_options, generate_registration_options,
    options_to_json, verify_authentication_response, verify_registration_response,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import AuthenticatorSelectionCriteria, ResidentKeyRequirement, UserVerificationRequirement

COOKIE = "__Host-threeq"
SESSION_SECONDS = 8 * 60 * 60
ROLES = ('owner', 'viewer')


class AuthError(ValueError):
    pass


class MobileAuth:
    def __init__(self, path, origin):
        url = urlsplit(origin)
        if (origin != f"https://{url.hostname}" or not url.hostname
                or not re.fullmatch(r"[a-z0-9][a-z0-9.-]+\.ts\.net", url.hostname)):
            raise ValueError("Mobile origin must be the HTTPS Tailscale Serve hostname")
        self.path = path
        self.origin = origin
        self.rp_id = url.hostname
        self.lock = threading.Lock()
        self.pairing = None
        self.challenges = {}
        self.sessions = {}
        with closing(self.connect()) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS passkeys (
                    rp TEXT NOT NULL, id BLOB NOT NULL, public_key BLOB NOT NULL,
                    sign_count INTEGER NOT NULL, PRIMARY KEY (rp, id)
                )
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(passkeys)")}
            if "role" not in columns:
                db.execute("ALTER TABLE passkeys ADD COLUMN role TEXT NOT NULL DEFAULT 'owner' "
                           "CHECK (role IN ('owner','viewer'))")
            if "label" not in columns:
                db.execute("ALTER TABLE passkeys ADD COLUMN label TEXT NOT NULL DEFAULT ''")
            if "vix_threshold" not in columns:
                db.execute("ALTER TABLE passkeys ADD COLUMN vix_threshold REAL")

    def connect(self):
        return sqlite3.connect(self.path, isolation_level=None, timeout=5)

    def prune(self):
        now = time.monotonic()
        for entries in (self.challenges, self.sessions):
            for token, record in list(entries.items()):
                if record[0] <= now:
                    del entries[token]

    def issue_pairing(self, role, label):
        if role not in ROLES or not isinstance(label, str) or len(label) > 48:
            raise AuthError("Invalid invite role or label.")
        with self.lock:
            code = secrets.token_hex(6).upper()
            self.pairing = (code, time.monotonic() + 300, 0, role, label.strip())
            self.challenges = {
                token: record for token, record in self.challenges.items() if record[1] != "register"
            }
            return code

    def devices(self):
        with self.lock, closing(self.connect()) as db:
            return [
                {"id": sha256(key).hexdigest(), "role": role, "label": label}
                for key, role, label in db.execute(
                    "SELECT id,role,label FROM passkeys WHERE rp=? ORDER BY rowid", (self.rp_id,),
                )
            ]

    def revoke(self, device):
        if not isinstance(device, str):
            raise AuthError("Invalid device.")
        with self.lock, closing(self.connect()) as db:
            for (key,) in db.execute("SELECT id FROM passkeys WHERE rp=?", (self.rp_id,)):
                if sha256(key).hexdigest() == device:
                    db.execute("DELETE FROM passkeys WHERE rp=? AND id=?", (self.rp_id, key))
                    self.sessions = {token: session for token, session in self.sessions.items() if session[2] != key}
                    return
            raise AuthError("Device not found.")

    def reset(self):
        with self.lock, closing(self.connect()) as db:
            db.execute("DELETE FROM passkeys WHERE rp=?", (self.rp_id,))
            self.pairing = None
            self.challenges.clear()
            self.sessions.clear()

    def options(self, kind, body):
        with self.lock:
            self.prune()
            if len(self.challenges) >= 64:
                raise AuthError("Too many authentication requests. Wait and try again.")
            verification = UserVerificationRequirement.REQUIRED
            role = label = None
            if kind == "register":
                if self.pairing is None:
                    raise AuthError("Request a pairing code on the Mac first.")
                code, expires, attempts, role, label = self.pairing
                submitted = body.get("code")
                if expires <= time.monotonic() or attempts >= 8:
                    self.pairing = None
                    raise AuthError("Pairing code expired. Generate a new code on the Mac.")
                if not (isinstance(submitted, str) and submitted.isascii()
                        and secrets.compare_digest(code, submitted.strip().upper())):
                    self.pairing = (code, expires, attempts + 1, role, label)
                    raise AuthError("Incorrect pairing code.")
                self.pairing = None
                handle = secrets.token_bytes(16)
                options = generate_registration_options(
                    rp_id=self.rp_id, rp_name="3Q", user_id=handle, user_name=f"3q-{handle.hex()}",
                    user_display_name=label or ("3Q 소유자" if role == "owner" else "3Q 시세 보기"),
                    authenticator_selection=AuthenticatorSelectionCriteria(
                        resident_key=ResidentKeyRequirement.REQUIRED, user_verification=verification,
                    ),
                )
            else:
                options = generate_authentication_options(
                    rp_id=self.rp_id, user_verification=verification,
                )
            token = secrets.token_urlsafe(24)
            self.challenges[token] = (time.monotonic() + 120, kind, options.challenge, role, label)
            return {"request": token, "options": json.loads(options_to_json(options))}

    def verify(self, kind, body):
        with self.lock, closing(self.connect()) as db:
            self.prune()
            token = body.get("request")
            challenge = self.challenges.pop(token, None) if isinstance(token, str) else None
            if challenge is None or challenge[1] != kind:
                raise AuthError("Authentication request expired. Try again.")
            credential = body.get("credential")
            if not isinstance(credential, dict):
                raise AuthError("Invalid passkey response.")
            arguments = {
                "credential": credential, "expected_challenge": challenge[2],
                "expected_rp_id": self.rp_id, "expected_origin": self.origin,
                "require_user_verification": True,
            }
            try:
                if kind == "register":
                    result = verify_registration_response(**arguments)
                    credential_id = result.credential_id
                    role, label = challenge[3:]
                    db.execute("INSERT INTO passkeys (rp,id,public_key,sign_count,role,label) VALUES (?, ?, ?, ?, ?, ?)",
                               (self.rp_id, credential_id, result.credential_public_key, result.sign_count, role, label))
                else:
                    credential_id = base64url_to_bytes(credential["rawId"])
                    row = db.execute(
                        "SELECT public_key, sign_count, role FROM passkeys WHERE rp=? AND id=?",
                        (self.rp_id, credential_id),
                    ).fetchone()
                    if row is None:
                        raise AuthError("This passkey is not registered.")
                    role = row[2]
                    result = verify_authentication_response(
                        **arguments, credential_public_key=row[0], credential_current_sign_count=row[1],
                    )
                    db.execute("UPDATE passkeys SET sign_count=? WHERE rp=? AND id=?",
                               (result.new_sign_count, self.rp_id, credential_id))
            except (WebAuthnException, KeyError, ValueError, TypeError, sqlite3.IntegrityError):
                raise AuthError("Passkey verification failed.") from None
            session = secrets.token_urlsafe(32)
            if len(self.sessions) >= 32:
                del self.sessions[next(iter(self.sessions))]
            self.sessions[session] = (time.monotonic() + SESSION_SECONDS, role, credential_id)
            return f"{COOKIE}={session}; Path=/; Max-Age={SESSION_SECONDS}; Secure; HttpOnly; SameSite=Strict"

    def session_token(self, header):
        try:
            cookie = SimpleCookie(header).get(COOKIE)
        except CookieError:
            return None
        return cookie.value if cookie else None

    def session_role(self, header):
        with self.lock:
            self.prune()
            session = self.sessions.get(self.session_token(header))
            return session[1] if session else None

    def preferences(self, header, body=None):
        with self.lock, closing(self.connect()) as db:
            self.prune()
            session = self.sessions.get(self.session_token(header))
            if session is None:
                raise PermissionError
            key = (self.rp_id, session[2])
            if body is not None:
                value = body.get("vix_threshold")
                if "vix_threshold" not in body or (value is not None and (
                    type(value) not in (int, float) or not 0 < value <= 1000 or not math.isfinite(value)
                )):
                    raise AuthError("Invalid VIX threshold.")
                db.execute("UPDATE passkeys SET vix_threshold=? WHERE rp=? AND id=?", (value, *key))
            row = db.execute("SELECT vix_threshold FROM passkeys WHERE rp=? AND id=?", key).fetchone()
            if row is None:
                raise PermissionError
            return {"vix_threshold": row[0]}

    def logout(self, header):
        with self.lock:
            self.sessions.pop(self.session_token(header), None)
        return f"{COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"
