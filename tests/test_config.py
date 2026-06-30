"""Tests for Config — defaults, YAML loading, section mapping, edge cases."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

try:
    import yaml  # noqa: F401
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

from automysqlbackup.config import Config


class TestConfigDefaults(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_default_username(self):
        self.assertEqual(self.cfg.username, "root")

    def test_default_host(self):
        self.assertEqual(self.cfg.host, "localhost")

    def test_default_port(self):
        self.assertEqual(self.cfg.port, 3306)

    def test_default_compression(self):
        self.assertEqual(self.cfg.compression, "gzip")

    def test_default_rotation(self):
        self.assertEqual(self.cfg.rotation_daily, 6)
        self.assertEqual(self.cfg.rotation_weekly, 35)
        self.assertEqual(self.cfg.rotation_monthly, 150)

    def test_default_db_exclude_contains_information_schema(self):
        self.assertIn("information_schema", self.cfg.db_exclude)

    def test_default_backup_dir(self):
        self.assertEqual(self.cfg.backup_dir, Path("/var/backup/db"))

    def test_default_dryrun_false(self):
        self.assertFalse(self.cfg.dryrun)

    def test_default_encrypt_false(self):
        self.assertFalse(self.cfg.encrypt)

    def test_default_encrypt_password_empty(self):
        self.assertEqual(self.cfg.encrypt_password, "")

    def test_default_master_data_none(self):
        self.assertIsNone(self.cfg.master_data)

    def test_default_password_empty(self):
        self.assertEqual(self.cfg.password, "")

    def test_load_with_no_files_returns_defaults(self):
        cfg = Config.load(extra=Path("/nonexistent/path.yaml"))
        self.assertEqual(cfg.username, "root")
        self.assertEqual(cfg.host, "localhost")


class TestConfigProperties(unittest.TestCase):
    def test_host_label_returns_friendly_name_when_set(self):
        cfg = Config()
        cfg.host = "10.0.0.1"
        cfg.host_friendly = "prod-db"
        self.assertEqual(cfg.host_label, "prod-db")

    def test_host_label_falls_back_to_host(self):
        cfg = Config()
        cfg.host = "10.0.0.1"
        cfg.host_friendly = ""
        self.assertEqual(cfg.host_label, "10.0.0.1")

    def test_compression_suffix_gzip(self):
        cfg = Config()
        cfg.compression = "gzip"
        self.assertEqual(cfg.compression_suffix, ".gz")

    def test_compression_suffix_bzip2(self):
        cfg = Config()
        cfg.compression = "bzip2"
        self.assertEqual(cfg.compression_suffix, ".bz2")

    def test_compression_suffix_none(self):
        cfg = Config()
        cfg.compression = ""
        self.assertEqual(cfg.compression_suffix, "")


@unittest.skipUnless(HAS_YAML, "pyyaml not installed — run: pip install pyyaml")
class TestConfigYAML(unittest.TestCase):
    def _yaml_file(self, content: str) -> Path:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        f.write(content)
        f.close()
        return Path(f.name)

    def _load(self, content: str) -> Config:
        p = self._yaml_file(content)
        try:
            cfg = Config()
            cfg._apply_yaml(p)
            return cfg
        finally:
            p.unlink()

    # ── mysql section ────────────────────────────────────────────────────────

    def test_mysql_username(self):
        cfg = self._load("mysql:\n  username: backupuser\n")
        self.assertEqual(cfg.username, "backupuser")

    def test_mysql_password(self):
        cfg = self._load("mysql:\n  password: s3cr3t\n")
        self.assertEqual(cfg.password, "s3cr3t")

    def test_mysql_password_null_becomes_empty_string(self):
        cfg = self._load("mysql:\n  password: null\n")
        self.assertEqual(cfg.password, "")

    def test_mysql_host_and_port(self):
        cfg = self._load("mysql:\n  host: db.example.com\n  port: 3307\n")
        self.assertEqual(cfg.host, "db.example.com")
        self.assertEqual(cfg.port, 3307)

    def test_mysql_ssl_disabled(self):
        cfg = self._load("mysql:\n  ssl: false\n")
        self.assertFalse(cfg.usessl)

    def test_mysql8_flag(self):
        cfg = self._load("mysql:\n  mysql8: true\n")
        self.assertTrue(cfg.mysql8)

    def test_mysql_encrypted_login(self):
        cfg = self._load("mysql:\n  encrypted_login: true\n  login_path: mypath\n")
        self.assertTrue(cfg.encrypted_login)
        self.assertEqual(cfg.login_path, "mypath")

    # ── binaries section ─────────────────────────────────────────────────────

    def test_binaries_mysqldump(self):
        cfg = self._load("binaries:\n  mysqldump: /usr/bin/mariadb-dump\n")
        self.assertEqual(cfg.mysql_dump, "/usr/bin/mariadb-dump")

    def test_binaries_mysqlshow(self):
        cfg = self._load("binaries:\n  mysqlshow: /usr/bin/mariadb-show\n")
        self.assertEqual(cfg.mysql_show, "/usr/bin/mariadb-show")

    # ── backup section ───────────────────────────────────────────────────────

    def test_backup_dir(self):
        cfg = self._load("backup:\n  dir: /mnt/backups\n")
        self.assertEqual(cfg.backup_dir, Path("/mnt/backups"))

    def test_backup_local_files(self):
        cfg = self._load("backup:\n  local_files:\n    - /etc/mysql/my.cnf\n")
        self.assertEqual(cfg.backup_local_files, ["/etc/mysql/my.cnf"])

    def test_backup_local_files_empty(self):
        cfg = self._load("backup:\n  local_files: []\n")
        self.assertEqual(cfg.backup_local_files, [])

    # ── schedule section ─────────────────────────────────────────────────────

    def test_schedule_do_monthly(self):
        cfg = self._load("schedule:\n  do_monthly: 15\n")
        self.assertEqual(cfg.do_monthly, 15)

    def test_schedule_do_weekly(self):
        cfg = self._load("schedule:\n  do_weekly: 1\n")
        self.assertEqual(cfg.do_weekly, 1)

    # ── rotation section ─────────────────────────────────────────────────────

    def test_rotation_daily(self):
        cfg = self._load("rotation:\n  daily: 14\n")
        self.assertEqual(cfg.rotation_daily, 14)

    def test_rotation_weekly_and_monthly(self):
        cfg = self._load("rotation:\n  weekly: 60\n  monthly: 365\n")
        self.assertEqual(cfg.rotation_weekly, 60)
        self.assertEqual(cfg.rotation_monthly, 365)

    # ── databases section ────────────────────────────────────────────────────

    def test_databases_include(self):
        cfg = self._load("databases:\n  include:\n    - mydb\n    - otherdb\n")
        self.assertEqual(cfg.db_names, ["mydb", "otherdb"])

    def test_databases_exclude_overrides_default(self):
        cfg = self._load(
            "databases:\n  exclude:\n    - information_schema\n    - performance_schema\n"
        )
        self.assertIn("performance_schema", cfg.db_exclude)

    def test_databases_monthly(self):
        cfg = self._load("databases:\n  monthly:\n    - importantdb\n")
        self.assertEqual(cfg.db_month_names, ["importantdb"])

    # ── tables section ───────────────────────────────────────────────────────

    def test_tables_exclude(self):
        cfg = self._load("tables:\n  exclude:\n    - mydb.cache\n    - mydb.log\n")
        self.assertEqual(cfg.table_exclude, ["mydb.cache", "mydb.log"])

    # ── dump section ─────────────────────────────────────────────────────────

    def test_dump_single_transaction(self):
        cfg = self._load("dump:\n  single_transaction: true\n")
        self.assertTrue(cfg.single_transaction)

    def test_dump_master_data_1(self):
        cfg = self._load("dump:\n  master_data: 1\n")
        self.assertEqual(cfg.master_data, 1)

    def test_dump_master_data_2(self):
        cfg = self._load("dump:\n  master_data: 2\n")
        self.assertEqual(cfg.master_data, 2)

    def test_dump_master_data_null(self):
        cfg = Config()
        cfg.master_data = 1
        p = self._yaml_file("dump:\n  master_data: null\n")
        try:
            cfg._apply_yaml(p)
        finally:
            p.unlink()
        self.assertIsNone(cfg.master_data)

    def test_dump_master_data_invalid_value_becomes_none(self):
        cfg = self._load("dump:\n  master_data: 3\n")
        self.assertIsNone(cfg.master_data)

    def test_dump_differential(self):
        cfg = self._load("dump:\n  differential: true\n")
        self.assertTrue(cfg.differential)

    def test_dump_max_allowed_packet(self):
        cfg = self._load("dump:\n  max_allowed_packet: 512M\n")
        self.assertEqual(cfg.max_allowed_packet, "512M")

    # ── compression section ──────────────────────────────────────────────────

    def test_compression_algorithm_bzip2(self):
        cfg = self._load("compression:\n  algorithm: bzip2\n")
        self.assertEqual(cfg.compression, "bzip2")

    def test_compression_multicore_false(self):
        cfg = self._load("compression:\n  multicore: false\n")
        self.assertFalse(cfg.multicore)

    def test_compression_threads(self):
        cfg = self._load("compression:\n  threads: 4\n")
        self.assertEqual(cfg.multicore_threads, 4)

    # ── latest / encryption / notification / hooks ───────────────────────────

    def test_latest_enabled(self):
        cfg = self._load("latest:\n  enabled: true\n")
        self.assertTrue(cfg.latest)

    def test_latest_clean_filenames(self):
        cfg = self._load("latest:\n  clean_filenames: true\n")
        self.assertTrue(cfg.latest_clean_filenames)

    def test_encryption_enabled(self):
        cfg = self._load("encryption:\n  enabled: true\n  password: hunter2\n")
        self.assertTrue(cfg.encrypt)
        self.assertEqual(cfg.encrypt_password, "hunter2")

    def test_notification_content(self):
        cfg = self._load("notification:\n  content: quiet\n")
        self.assertEqual(cfg.mailcontent, "quiet")

    def test_notification_address(self):
        cfg = self._load("notification:\n  address: ops@example.com\n")
        self.assertEqual(cfg.mail_address, "ops@example.com")

    def test_hooks(self):
        cfg = self._load("hooks:\n  prebackup: /usr/local/bin/pre.sh\n")
        self.assertEqual(cfg.prebackup, "/usr/local/bin/pre.sh")

    def test_runtime_dryrun(self):
        cfg = self._load("runtime:\n  dryrun: true\n")
        self.assertTrue(cfg.dryrun)

    # ── Edge cases ───────────────────────────────────────────────────────────

    def test_omitted_keys_preserve_defaults(self):
        cfg = self._load("mysql:\n  username: admin\n")
        self.assertEqual(cfg.host, "localhost")
        self.assertEqual(cfg.port, 3306)

    def test_empty_yaml_file_logs_warning(self):
        p = self._yaml_file("")
        try:
            cfg = Config()
            with self.assertLogs("automysqlbackup.config", level="WARNING"):
                cfg._apply_yaml(p)
        finally:
            p.unlink()

    def test_yaml_list_at_top_level_logs_warning(self):
        p = self._yaml_file("- item1\n- item2\n")
        try:
            cfg = Config()
            with self.assertLogs("automysqlbackup.config", level="WARNING"):
                cfg._apply_yaml(p)
        finally:
            p.unlink()

    def test_layering_extra_overrides_global(self):
        p1 = self._yaml_file("mysql:\n  username: global_user\n  host: host1\n")
        p2 = self._yaml_file("mysql:\n  username: extra_user\n")
        try:
            cfg = Config()
            cfg._apply_yaml(p1)
            cfg._apply_yaml(p2)
            self.assertEqual(cfg.username, "extra_user")
            self.assertEqual(cfg.host, "host1")   # not overridden by p2
        finally:
            p1.unlink()
            p2.unlink()

    def test_umask_is_applied(self):
        p = self._yaml_file('umask: "0077"\n')
        try:
            old = os.umask(0o022)
            try:
                Config()._apply_yaml(p)
                applied = os.umask(old)
                self.assertEqual(applied, 0o077)
            finally:
                os.umask(old)
        finally:
            p.unlink()

    def test_missing_pyyaml_raises_runtime_error(self):
        import unittest.mock
        p = self._yaml_file("mysql:\n  username: x\n")
        try:
            cfg = Config()
            with unittest.mock.patch.dict("sys.modules", {"yaml": None}):
                with self.assertRaises(RuntimeError, msg="PyYAML is required"):
                    cfg._apply_yaml(p)
        finally:
            p.unlink()


if __name__ == "__main__":
    unittest.main()
