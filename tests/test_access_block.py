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

    def test_email_business_error_is_preserved_without_guessing_account_block(self):
        with tempfile.TemporaryDirectory() as directory:
            result = collect(directory, 'sample@example.invalid', {
                'authenticated':False,'emailStatus':409,'failureStage':'email_once',
                'responses':[{'path':'/json/account-status-validation','status':409,
                              'businessError':{'message':'Request conflict'}}]})
        self.assertEqual(result['status'], 'failed')
        self.assertIn('HTTP 409', result['error'])
        self.assertIn('Request conflict', result['error'])
