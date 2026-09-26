"""Checks recovery when WorkBuddy removes old rows at login."""

from pathlib import Path
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workbuddy_transfer as switch


OLD = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


def make_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, created_at INTEGER)")
        db.execute("CREATE TABLE session_usage (session_id TEXT PRIMARY KEY, used INTEGER NOT NULL, "
                   "size INTEGER NOT NULL, updated_at INTEGER NOT NULL, credit_json TEXT)")
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        db.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                       [(sid, uid, title, now_ms) for sid, uid, title in rows])
        db.commit()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="wb-switch-test-", dir=switch.SCRIPT_DIR)).resolve()
        self.assertTrue(self.base.is_relative_to(switch.SCRIPT_DIR.resolve()))
        self.original_script_dir = switch.SCRIPT_DIR
        switch.SCRIPT_DIR = self.base

    def tearDown(self):
        switch.SCRIPT_DIR = self.original_script_dir
        self.assertTrue(self.base.is_relative_to(self.original_script_dir.resolve()))
        shutil.rmtree(self.base)

    def test_restores_rows_and_records_when_current_db_has_only_new_account(self):
        old_root = self.base / "old-root"
        current_root = self.base / "current-root"
        old_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        new_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        make_db(old_root / "workbuddy.db", [(old_id, OLD, "old")])
        old_project = old_root / "projects" / "workspace"
        old_project.mkdir(parents=True)
        (old_project / f"{old_id}.jsonl").write_text('{"type":"message"}\n', encoding="utf-8")
        old_memory = old_root / "memory"
        old_memory.mkdir()
        (old_memory / f"{OLD}_memory.md").write_text("old memory", encoding="utf-8")
        source_snapshot = self.base / "backups" / "old"
        switch.snapshot(old_root, source_snapshot, require_old_uid=OLD)

        make_db(current_root / "workbuddy.db", [(new_id, NEW, "new")])
        new_project = current_root / "projects" / "workspace"
        new_project.mkdir(parents=True)
        (new_project / f"{new_id}.jsonl").write_text('{"type":"message"}\n', encoding="utf-8")
        args = SimpleNamespace(root=current_root, backups=self.base / "backups",
                               snapshot=source_snapshot, new_uid=NEW)
        switch.migrate(args)

        rows = dict(switch.session_rows(current_root / "workbuddy.db"))
        self.assertEqual(rows, {old_id: NEW, new_id: NEW})
        self.assertTrue((current_root / "projects" / "workspace" / f"{old_id}.jsonl").is_file())
        self.assertEqual((current_root / "memory" / f"{NEW}_memory.md").read_text(), "old memory")
        before = list((self.base / "backups").glob("before-migration-*"))
        self.assertEqual(len(before), 1)
        self.assertEqual(dict(switch.session_rows(before[0] / "workbuddy.db")), {new_id: NEW})
        self.assertEqual(len(list(self.base.glob("migration-report-*.json"))), 1)

    def test_many_old_sessions_with_another_preexisting_account(self):
        old_root = self.base / "old-root"
        current_root = self.base / "current-root"
        old_ids = [f"{number:08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa" for number in range(1001)]
        other_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        new_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        make_db(old_root / "workbuddy.db",
                [(sid, OLD, "old") for sid in old_ids] + [(other_id, OTHER, "other")])
        old_project = old_root / "projects" / "workspace"
        old_project.mkdir(parents=True)
        for sid in old_ids + [other_id]:
            (old_project / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        source_snapshot = self.base / "backups" / "old"
        switch.snapshot(old_root, source_snapshot, require_old_uid=OLD)

        make_db(current_root / "workbuddy.db", [(other_id, OTHER, "other"), (new_id, NEW, "new")])
        current_project = current_root / "projects" / "workspace"
        current_project.mkdir(parents=True)
        for sid in (other_id, new_id):
            (current_project / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        args = SimpleNamespace(root=current_root, backups=self.base / "backups",
                               snapshot=source_snapshot, new_uid=NEW)
        switch.migrate(args)

        rows = dict(switch.session_rows(current_root / "workbuddy.db"))
        self.assertEqual(len(rows), 1003)
        self.assertTrue(all(rows[sid] == NEW for sid in old_ids))
        self.assertEqual(rows[other_id], OTHER)
        self.assertEqual(rows[new_id], NEW)


if __name__ == "__main__":
    unittest.main()

