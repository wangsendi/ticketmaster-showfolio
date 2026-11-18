import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from helpers import successful_run
from results import collect


class SharedHarTests(unittest.TestCase):
    def test_one_read_preserves_audit_hash_and_selected_markets(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = successful_run(tmp, "fixture@example.invalid")
            har_path = Path(tmp) / "com-login.har"
            expected_hash = hashlib.sha256(har_path.read_bytes()).hexdigest()
            original = Path.read_bytes
            reads = []

            def read(path):
                if path == har_path:
                    reads.append(path)
                return original(path)

            with patch.object(Path, "read_bytes", read):
                data = collect(tmp, "fixture@example.invalid", summary)
            self.assertEqual(len(reads), 1)
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["result"]["verification"]["harSHA256"], expected_hash)
            self.assertEqual(data["selections"][0]["id"], "m1")
            self.assertTrue(data["selections"][0]["resolved"])

    def test_shared_parse_still_rejects_wrong_account_and_cookie(self):
        for wrong_account in (True, False):
            with self.subTest(wrong_account=wrong_account), tempfile.TemporaryDirectory() as tmp:
                summary = successful_run(tmp, "fixture@example.invalid")
                if not wrong_account:
                    (Path(tmp) / "cookie-header.txt").write_text("SID=wrong")
                data = collect(tmp, "other@example.invalid" if wrong_account else "fixture@example.invalid", summary)
                self.assertEqual(data["status"], "failed")
                self.assertIsNone(data["cookie_header"])
                self.assertIsNone(data["selections"])
