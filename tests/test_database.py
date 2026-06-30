"""Tests for DatabaseOps — argument builders and list_databases."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from automysqlbackup.config import Config
from automysqlbackup.compression import CompressionHandler
from automysqlbackup.database import DatabaseOps


def _make_db(**kwargs) -> tuple[DatabaseOps, Config]:
    cfg = Config()
    cfg.mysql = "/usr/bin/mysql"
    cfg.mysql_dump = "/usr/bin/mysqldump"
    cfg.mysql_show = "/usr/bin/mysqlshow"
    cfg.username = "root"
    cfg.password = ""
    cfg.host = "localhost"
    cfg.port = 3306
    cfg.socket = ""
    cfg.usessl = False
    cfg.mysql8 = False
    cfg.encrypted_login = False
    cfg.commcomp = False
    cfg.max_allowed_packet = ""
    cfg.single_transaction = False
    cfg.master_data = None
    cfg.table_exclude = []
    cfg.use_separate_dirs = True
    cfg.create_database = False
    cfg.dryrun = False
    cfg.compression = ""
    cfg.multicore = False
    for k, v in kwargs.items():
        setattr(cfg, k, v)

    # Mock auth so tests don't need a real credential context
    auth = MagicMock()
    auth.common_args.return_value = [f"--user={cfg.username}", f"--host={cfg.host}"]
    auth.ssl_args.return_value = []

    comp = CompressionHandler(cfg)
    db = DatabaseOps(cfg, auth, comp)
    return db, cfg


class TestDumpArgs(unittest.TestCase):
    def _args(self, **kwargs) -> list[str]:
        db, _ = _make_db(**kwargs)
        return db._dump_args()

    def test_always_includes_opt_and_quote_names(self):
        args = self._args()
        self.assertIn("--opt", args)
        self.assertIn("--quote-names", args)

    # ── --databases / --no-create-db logic ──────────────────────────────────

    def test_separate_dirs_no_create_database_uses_no_create_db(self):
        args = self._args(use_separate_dirs=True, create_database=False)
        self.assertIn("--no-create-db", args)
        self.assertNotIn("--databases", args)

    def test_separate_dirs_with_create_database_uses_databases(self):
        args = self._args(use_separate_dirs=True, create_database=True)
        self.assertIn("--databases", args)
        self.assertNotIn("--no-create-db", args)

    def test_combined_dump_always_uses_databases(self):
        args = self._args(use_separate_dirs=False, create_database=False)
        self.assertIn("--databases", args)
        self.assertNotIn("--no-create-db", args)

    # ── optional flags ────────────────────────────────────────────────────────

    def test_single_transaction_included_when_enabled(self):
        args = self._args(single_transaction=True)
        self.assertIn("--single-transaction", args)

    def test_single_transaction_excluded_when_disabled(self):
        args = self._args(single_transaction=False)
        self.assertNotIn("--single-transaction", args)

    def test_commcomp_adds_compress(self):
        args = self._args(commcomp=True)
        self.assertIn("--compress", args)

    def test_commcomp_off_excludes_compress(self):
        args = self._args(commcomp=False)
        self.assertNotIn("--compress", args)

    def test_master_data_1(self):
        args = self._args(master_data=1)
        self.assertIn("--master-data=1", args)

    def test_master_data_2(self):
        args = self._args(master_data=2)
        self.assertIn("--master-data=2", args)

    def test_master_data_none_excluded(self):
        args = self._args(master_data=None)
        self.assertFalse(any("--master-data" in a for a in args))

    def test_max_allowed_packet_included_when_set(self):
        args = self._args(max_allowed_packet="512M")
        self.assertIn("--max_allowed_packet=512M", args)

    def test_max_allowed_packet_excluded_when_empty(self):
        args = self._args(max_allowed_packet="")
        self.assertFalse(any("max_allowed_packet" in a for a in args))

    def test_table_exclude_generates_ignore_table_flags(self):
        args = self._args(table_exclude=["mydb.cache", "mydb.log"])
        self.assertIn("--ignore-table=mydb.cache", args)
        self.assertIn("--ignore-table=mydb.log", args)

    def test_empty_table_exclude_produces_no_ignore_flags(self):
        args = self._args(table_exclude=[])
        self.assertFalse(any("--ignore-table" in a for a in args))


class TestSchemaArgs(unittest.TestCase):
    def test_includes_all_databases_and_routines_no_data(self):
        db, _ = _make_db()
        args = db._schema_args()
        self.assertIn("--all-databases", args)
        self.assertIn("--routines", args)
        self.assertIn("--no-data", args)


class TestListDatabases(unittest.TestCase):
    def test_excludes_configured_db_exclude(self):
        db, cfg = _make_db()
        cfg.db_exclude = ["information_schema", "performance_schema"]
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "mydb\ninformation_schema\nperformance_schema\n"
        with patch("automysqlbackup.database.subprocess.run", return_value=mock_result):
            result = db.list_databases()
        self.assertEqual(result, ["mydb"])

    def test_raises_on_nonzero_returncode(self):
        db, _ = _make_db()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Access denied"
        with patch("automysqlbackup.database.subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError):
                db.list_databases()

    def test_command_includes_batch_and_skip_column_names(self):
        db, _ = _make_db()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        with patch("automysqlbackup.database.subprocess.run", return_value=mock_result) as mock_run:
            db.list_databases()
        cmd = mock_run.call_args[0][0]
        self.assertIn("--batch", cmd)
        self.assertIn("--skip-column-names", cmd)


class TestDumpDatabasesDryrun(unittest.TestCase):
    def test_dryrun_returns_0_without_running_mysqldump(self):
        db, _ = _make_db(dryrun=True)
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "backup.sql"
            with patch("automysqlbackup.database.subprocess.Popen") as mock_popen:
                rc = db.dump_databases(["mydb"], dest)
        mock_popen.assert_not_called()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
