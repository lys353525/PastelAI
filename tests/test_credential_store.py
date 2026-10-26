import ctypes
import ctypes.util
import os
import unittest
from unittest.mock import patch

import credential_store


class CredentialStoreTests(unittest.TestCase):
    def test_provider_names_are_restricted(self):
        with self.assertRaises(ValueError):
            credential_store._target("unexpected")

    def test_non_windows_fails_closed(self):
        with patch.object(credential_store, "_windows_api", wraps=credential_store._windows_api) as api:
            with patch.object(credential_store.os, "name", "posix"):
                with self.assertRaises(credential_store.CredentialError):
                    credential_store.get_api_key("api")
            api.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows Credential Manager integration test")
    def test_credential_round_trip(self):
        value = "test-secret-" + os.urandom(8).hex()
        try:
            credential_store.set_api_key("api", value)
            self.assertEqual(credential_store.get_api_key("api"), value)
        finally:
            credential_store.delete_api_key("api")
        self.assertEqual(credential_store.get_api_key("api"), "")


if __name__ == "__main__":
    unittest.main()
