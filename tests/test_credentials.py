import json
from pathlib import Path
import tempfile
import unittest

from accounts import Account
from jobs import QueryService, proxy_outcome
from results import collect
from store import Store


def rejected_result(status=200):
    return {"authenticated": False, "passwordStatus": status,
            "failureStage": "wait_exchange", "errorMessage": "COM exchange not observed",
            "responses": [{"path": "/json/sign-in", "status": status,
                           "businessError": {"errorType": "Operation Not Allowed",
                                             "message": "Credentials mismatch 1 attempts used", "responseCode": 403}}]}


class CredentialResultTests(unittest.TestCase):
    def test_business_rejection_is_complete_without_claiming_authentication(self):
        for status in (200, 403):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                screenshot = b"saved-error-page"
                Path(directory, 'failure.png').write_bytes(screenshot)
                data = collect(directory, 'sample@example.invalid', rejected_result(status))
                self.assertEqual(data['status'], 'success')
                self.assertEqual(data['result']['verificationRequirement'], '账号密码错误')
                self.assertFalse(data['result']['authenticated'])
                self.assertIsNone(data['error'])
                self.assertIsNone(data['cookie_header'])
                self.assertEqual(data['screenshot'], screenshot)
                self.assertIsNone(proxy_outcome(data))

    def test_generic_403_and_callback_timeout_are_still_retryable(self):
        for status in (200, 403):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                summary = rejected_result(status)
                summary['responses'][0]['businessError']['message'] = 'Forbidden'
                data = collect(directory, 'sample@example.invalid', summary)
                self.assertEqual(data['status'], 'failed')
                self.assertNotIn('verificationRequirement', data['result'])

    def test_stopped_query_does_not_become_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            data = collect(directory, 'sample@example.invalid', {**rejected_result(), 'stopped': True})
            self.assertEqual(data['status'], 'cancelled')

    def test_historical_rejection_is_migrated_and_stays_complete_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'test.sqlite3'
            store = Store(database)
            batch = store.create_batch([Account(1, 'sample@example.invalid', 'fixture-password')], 'test', 'https://signup.ticketmaster.com/noahkahan', 1)
            run_id = store.runs(batch)[0]['id']
            with store.connect(write=True) as db:
                db.execute("UPDATE runs SET status='failed',result=?,error='COM exchange not observed',screenshot=? WHERE id=?", (json.dumps(rejected_result()), b'evidence', run_id))
            for _ in range(2):
                store = Store(database)
                row = store.runs(batch)[0]
                self.assertEqual(row['status'], 'success')
                self.assertEqual(row['verification_requirement'], '账号密码错误')
                self.assertEqual(store.batches()[0]['success'], 1)
                with store.connect() as db:
                    self.assertEqual(db.execute('SELECT screenshot FROM runs WHERE id=?', (run_id,)).fetchone()['screenshot'], b'evidence')
            store.finish_batch(batch)
            service = QueryService(store)
            with self.assertRaisesRegex(ValueError, '没有需要重试'):
                service.retry(batch, scope='continuous')
            service.retry(batch, run_id=run_id)
            self.assertEqual(store.run_status(run_id), 'pending')
