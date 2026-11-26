import base64
import hashlib
import importlib.util
import json
import secrets
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from threeq.__main__ import serve

ORIGIN = "https://threeq-example.ngrok-free.app"


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def client_data(kind, options):
    return json.dumps({"type": f"webauthn.{kind}", "challenge": options["options"]["challenge"],
                       "origin": ORIGIN}).encode()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(importlib.util.find_spec("webauthn"), "mobile dependencies are optional")
class MobileTest(unittest.TestCase):
    def test_mobile_origin_accepts_fixed_https_domains_only(self):
        from threeq.mobile import mobile_hostname

        self.assertEqual(mobile_hostname(ORIGIN), "threeq-example.ngrok-free.app")
        self.assertEqual(mobile_hostname("https://neo.example.ts.net"), "neo.example.ts.net")
        for origin in ("http://3q.example.com", "https://3q.example.com/",
                       "https://3q.example.com:443", "https://127.0.0.1",
                       "https://3q.local", "https://neo..example.com",
                       "https://user@3q.example.com"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                mobile_hostname(origin)

    def test_signed_passkey_flow_and_mobile_permissions(self):
        import cbor2
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        admin_port, mobile_port = free_port(), free_port()
        while mobile_port == admin_port:
            mobile_port = free_port()
        admin = f"http://127.0.0.1:{admin_port}"
        mobile = f"http://127.0.0.1:{mobile_port}"

        def request(address, path, body=None, origin=None, cookie=None):
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            if cookie:
                headers["Cookie"] = cookie
            data = None if body is None else json.dumps(body).encode()
            with urlopen(Request(address + path, data=data, headers=headers), timeout=3) as reply:
                return json.load(reply), reply.headers.get("Set-Cookie")

        with (tempfile.TemporaryDirectory() as directory,
              patch.dict("os.environ", clear=True),
              patch("threeq.__main__.sys.platform", "darwin"),
              patch("threeq.keychain.Keychain") as stored,
              patch("threeq.__main__.Keychain") as write_keychain):
            stored.return_value.read.return_value = None
            thread = threading.Thread(target=serve, daemon=True, args=(
                Path(directory) / "live.db", admin_port, None, True, ORIGIN, mobile_port,
            ))
            thread.start()
            try:
                for _ in range(30):
                    try:
                        request(admin, "/api/state")
                        break
                    except URLError:
                        time.sleep(0.1)
                with self.assertRaises(HTTPError) as error:
                    request(mobile, "/api/state")
                self.assertEqual(error.exception.code, 401)
                with self.assertRaises(HTTPError) as error:
                    request(mobile, "/api/mobile/pair", {}, ORIGIN)
                self.assertEqual(error.exception.code, 404)

                rp_hash = hashlib.sha256(b"threeq-example.ngrok-free.app").digest()

                def register(role):
                    pair, _ = request(admin, "/api/mobile/pair", {"role": role, "label": role}, admin)
                    options, _ = request(
                        mobile, "/api/mobile/register/options", {"code": pair["code"], "role": "owner"}, ORIGIN,
                    )
                    with self.assertRaises(HTTPError):
                        request(mobile, "/api/mobile/register/options", {"code": pair["code"]}, ORIGIN)
                    key = ec.generate_private_key(ec.SECP256R1())
                    point = key.public_key().public_numbers()
                    cose = {1: 2, 3: -7, -1: 1, -2: point.x.to_bytes(32), -3: point.y.to_bytes(32)}
                    credential_id = secrets.token_bytes(20)
                    encoded_id = encode(credential_id)
                    auth_data = (rp_hash + b"\x45" + bytes(20) + len(credential_id).to_bytes(2)
                                 + credential_id + cbor2.dumps(cose))
                    response = {"id": encoded_id, "rawId": encoded_id, "type": "public-key", "response": {
                        "clientDataJSON": encode(client_data("create", options)),
                        "attestationObject": encode(cbor2.dumps({"fmt": "none", "authData": auth_data, "attStmt": {}})),
                    }}
                    _, cookie = request(mobile, "/api/mobile/register/verify",
                                        {"request": options["request"], "credential": response}, ORIGIN)
                    return key, response, cookie

                key, response, cookie = register("owner")
                self.assertIn("Secure; HttpOnly; SameSite=Strict", cookie)
                cookie = cookie.split(";")[0]
                state, _ = request(mobile, "/api/state", cookie=cookie)
                self.assertFalse(state["mobile_enabled"])
                form = {"key": "fixture-key", "secret": "fixture-secret", "account": "12345678", "product": "01"}
                with self.assertRaises(HTTPError):
                    request(mobile, "/api/settings/kis", form, "https://invalid.test", cookie)
                request(mobile, "/api/settings/kis", form, ORIGIN, cookie)
                write_keychain.return_value.write.assert_called_once_with(tuple(form.values()))
                with self.assertRaises(HTTPError):
                    request(mobile, "/api/stop", {}, ORIGIN, cookie)
                _, _, viewer_cookie = register("viewer")
                viewer_cookie = viewer_cookie.split(";")[0]
                viewer, _ = request(mobile, "/api/state", cookie=viewer_cookie)
                self.assertEqual(set(viewer), {"mode", "latest", "quotes", "chart"})
                self.assertEqual(viewer["mode"], "market")
                with self.assertRaises(HTTPError) as error:
                    request(mobile, "/api/settings/kis", form, ORIGIN, viewer_cookie)
                self.assertEqual(error.exception.code, 403)
                with self.assertRaises(HTTPError):
                    request(mobile, "/api/mobile/devices", cookie=cookie)
                with self.assertRaises(HTTPError):
                    request(mobile, "/api/mobile/preferences")
                request(mobile, "/api/mobile/preferences", {"vix_threshold": 25}, ORIGIN, viewer_cookie)
                request(mobile, "/api/mobile/preferences", {"vix_threshold": 33}, ORIGIN, cookie)
                viewer_preferences, _ = request(mobile, "/api/mobile/preferences", cookie=viewer_cookie)
                owner_preferences, _ = request(mobile, "/api/mobile/preferences", cookie=cookie)
                self.assertEqual(viewer_preferences["vix_threshold"], 25)
                self.assertEqual(owner_preferences["vix_threshold"], 33)
                devices, _ = request(admin, "/api/mobile/devices")
                viewer_id = next(device["id"] for device in devices if device["role"] == "viewer")
                request(admin, "/api/mobile/revoke", {"id": viewer_id}, admin)
                with self.assertRaises(HTTPError):
                    request(mobile, "/api/state", cookie=viewer_cookie)
                request(mobile, "/api/state", cookie=cookie)
                request(mobile, "/api/mobile/logout", {}, ORIGIN, cookie)

                options, _ = request(mobile, "/api/mobile/login/options", {}, ORIGIN)
                auth_data = rp_hash + b"\x05" + (1).to_bytes(4)
                client = client_data("get", options)
                signature = key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
                response["response"] = {
                    "clientDataJSON": encode(client), "authenticatorData": encode(auth_data),
                    "signature": encode(signature), "userHandle": None,
                }
                payload = {"request": options["request"], "credential": response}
                _, new_cookie = request(mobile, "/api/mobile/login/verify", payload, ORIGIN)
                with self.assertRaises(HTTPError):
                    request(mobile, "/api/mobile/login/verify", payload, ORIGIN)
                request(admin, "/api/mobile/reset", {}, admin)
                with self.assertRaises(HTTPError):
                    request(mobile, "/api/state", cookie=new_cookie.split(";")[0])
            finally:
                request(admin, "/api/stop", {}, admin)
                thread.join(5)
