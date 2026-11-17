import json
from pathlib import Path
import tempfile
import unittest

from accounts import Account
from jobs import QueryService, proxy_outcome
from results import account_issue, collect
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


class AccountIssueTests(unittest.TestCase):
    def test_explicit_account_issues_complete_and_do_not_affect_proxy(self):
        cases = [
            ({'emailStatus':404,'emailBusinessError':{'message':'The user is not found'}}, '账号不存在'),
            ({'emailStatus':409,'emailBusinessError':{'message':'Password reset required'}}, '需要重置密码'),
            ({'uiObservations':[{'passwordResetVisible':True}], 'requiresHumanInteraction':True}, '需要重置密码'),
            ({'passwordFormBeforeSubmit':{'valueLength':7,'submitEnabled':False,'valid':True}}, '密码仅7位'),
            ({'passwordFormBeforeSubmit':{'valueLength':5,'submitEnabled':False,'tooShort':True}}, '密码格式不符合'),
        ]
        for evidence, label in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                Path(directory,'failure.png').write_bytes(b'account-evidence')
                data = collect(directory,'sample@example.invalid',{'authenticated':False,**evidence})
                self.assertEqual(data['status'],'success')
                self.assertIn(label,data['result']['verificationRequirement'])
                self.assertIsNone(data['error'])
                self.assertIsNone(proxy_outcome(data))
                self.assertEqual(data['screenshot'],b'account-evidence')
                self.assertIsNone(data['cookie_header'])

    def test_http_status_and_disabled_button_alone_are_not_terminal_account_issues(self):
        for evidence in [
            {'emailStatus':404}, {'emailStatus':409}, {'emailBusinessError':{'message':'Forbidden'}},
            {'passwordFormBeforeSubmit':{'valueLength':12,'submitEnabled':False}},
            {'passwordFormBeforeSubmit':{'valueLength':7,'submitEnabled':True}},
            {'passwordFormAfterFill':{'valueLength':7,'submitEnabled':False}},
        ]:
            with self.subTest(evidence=evidence):
                self.assertIsNone(account_issue(evidence))

    def test_cancellation_is_preserved_even_with_account_issue_and_human_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            data=collect(directory,'sample@example.invalid',{'authenticated':False,'stopped':True,
                'requiresHumanInteraction':True,'accountIssue':'password-reset-required'})
            self.assertEqual(data['status'],'cancelled')

    def test_historical_account_issue_migrates_and_stays_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'fixture.sqlite3'
            store=Store(database)
            batch=store.create_batch([Account(1,'sample@example.invalid','fixture')],'test','https://signup.ticketmaster.com/noahkahan',1)
            rid=store.runs(batch)[0]['id']
            evidence={'emailStatus':404,'responses':[{'path':'/json/account-status-validation','businessError':{'message':'The user is not found'}}]}
            with store.connect(write=True) as db:
                db.execute("UPDATE runs SET status='failed',result=? WHERE id=?",(json.dumps(evidence),rid))
            for _ in range(2):
                store=Store(database)
                row=store.runs(batch)[0]
                self.assertEqual(row['status'],'success')
                self.assertEqual(row['verification_requirement'],'账号不存在')
