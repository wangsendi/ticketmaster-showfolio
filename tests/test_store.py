"""Exercise concurrent result writes without real accounts or browser processes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

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
