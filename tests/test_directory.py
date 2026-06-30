"""Tests for BackupDirectory — setup, rotation, hardlink_to_latest."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from automysqlbackup.config import Config
from automysqlbackup.directory import BackupDirectory


def _cfg(backup_dir: Path, **kwargs) -> Config:
    cfg = Config()
    cfg.backup_dir = backup_dir
    cfg.dryrun = kwargs.get("dryrun", False)
    cfg.latest = kwargs.get("latest", False)
    cfg.backup_local_files = kwargs.get("backup_local_files", [])
    cfg.full_schema = kwargs.get("full_schema", True)
    cfg.dbstatus = kwargs.get("dbstatus", True)
    return cfg


class TestSetup(unittest.TestCase):
    def test_creates_required_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp) / "backups"
            BackupDirectory(_cfg(bdir)).setup()
            for sub in ("daily", "weekly", "monthly", "latest", "tmp", "fullschema", "status"):
                self.assertTrue((bdir / sub).is_dir(), f"Missing: {sub}")

    def test_creates_fullschema_subdir_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp) / "backups"
            BackupDirectory(_cfg(bdir, full_schema=True)).setup()
            self.assertTrue((bdir / "fullschema").is_dir())

    def test_omits_fullschema_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp) / "backups"
            BackupDirectory(_cfg(bdir, full_schema=False, dbstatus=False)).setup()
            self.assertFalse((bdir / "fullschema").is_dir())

    def test_creates_status_subdir_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp) / "backups"
            BackupDirectory(_cfg(bdir, dbstatus=True)).setup()
            self.assertTrue((bdir / "status").is_dir())

    def test_creates_local_files_subdir_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp) / "backups"
            BackupDirectory(_cfg(bdir, backup_local_files=["/etc/hosts"])).setup()
            self.assertTrue((bdir / "backup_local_files").is_dir())

    def test_raises_when_parent_does_not_exist(self):
        with self.assertRaises(RuntimeError):
            BackupDirectory(_cfg(Path("/nonexistent_xyz/backups"))).setup()

    def test_dryrun_does_not_create_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp) / "backups"
            BackupDirectory(_cfg(bdir, dryrun=True)).setup()
            self.assertFalse(bdir.exists())

    def test_setup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp) / "backups"
            bd = BackupDirectory(_cfg(bdir))
            bd.setup()
            bd.setup()   # must not raise


class TestRotate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._dir = Path(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp)

    def _aged_file(self, name: str, age_days: float) -> Path:
        p = self._dir / name
        p.write_bytes(b"data")
        mtime = time.time() - age_days * 86400
        os.utime(p, (mtime, mtime))
        return p

    def test_deletes_file_older_than_cutoff(self):
        old = self._aged_file("old.sql.gz", age_days=10)
        BackupDirectory(_cfg(self._dir)).rotate(self._dir, max_age_days=7)
        self.assertFalse(old.exists())

    def test_keeps_file_newer_than_cutoff(self):
        recent = self._aged_file("recent.sql.gz", age_days=3)
        BackupDirectory(_cfg(self._dir)).rotate(self._dir, max_age_days=7)
        self.assertTrue(recent.exists())

    def test_descends_into_subdirectories(self):
        sub = self._dir / "mydb"
        sub.mkdir()
        old = sub / "old.sql.gz"
        old.write_bytes(b"data")
        mtime = time.time() - 10 * 86400
        os.utime(old, (mtime, mtime))
        BackupDirectory(_cfg(self._dir)).rotate(self._dir, max_age_days=7)
        self.assertFalse(old.exists())

    def test_dryrun_does_not_delete_anything(self):
        old = self._aged_file("old.sql.gz", age_days=10)
        BackupDirectory(_cfg(self._dir, dryrun=True)).rotate(self._dir, max_age_days=7)
        self.assertTrue(old.exists())

    def test_uses_mtime_not_ctime(self):
        # A file whose mtime is within the window but ctime might differ
        # should be kept.
        recent = self._aged_file("file.sql.gz", age_days=3)
        # Touch metadata (ctime updated) but mtime stays at 3 days ago
        os.chmod(recent, 0o644)
        BackupDirectory(_cfg(self._dir)).rotate(self._dir, max_age_days=7)
        self.assertTrue(recent.exists())


class TestHardlinkToLatest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._dir = Path(self._tmp)
        (self._dir / "latest").mkdir()

    def tearDown(self):
        shutil.rmtree(self._tmp)

    def test_creates_hardlink_in_latest(self):
        src = self._dir / "backup.sql.gz"
        src.write_bytes(b"backup data")
        BackupDirectory(_cfg(self._dir, latest=True)).hardlink_to_latest(src)
        dest = self._dir / "latest" / "backup.sql.gz"
        self.assertTrue(dest.exists())

    def test_dest_shares_inode_with_src(self):
        src = self._dir / "backup.sql.gz"
        src.write_bytes(b"data")
        BackupDirectory(_cfg(self._dir, latest=True)).hardlink_to_latest(src)
        dest = self._dir / "latest" / "backup.sql.gz"
        self.assertEqual(src.stat().st_ino, dest.stat().st_ino)

    def test_no_op_when_latest_disabled(self):
        src = self._dir / "backup.sql.gz"
        src.write_bytes(b"data")
        BackupDirectory(_cfg(self._dir, latest=False)).hardlink_to_latest(src)
        self.assertFalse((self._dir / "latest" / "backup.sql.gz").exists())

    def test_no_op_when_src_does_not_exist(self):
        BackupDirectory(_cfg(self._dir, latest=True)).hardlink_to_latest(
            self._dir / "missing.sql.gz"
        )
        self.assertEqual(list((self._dir / "latest").iterdir()), [])

    def test_overwrites_stale_hardlink(self):
        src = self._dir / "backup.sql.gz"
        src.write_bytes(b"v1")
        bd = BackupDirectory(_cfg(self._dir, latest=True))
        bd.hardlink_to_latest(src)
        # Write new content; hardlink must be refreshed
        src.write_bytes(b"v2")
        bd.hardlink_to_latest(src)
        dest = self._dir / "latest" / "backup.sql.gz"
        self.assertEqual(dest.read_bytes(), b"v2")


class TestCleanupLatest(unittest.TestCase):
    def test_removes_all_files_in_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp)
            lat = bdir / "latest"
            lat.mkdir()
            (lat / "a.sql.gz").write_bytes(b"a")
            (lat / "b.sql.gz").write_bytes(b"b")
            BackupDirectory(_cfg(bdir, latest=True)).cleanup_latest()
            self.assertEqual(list(lat.iterdir()), [])

    def test_no_op_when_latest_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp)
            lat = bdir / "latest"
            lat.mkdir()
            f = lat / "keep.sql.gz"
            f.write_bytes(b"keep")
            BackupDirectory(_cfg(bdir, latest=False)).cleanup_latest()
            self.assertTrue(f.exists())


if __name__ == "__main__":
    unittest.main()
