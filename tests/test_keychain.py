import os
import unittest
from unittest.mock import patch

from threeq import keychain
from threeq.kis import KIS

VALUES = ("test-app-key", "test-app-secret", "12345678", "01")


class CredentialTest(unittest.TestCase):
    def test_configure_saves_complete_input(self):
        with (
            patch.object(keychain, "Keychain") as store,
            patch.object(keychain.sys.stdin, "isatty", return_value=True),
            patch.object(keychain.getpass, "getpass", side_effect=VALUES),
        ):
            keychain.configure()
            store.return_value.write.assert_called_once_with(VALUES)

    def test_cancelled_entry_does_not_replace_existing_settings(self):
        with (
            patch.object(keychain, "Keychain") as store,
            patch.object(keychain.sys.stdin, "isatty", return_value=True),
            patch.object(keychain.getpass, "getpass", side_effect=[VALUES[0], EOFError]),
        ):
            with self.assertRaises(keychain.CredentialError):
                keychain.configure()
            store.return_value.write.assert_not_called()

    def test_kis_loads_keychain_when_no_environment_override(self):
        with (
            patch.dict(os.environ, clear=True),
            patch.object(keychain.sys, "platform", "darwin"),
            patch.object(keychain, "Keychain") as store,
        ):
            store.return_value.read.return_value = VALUES
            kis = KIS()
            self.assertEqual((kis.key, kis.secret, kis.cano, kis.product), VALUES)

    def test_environment_cannot_mix_with_keychain(self):
        with (
            patch.dict(os.environ, {keychain.VARIABLES[0]: VALUES[0]}, clear=True),
            patch.object(keychain, "Keychain") as store,
        ):
            with self.assertRaises(keychain.CredentialError):
                keychain.load_credentials()
            store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
