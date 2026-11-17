from pathlib import Path
import tempfile
import unittest

from helpers import PNG
from results import collect


class BrowsingPauseTests(unittest.TestCase):
    def test_access_block_is_retryable_failure_with_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "observed-verification.png").write_bytes(PNG)
            result = collect(directory, "example@example.invalid", {
                "authenticated":False, "requiresHumanInteraction":True,
                "browsingActivityPaused":True, "failureStage":"wait_email",
                "errorMessage":"Target page closed"})
        self.assertEqual(result["status"], "failed")
        self.assertIn("暂停", result["error"])
        self.assertEqual(result["screenshot"], PNG)
        self.assertIsNone(result["cookie_header"])
