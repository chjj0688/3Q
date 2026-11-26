"""Store the production KIS configuration as one macOS Keychain item."""
import ctypes as c
import getpass
import json
import os
import re
import sys
import warnings

SERVICE = b"com.threeq.kis.production"
ACCOUNT = b"production"
NOT_FOUND = -25300
DUPLICATE = -25299
VARIABLES = (
    "THREEQ_KIS_APP_KEY", "THREEQ_KIS_APP_SECRET", "THREEQ_KIS_ACCOUNT", "THREEQ_KIS_PRODUCT",
)


class CredentialError(ValueError):
    pass


def validate(values):
    if not (len(values) == 4 and all(isinstance(value, str) and value for value in values)
            and re.fullmatch(r"[0-9]{8}", values[2]) and re.fullmatch(r"[0-9]{2}", values[3])):
        raise CredentialError("Enter the production key, secret, 8-digit account and 2-digit product")
    return tuple(values)


class Keychain:
    def __init__(self):
        if sys.platform != "darwin":
            raise CredentialError("KIS Keychain setup requires macOS")
        security = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
        foundation = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        key = [c.c_void_p, c.c_uint32, c.c_char_p, c.c_uint32, c.c_char_p]
        pointer = c.POINTER(c.c_void_p)
        length = c.POINTER(c.c_uint32)
        self.find = security.SecKeychainFindGenericPassword
        self.find.argtypes = [*key, length, pointer, pointer]
        self.add = security.SecKeychainAddGenericPassword
        self.add.argtypes = [*key, c.c_uint32, c.c_char_p, pointer]
        self.modify = security.SecKeychainItemModifyAttributesAndData
        self.modify.argtypes = [c.c_void_p, c.c_void_p, c.c_uint32, c.c_char_p]
        self.free = security.SecKeychainItemFreeContent
        self.free.argtypes = [c.c_void_p, c.c_void_p]
        self.release = foundation.CFRelease
        self.release.argtypes = [c.c_void_p]
        self.release.restype = None
        self.key = (None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT)

    @staticmethod
    def check(status):
        if status:
            raise CredentialError(f"macOS Keychain operation failed (OSStatus {status})")

    def read(self):
        size = c.c_uint32()
        data = c.c_void_p()
        status = self.find(*self.key, c.byref(size), c.byref(data), None)
        if status == NOT_FOUND:
            return None
        self.check(status)
        try:
            values = json.loads(c.string_at(data, size.value))
            if not isinstance(values, list):
                raise ValueError
            return validate(values)
        except (ValueError, UnicodeError):
            raise CredentialError("Invalid 3Q Keychain entry. Run configure-kis again.") from None
        finally:
            self.free(None, data)

    def write(self, values):
        data = json.dumps(validate(values)).encode()
        status = self.add(*self.key, len(data), data, None)
        if status != DUPLICATE:
            self.check(status)
            return
        item = c.c_void_p()
        self.check(self.find(*self.key, None, None, c.byref(item)))
        try:
            self.check(self.modify(item, None, len(data), data))
        finally:
            self.release(item)


def load_credentials():
    values = tuple(os.environ.get(name, "") for name in VARIABLES)
    if any(values):
        return validate(values)
    if sys.platform == "darwin":
        entry = Keychain().read()
        if entry is not None:
            return entry
        raise CredentialError("KIS credentials are missing. Run python3 -m threeq configure-kis")
    raise CredentialError("Configure all four THREEQ_KIS environment variables")


def configure():
    keychain = Keychain()
    if not sys.stdin.isatty():
        raise CredentialError("Run configure-kis in an interactive Mac terminal")
    labels = ("실전용 APP KEY", "실전용 APP SECRET", "실계좌 번호 앞 8자리", "계좌 상품코드 뒤 2자리")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            values = tuple(getpass.getpass(f"{label}: ").strip() for label in labels)
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        raise CredentialError("입력을 취소했습니다. 키체인은 변경되지 않았습니다.") from None
    keychain.write(values)
