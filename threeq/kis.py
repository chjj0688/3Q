"""KIS overseas-stock production API. Trading requests are never retried."""
import json
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .keychain import CredentialError, load_credentials, validate

BASE = "https://openapi.koreainvestment.com:9443"
TRADING = "/uapi/overseas-stock/v1/trading"
SYMBOLS = ("QQQ", "TQQQ", "SQQQ")


class KISError(RuntimeError):
    pass


class KISRejected(KISError):
    """A response explicitly reported application-level rejection."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def cents(value):
    try:
        price = Decimal(str(value)) * 100
        if not price.is_finite() or price <= 0 or price > 100_000_000:
            raise ValueError
        return int(price.quantize(Decimal("1")))
    except (InvalidOperation, ValueError) as exc:
        raise KISError("Invalid KIS price") from exc


class KIS:
    def __init__(self, credentials=None):
        try:
            self.key, self.secret, self.cano, self.product = (
                load_credentials() if credentials is None else validate(credentials)
            )
        except CredentialError as exc:
            raise KISError(str(exc)) from None
        self.opener = build_opener(NoRedirect())
        self.token = None
        self.expires = 0

    def request(self, path, *, query=None, body=None, headers=None):
        if query is not None:
            path += "?" + urlencode(query)
        request = Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with self.opener.open(request, timeout=6) as response:
                data = json.load(response)
                continuation = response.headers.get("tr_cont", "")
        except HTTPError as exc:
            exc.close()
            raise KISError(f"KIS HTTP {exc.code}") from None
        except (URLError, OSError, ValueError):
            raise KISError("KIS connection or response error") from None
        if not isinstance(data, dict):
            raise KISError("Invalid KIS response")
        if "rt_cd" in data and str(data["rt_cd"]) != "0":
            code = str(data.get("msg_cd", ""))
            safe_code = code if re.fullmatch(r"[A-Za-z0-9]{1,20}", code) else "unknown"
            raise KISRejected(f"KIS rejected request ({safe_code})")
        return data, continuation

    def authenticate(self):
        if time.monotonic() < self.expires and self.token:
            return
        data, _ = self.request("/oauth2/tokenP", body={
            "grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret,
        })
        token = data.get("access_token")
        try:
            expires_in = int(data.get("expires_in", 0))
        except (ValueError, TypeError):
            expires_in = 0
        if not isinstance(token, str) or not token or expires_in <= 60:
            raise KISError("KIS did not return a usable access token")
        self.token = token
        self.expires = time.monotonic() + expires_in - 60

    def trade_api(self, path, tr_id, *, query=None, body=None, cont=""):
        self.authenticate()
        data, continuation = self.request(path, query=query, body=body, headers={
            "authorization": f"Bearer {self.token}", "appkey": self.key,
            "appsecret": self.secret, "tr_id": tr_id, "custtype": "P", "tr_cont": cont,
        })
        if str(data.get("rt_cd")) != "0":
            raise KISError("KIS response lacks a success code")
        return data, continuation

    @property
    def account(self):
        return {"CANO": self.cano, "ACNT_PRDT_CD": self.product}

    def quote(self, symbol):
        data, _ = self.trade_api("/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                                 query={"AUTH": "", "EXCD": "NAS", "SYMB": symbol})
        return cents(data.get("output", {}).get("last"))

    def pages(self, endpoint, tr_id, params, output):
        fk = nk = cont = ""
        for _ in range(10):
            data, more = self.trade_api(TRADING + endpoint, tr_id, cont=cont, query={
                **self.account, **params, "CTX_AREA_FK200": fk, "CTX_AREA_NK200": nk,
            })
            rows = data.get(output)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise KISError("Invalid KIS account response")
            yield from rows
            if more not in ("M", "F"):
                return
            fk = data.get("ctx_area_fk200", "")
            nk = data.get("ctx_area_nk200", "")
            if not (fk or nk):
                raise KISError("KIS pagination cursor missing")
            cont = "N"
        raise KISError("KIS pagination limit exceeded")

    def holdings(self):
        return list(self.pages("/inquire-balance", "TTTS3012R",
                               {"OVRS_EXCG_CD": "NASD", "TR_CRCY_CD": "USD"}, "output1"))

    def executions(self, start, end):
        return list(self.pages("/inquire-ccnl", "TTTS3035R", {
            "PDNO": "%", "ORD_STRT_DT": start, "ORD_END_DT": end,
            "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00", "OVRS_EXCG_CD": "NASD",
            "SORT_SQN": "DS", "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
        }, "output"))

    def order(self, side, symbol, quantity, limit_cents):
        data, _ = self.trade_api(TRADING + "/order",
                                 "TTTT1002U" if side == "BUY" else "TTTT1006U", body={
            **self.account, "OVRS_EXCG_CD": "NASD", "PDNO": symbol, "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": f"{Decimal(limit_cents) / 100:.2f}", "CTAC_TLNO": "",
            "MGCO_APTM_ODNO": "", "SLL_TYPE": "" if side == "BUY" else "00",
            "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
        })
        output = data.get("output")
        number = str(output.get("ODNO", "")).strip() if isinstance(output, dict) else ""
        if not re.fullmatch(r"[0-9]{1,20}", number) or int(number) == 0:
            raise KISError("Order response did not include a usable order number")
        return number
