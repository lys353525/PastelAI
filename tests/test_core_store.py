import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = core.LocalStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_save_and_load_chats_round_trip(self):
        chats = {"chat1": {"title": "대화", "messages": [{"id": "msg1", "role": "user", "text": "안녕", "attachments": [], "artifacts": []}]}}
        self.store.save_chats(chats)
        loaded = self.store.load_chats()
        self.assertEqual(loaded["chat1"]["title"], "대화")
        self.assertEqual(loaded["chat1"]["messages"][0]["text"], "안녕")

    def test_invalid_chat_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.chat_dir("../escape")

    def test_malformed_chat_records_warning(self):
        self.store.history_file.write_text('{"bad-id?": {}}', encoding="utf-8")
        self.assertEqual(self.store.load_chats(), {})
        self.assertTrue(self.store.warnings)

    def test_settings_never_persist_api_key(self):
        with patch.object(core.credential_store, "set_api_key") as setter:
            self.store.save_settings({"provider": "api", "api_key": "hidden-test-key", "model": "m"})
        setter.assert_called_once_with("api", "hidden-test-key")
        payload = json.loads(self.store.settings_file.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", payload)

    def test_legacy_key_migrates_then_is_removed(self):
        self.store.settings_file.write_text(json.dumps({"provider": "api", "api_key": "legacy-test-key"}), encoding="utf-8")
        with patch.object(core.credential_store, "get_api_key", return_value=""), patch.object(core.credential_store, "set_api_key") as setter:
            settings = self.store.settings()
        setter.assert_called_once_with("api", "legacy-test-key")
        self.assertEqual(settings["api_key"], "legacy-test-key")
        payload = json.loads(self.store.settings_file.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", payload)


if __name__ == "__main__":
    unittest.main()
