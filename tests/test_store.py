"""Exercise concurrent result writes without real accounts or browser processes."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from accounts import Account
from runner import DEFAULT_URL
from store import Store


class StoreConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / 'concurrency.sqlite3')

    def test_sixty_concurrent_accounts_save_progress_and_screenshots(self):
        batch = self.store.create_batch([Account(i, f'user{i}@example.invalid', 'fixture') for i in range(60)], 'concurrent', DEFAULT_URL, 60)
        ids = [row['id'] for row in self.store.runs(batch)]
        with self.store.connect(write=True) as db:
            db.execute("UPDATE runs SET status='running' WHERE batch_id=?", (batch,))
        screenshot = bytes(range(256)) * 1024
        def save(run_id):
            self.store.progress(run_id, {'stage':'capture_page'})
            self.store.finish_run(run_id, {'status':'success','screenshot':screenshot,'result':{}})
        with ThreadPoolExecutor(max_workers=60) as executor:
            list(executor.map(save, ids))
        self.assertEqual({row['status'] for row in self.store.runs(batch)}, {'success'})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT SUM(LENGTH(screenshot)) FROM runs').fetchone()[0], 60 * len(screenshot))

    def test_writers_queue_while_wal_readers_continue(self):
        batch = self.store.create_batch([Account(1,'fixture@example.invalid','fixture')], 'queue', DEFAULT_URL)
        entered, release, queued, completed = (threading.Event() for _ in range(4))
        def first():
            with self.store.connect(write=True) as db:
                db.execute("UPDATE batches SET name='first' WHERE id=?", (batch,))
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError('test did not release writer')
        def second():
            queued.set()
            with self.store.connect(write=True) as db:
                name = db.execute('SELECT name FROM batches WHERE id=?', (batch,)).fetchone()[0]
                self.assertEqual(name, 'first')
                db.execute("UPDATE batches SET name='second' WHERE id=?", (batch,))
            completed.set()
        with ThreadPoolExecutor(max_workers=2) as executor:
            one = executor.submit(first)
            self.assertTrue(entered.wait(timeout=3))
            two = executor.submit(second)
            try:
                self.assertTrue(queued.wait(timeout=3))
                self.assertFalse(completed.wait(timeout=0.1))
                self.assertEqual(self.store.batch(batch)['name'], 'queue')
            finally:
                release.set()
            one.result(timeout=5); two.result(timeout=5)
        self.assertEqual(self.store.batch(batch)['name'], 'second')

    def test_failed_write_rolls_back_and_releases_queue(self):
        batch = self.store.create_batch([Account(1,'fixture@example.invalid','fixture')], 'rollback', DEFAULT_URL)
        with self.assertRaises(ValueError):
            with self.store.connect(write=True) as db:
                db.execute("UPDATE batches SET name='discard' WHERE id=?", (batch,))
                raise ValueError('fixture')
        self.assertEqual(self.store.batch(batch)['name'], 'rollback')
        self.store.finish_batch(batch)
        self.assertEqual(self.store.batch(batch)['status'], 'complete')

    def test_result_serialization_does_not_hold_the_writer_lock(self):
        from store import dumps
        batch = self.store.create_batch([Account(1, 'fixture@example.invalid', 'fixture')], 'serialization', DEFAULT_URL)
        run_id = self.store.runs(batch)[0]['id']
        serializing, release = threading.Event(), threading.Event()
        result = {'verificationRequirement': '账号密码错误'}

        def slow_dumps(value):
            if value is result:
                serializing.set()
                if not release.wait(5):
                    raise TimeoutError('serialization was not released')
            return dumps(value)

        with patch('store.dumps', slow_dumps), ThreadPoolExecutor(max_workers=2) as executor:
            saving = executor.submit(self.store.finish_run, run_id, {'status': 'success', 'result': result})
            try:
                self.assertTrue(serializing.wait(3))
                other_write = executor.submit(self.store.finish_batch, batch)
                other_write.result(timeout=2)
            finally:
                release.set()
            saving.result(timeout=3)
        self.assertEqual(self.store.runs(batch)[0]['verification_requirement'], '账号密码错误')

    def test_requirement_projection_tracks_updates_clears_rollback_and_delete(self):
        batch = self.store.create_batch([Account(1, 'fixture@example.invalid', 'fixture')], 'projection', DEFAULT_URL)
        run_id = self.store.runs(batch)[0]['id']
        with self.store.connect(write=True) as db:
            db.execute('UPDATE runs SET result=? WHERE id=?', (json.dumps({'verificationRequirement': '账号密码错误'}), run_id))
        self.assertEqual(self.store.runs(batch)[0]['verification_requirement'], '账号密码错误')
        with self.assertRaises(ValueError), self.store.connect(write=True) as db:
            db.execute('UPDATE runs SET result=NULL WHERE id=?', (run_id,))
            raise ValueError('rollback')
        self.assertEqual(self.store.runs(batch)[0]['verification_requirement'], '账号密码错误')
        with self.store.connect(write=True) as db:
            db.execute('UPDATE runs SET result=NULL WHERE id=?', (run_id,))
        self.assertIsNone(self.store.runs(batch)[0]['verification_requirement'])
        with self.store.connect(write=True) as db:
            db.execute('INSERT INTO runs(batch_id,source_line,email,password,result) VALUES(?,?,?,?,?)',
                       (batch, 2, 'second@example.invalid', 'fixture', '{"verificationRequirement":"账号不存在"}'))
        self.assertEqual(self.store.runs(batch)[1]['verification_requirement'], '账号不存在')
        with self.store.connect(write=True) as db:
            db.execute('DELETE FROM runs WHERE batch_id=?', (batch,))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM run_requirements').fetchone()[0], 0)

    def test_existing_database_backfills_projection_once_and_preserves_screenshot(self):
        batch = self.store.create_batch([Account(1, 'fixture@example.invalid', 'fixture')], 'migration', DEFAULT_URL)
        run_id = self.store.runs(batch)[0]['id']
        self.store.finish_run(run_id, {'status': 'success', 'result': {'verificationRequirement': '账号密码错误'}, 'screenshot': b'evidence'})
        with self.store.connect(write=True) as db:
            db.execute('DROP TRIGGER runs_requirement_insert')
            db.execute('DROP TRIGGER runs_requirement_update')
            db.execute('DROP TABLE run_requirements')
        for _ in range(2):
            migrated = Store(self.store.path)
            self.assertEqual(migrated.runs(batch)[0]['verification_requirement'], '账号密码错误')
            self.assertEqual(migrated.run(run_id)['screenshot'], b'evidence')
