import sqlite3
from pathlib import Path
from test_quality import Base
from qa.backup import backup
from qa.db import Store
from qa.demo import seed

class BackupTests(Base):
    def test_backup_restores_records_and_coaching(self):
        seed(self.store)
        e = self.store.snapshot()['evaluations'][0]
        self.store.create_task(e['id'], 'Lead', '2026-10-12', 'Practice discovery', 'test')
        dest = Path(self.tmp.name) / 'restored.db'
        backup(self.store.path, dest)
        restored = Store(dest).snapshot()
        self.assertEqual(restored['validated_count'],1)
        self.assertEqual(len(restored['calls']),3)
        self.assertEqual(restored['tasks'][0]['owner'],'Lead')
    def test_backup_never_overwrites_existing_file(self):
        dest = Path(self.tmp.name) / 'existing'
        dest.write_text('keep')
        with self.assertRaises(FileExistsError):
            backup(self.store.path,dest)
        self.assertEqual(dest.read_text(),'keep')
