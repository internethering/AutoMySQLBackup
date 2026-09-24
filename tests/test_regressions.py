"""Regression tests for bugs found in the v4.0 review.

Most tests run the real code paths against fake mysql/mysqldump shell scripts,
so they exercise subprocess handling, compression and file placement end to end.
"""

from __future__ import annotations

import binascii
import contextlib
import datetime
import io
import logging
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from automysqlbackup.auth import AuthContext, _quote_option
from automysqlbackup.cli import PKG_LOG, AutoMySQLBackup, _LogCapture
from automysqlbackup.compression import CompressionHandler
from automysqlbackup.config import Config
from automysqlbackup.database import DatabaseOps
from automysqlbackup.directory import BackupDirectory
from automysqlbackup.encryption import OPENSSL_ARGS, EncryptionHandler
from automysqlbackup.interactive import InteractiveManager
from automysqlbackup.manifest import Manifest
from automysqlbackup.notifier import Notifier, uuencode
from automysqlbackup.orchestrator import BackupOrchestrator
from automysqlbackup.recovery import DiffRecovery

FAKE_MYSQL = """\
#!/bin/sh
echo good
echo bad
"""

# Last argument is the database name. "bad" fails like a real mysqldump would;
# other databases print $FAKE_DUMP_DIR/<db>.sql if present.
FAKE_MYSQLDUMP = """\
#!/bin/sh
for a; do last=$a; done
case "$*" in *--all-databases*) echo "-- schema"; exit 0;; esac
if [ "$last" = bad ]; then
  echo "mysqldump: Got error: 1049: Unknown database 'bad'" >&2
  exit 2
fi
if [ -f "$FAKE_DUMP_DIR/$last.sql" ]; then cat "$FAKE_DUMP_DIR/$last.sql"; else echo "-- dump of $last"; fi
"""


def _script(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


class _FakeServer(unittest.TestCase):
    """Temp dir with fake binaries and a backup directory."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.dumps = self.tmp / "dumps"
        self.dumps.mkdir()
        self.backup = self.tmp / "backup"
        self.mysql = _script(self.bin / "mysql", FAKE_MYSQL)
        self.mysqldump = _script(self.bin / "mysqldump", FAKE_MYSQLDUMP)
        env = patch.dict(os.environ, {"FAKE_DUMP_DIR": str(self.dumps)})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmpdir.cleanup()

    def cfg(self, **kwargs) -> Config:
        cfg = Config()
        cfg.backup_dir = self.backup
        cfg.mysql = self.mysql
        cfg.mysql_dump = self.mysqldump
        cfg.usessl = False
        cfg.multicore = False
        cfg.full_schema = False
        cfg.dbstatus = False
        cfg.do_weekly = 0
        cfg.do_monthly = 0
        for k, v in kwargs.items():
            setattr(cfg, k, v)
        return cfg

    def orchestrator(self, cfg: Config, now: datetime.datetime | None = None):
        dirs = BackupDirectory(cfg)
        dirs.setup()
        comp = CompressionHandler(cfg)
        auth = AuthContext(cfg)
        db = DatabaseOps(cfg, auth, comp)
        orch = BackupOrchestrator(cfg, db, comp, EncryptionHandler(cfg), dirs)
        if now is not None:
            orch._now = now
        return orch


# ── 1. Errors from other modules reach the report and the exit code ──────────

class TestErrorReporting(_FakeServer):
    def test_capture_sees_records_from_submodules(self):
        cap = _LogCapture()
        PKG_LOG.addHandler(cap)
        try:
            logging.getLogger("automysqlbackup.orchestrator").error("Dump failed for x")
        finally:
            PKG_LOG.removeHandler(cap)
        self.assertIn("Dump failed for x", cap.error_text)

    def test_failed_dump_gives_nonzero_exit_and_error_report(self):
        conf = self.tmp / "conf.yaml"
        conf.write_text(textwrap.dedent(f"""\
            binaries: {{mysql: {self.mysql}, mysqldump: {self.mysqldump}}}
            mysql: {{ssl: false}}
            backup: {{dir: {self.backup}}}
            schedule: {{do_weekly: 0, do_monthly: 0}}
            dump: {{full_schema: false, dbstatus: false}}
            compression: {{multicore: false}}
            """))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(Config, "_GLOBAL_CONFIG", self.tmp / "none.yaml"):
            rc = AutoMySQLBackup().main(["-c", str(conf)])
        self.assertEqual(rc, 1)
        self.assertIn("Errors reported", out.getvalue())
        self.assertIn("Unknown database 'bad'", out.getvalue())
        # The good database was still backed up; the bad one left nothing behind.
        self.assertEqual(len(list((self.backup / "daily" / "good").iterdir())), 1)
        self.assertEqual(list((self.backup / "daily" / "bad").iterdir()), [])

    def test_invalid_config_returns_2(self):
        conf = self.tmp / "conf.yaml"
        conf.write_text("compression: {algorithm: xz}\n")
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                patch.object(Config, "_GLOBAL_CONFIG", self.tmp / "none.yaml"):
            rc = AutoMySQLBackup().main(["-c", str(conf)])
        self.assertEqual(rc, 2)
        self.assertIn("compression.algorithm", err.getvalue())


# ── 2. Rotation of shared directories ────────────────────────────────────────

class TestRotation(unittest.TestCase):
    def _old(self, path: Path, days: int) -> Path:
        path.write_text("x")
        t = time.time() - days * 86400
        os.utime(path, (t, t))
        return path

    def test_pattern_limits_rotation_to_one_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            monthly = self._old(d / "fullschema_monthly_2024-01-01_00h00m_January.sql.gz", 10)
            daily = self._old(d / "fullschema_daily_2024-01-01_00h00m_Monday.sql.gz", 10)
            cfg = Config()
            cfg.backup_dir = d
            BackupDirectory(cfg).rotate(d, 6, pattern="fullschema_daily_*")
            self.assertTrue(monthly.exists())
            self.assertFalse(daily.exists())

    def test_keep_and_manifest_files_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            master = self._old(d / "master.sql.gz", 30)
            other = self._old(d / "other.sql.gz", 30)
            mf = self._old(d / "Manifest", 30)
            lock = self._old(d / "Manifest.lock", 30)
            cfg = Config()
            cfg.backup_dir = d
            BackupDirectory(cfg).rotate(d, 21, keep=[master])
            self.assertTrue(master.exists())
            self.assertTrue(mf.exists())
            self.assertTrue(lock.exists())
            self.assertFalse(other.exists())

    def test_rotate_missing_directory_is_noop(self):
        cfg = Config()
        BackupDirectory(cfg).rotate(Path("/nonexistent/dir"), 1)


# ── 3. Manifest IDs containing "_" ───────────────────────────────────────────

class TestManifestIds(unittest.TestCase):
    def test_underscore_ids_are_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            master = d / "daily_db_2024-01-01_00h00m_Monday_ab_cd_12.sql.gz"
            diff = d / "daily_db_2024-01-02_00h00m_Tuesday_x_y_z_12.diff.gz"
            master.write_bytes(b"m")
            diff.write_bytes(b"d")
            m = Manifest(d / "Manifest")
            m.add(master, "0", "db")
            m.add(diff, "ab_cd_12", "db")
            self.assertEqual(len(m.entries), 2)
            self.assertEqual(m.master_for(m.diffs_for_db("db")[0]).filename, master)

    def test_master_for_returns_the_referenced_not_the_latest_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            m1 = d / "m_aaaaaaaa.sql"
            m2 = d / "m_bbbbbbbb.sql"
            diff = d / "d_cccccccc.diff"
            for f, c in ((m1, b"1"), (m2, b"2"), (diff, b"3")):
                f.write_bytes(c)
            m = Manifest(d / "Manifest")
            m.add(m1, "0", "db")
            m.add(diff, "aaaaaaaa", "db")
            m.add(m2, "0", "db")
            self.assertEqual(m.latest_master("db").filename, m2)
            self.assertEqual(m.master_for(m.diffs_for_db("db")[0]).filename, m1)


# ── 4./5. Interactive manager ────────────────────────────────────────────────

class TestInteractive(unittest.TestCase):
    def test_empty_input_selects_nothing(self):
        with patch("builtins.input", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(InteractiveManager._select("x", ["a", "b"]), [])

    def test_star_and_ranges(self):
        with patch("builtins.input", return_value="*"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(InteractiveManager._select("x", ["a", "b"]), ["a", "b"])
        with patch("builtins.input", return_value="1-5, 0, 0"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(InteractiveManager._select("x", ["a", "b", "c"]), ["b", "c", "a"])

    def test_convert_uses_the_diffs_own_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "daily" / "db"
            d.mkdir(parents=True)
            old_master = d / "m_aaaaaaaa.sql"
            new_master = d / "m_bbbbbbbb.sql"
            diff = d / "d_cccccccc.diff"
            for f, c in ((old_master, b"1"), (diff, b"2"), (new_master, b"3")):
                f.write_bytes(c)
            m = Manifest(d / "Manifest")
            m.add(old_master, "0", "db")
            m.add(diff, "aaaaaaaa", "db")
            m.add(new_master, "0", "db")
            cfg = Config()
            cfg.backup_dir = Path(tmp)
            mgr = InteractiveManager(cfg, CompressionHandler(cfg))
            with patch.object(mgr._recovery, "apply", return_value=None) as apply, \
                    patch("builtins.input", side_effect=["0", "0", "0"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                mgr.run()
            apply.assert_called_once_with(old_master, diff)

    def test_remove_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "daily"
            d.mkdir()
            master = d / "m_aaaaaaaa.sql"
            diff = d / "d_bbbbbbbb.diff"
            master.write_bytes(b"m")
            diff.write_bytes(b"d")
            m = Manifest(d / "Manifest")
            m.add(master, "0", "all-databases")
            m.add(diff, "aaaaaaaa", "all-databases")
            cfg = Config()
            cfg.backup_dir = Path(tmp)
            mgr = InteractiveManager(cfg, CompressionHandler(cfg))
            # chain 0, diff 0, action 1 (remove), then decline
            with patch("builtins.input", side_effect=["0", "0", "1", "n"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                mgr.run()
            self.assertTrue(diff.exists())
            with patch("builtins.input", side_effect=["0", "0", "1", "y"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                mgr.run()
            self.assertFalse(diff.exists())


# ── 6./7. Failed and noisy dumps ─────────────────────────────────────────────

class TestDumpFiles(_FakeServer):
    def _db(self, cfg: Config) -> DatabaseOps:
        return DatabaseOps(cfg, AuthContext(cfg), CompressionHandler(cfg))

    def test_failed_dump_leaves_no_file(self):
        cfg = self.cfg()
        dest = self.tmp / "bad.sql.gz"
        with self.assertLogs("automysqlbackup.database", level="ERROR") as logs:
            rc = self._db(cfg).dump_databases(["bad"], dest)
        self.assertNotEqual(rc, 0)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name(dest.name + ".part").exists())
        self.assertIn("Unknown database", "\n".join(logs.output))

    def test_weekly_retry_after_failure_is_not_skipped(self):
        now = datetime.datetime.now()
        cfg = self.cfg(do_weekly=now.isoweekday())
        orch = self.orchestrator(cfg)
        with self.assertLogs("automysqlbackup", level="ERROR"):
            orch.run(["bad"], ["bad"])
        # The database comes back; a second run the same day must back it up.
        _script(Path(self.mysqldump), FAKE_MYSQLDUMP.replace("= bad ]", "= never ]"))
        orch = self.orchestrator(cfg)
        orch.run(["bad"], ["bad"])
        self.assertEqual(len(list((self.backup / "weekly" / "bad").glob("*.sql.gz"))), 1)

    def test_leftover_part_file_does_not_count_as_done(self):
        now = datetime.datetime.now()
        cfg = self.cfg(do_weekly=now.isoweekday())
        orch = self.orchestrator(cfg)
        d = self.backup / "weekly" / "good"
        d.mkdir(parents=True)
        (d / f"weekly_good_{orch.timestamp}_{orch.week_no}.sql.gz.part").write_bytes(b"")
        orch.run(["good"], ["good"])
        self.assertEqual(len(list(d.glob("*.sql.gz"))), 1)

    def test_large_stderr_does_not_deadlock(self):
        noisy = _script(self.bin / "noisy", "#!/bin/sh\nhead -c 300000 /dev/zero | tr '\\0' x >&2\necho ok\n")
        cfg = self.cfg(mysql_dump=noisy)
        dest = self.tmp / "out.sql.gz"
        with self.assertLogs("automysqlbackup.database", level="WARNING"):
            rc = self._db(cfg).dump_databases(["x"], dest)
        self.assertEqual(rc, 0)
        self.assertEqual(subprocess.run(["gzip", "-dc", str(dest)], capture_output=True).stdout,
                         b"ok\n")

    def test_uncompressed_dump_streams_to_file(self):
        cfg = self.cfg(compression="")
        dest = self.tmp / "good.sql"
        self.assertEqual(self._db(cfg).dump_databases(["good"], dest), 0)
        self.assertEqual(dest.read_text(), "-- dump of good\n")

    def test_source_ok_accepts_diff_exit_1(self):
        cfg = self.cfg()
        comp = CompressionHandler(cfg)
        proc = subprocess.Popen(["sh", "-c", "echo x; exit 1"], stdout=subprocess.PIPE)
        self.assertEqual(comp.pipe_compress(proc, self.tmp / "a.gz", source_ok=(0, 1)), 0)
        proc = subprocess.Popen(["sh", "-c", "echo x; exit 1"], stdout=subprocess.PIPE)
        self.assertEqual(comp.pipe_compress(proc, self.tmp / "b.gz"), 1)


# ── Idempotency with encryption, dry run, differential chain ────────────────

class TestOrchestratorEndToEnd(_FakeServer):
    def test_encrypted_weekly_is_not_repeated(self):
        now = datetime.datetime.now()
        cfg = self.cfg(do_weekly=now.isoweekday(), encrypt=True, encrypt_password="pw")
        self.orchestrator(cfg).run(["good"], ["good"])
        self.orchestrator(cfg).run(["good"], ["good"])
        self.assertEqual(len(list((self.backup / "weekly" / "good").glob("*.enc"))), 1)

    def test_dryrun_on_fresh_directory_writes_nothing(self):
        cfg = self.cfg(dryrun=True, differential=True, latest=True,
                       latest_clean_filenames=True, full_schema=True)
        dirs = BackupDirectory(cfg)
        dirs.setup()
        comp = CompressionHandler(cfg)
        db = DatabaseOps(cfg, AuthContext(cfg), comp)
        BackupOrchestrator(cfg, db, comp, EncryptionHandler(cfg), dirs).run(["good"], ["good"])
        self.assertFalse(self.backup.exists())

    def test_aux_dumps_keep_monthly_after_daily_rotation(self):
        cfg = self.cfg(full_schema=True, do_monthly=1)
        first = datetime.datetime(2024, 3, 1, 1, 0)
        self.orchestrator(cfg, first).run(["good"], ["good"])
        for f in (self.backup / "fullschema").iterdir():
            t = time.time() - 10 * 86400
            os.utime(f, (t, t))
        self.orchestrator(cfg, datetime.datetime(2024, 3, 11, 1, 0)).run(["good"], ["good"])
        names = [f.name for f in (self.backup / "fullschema").iterdir()]
        self.assertTrue(any(n.startswith("fullschema_monthly_") for n in names), names)

    def test_differential_chain_restores_current_state(self):
        cfg = self.cfg(differential=True, do_weekly=1)
        (self.dumps / "good.sql").write_text("line1\nline2\nline3\n")
        self.orchestrator(cfg, datetime.datetime(2024, 3, 18, 1, 0)).run(["good"], [])  # Monday
        (self.dumps / "good.sql").write_text("line1\nline2 changed\nline3\nline4\n")
        self.orchestrator(cfg, datetime.datetime(2024, 3, 19, 1, 0)).run(["good"], [])

        d = self.backup / "daily" / "good"
        m = Manifest(d / "Manifest")
        m.parse()
        diff = m.diffs_for_db("good")[0]
        master = m.master_for(diff)
        self.assertIsNotNone(master)
        self.assertEqual(list((self.backup / "tmp").iterdir()), [])  # staging cleaned up
        out = DiffRecovery(CompressionHandler(cfg)).apply(master.filename, diff.filename)
        self.assertIsNotNone(out)
        self.assertEqual(out.read_text(), "line1\nline2 changed\nline3\nline4\n")


# ── Encryption, credentials, config ─────────────────────────────────────────

class TestEncryption(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_roundtrip_with_documented_decrypt_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "b.sql.gz"
            src.write_bytes(b"secret data")
            cfg = Config()
            cfg.encrypt = True
            cfg.encrypt_password = "p@ss word"
            out = EncryptionHandler(cfg).encrypt(src)
            dec = subprocess.run(
                ["openssl", "enc", "-d", *OPENSSL_ARGS, "-in", str(out),
                 "-pass", "pass:p@ss word"], capture_output=True, check=True)
            self.assertEqual(dec.stdout, b"secret data")
            self.assertFalse(src.exists())

    def test_password_not_on_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "b.sql"
            src.write_bytes(b"x")
            cfg = Config()
            cfg.encrypt = True
            cfg.encrypt_password = "topsecret"
            result = MagicMock(returncode=0)
            with patch("automysqlbackup.encryption.subprocess.run", return_value=result) as run:
                EncryptionHandler(cfg).encrypt(src)
            argv = run.call_args[0][0]
            self.assertFalse(any("topsecret" in a for a in argv))
            self.assertIn(b"topsecret", run.call_args[1]["input"])

    def test_empty_password_is_rejected(self):
        cfg = Config()
        cfg.encrypt = True
        with self.assertRaises(RuntimeError):
            EncryptionHandler(cfg).encrypt(Path("/nonexistent"))


class TestCredentialQuoting(unittest.TestCase):
    def test_special_characters_are_quoted_and_escaped(self):
        self.assertEqual(_quote_option('a#b'), '"a#b"')
        self.assertEqual(_quote_option(' x '), '" x "')
        self.assertEqual(_quote_option('q"\\'), '"q\\"\\\\"')


class TestConfigValidation(unittest.TestCase):
    def _load(self, text: str) -> Config:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.yaml"
            p.write_text(text)
            with patch.object(Config, "_GLOBAL_CONFIG", Path(tmp) / "none.yaml"):
                return Config.load(p)

    def test_defaults_are_valid(self):
        Config().validate()

    def test_unknown_values_are_rejected(self):
        for text in ("compression: {algorithm: xz}\n",
                     "notification: {content: mail}\n",
                     "schedule: {do_weekly: 8}\n",
                     "rotation: {daily: -1}\n",
                     "encryption: {enabled: true}\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self._load(text)

    def test_unquoted_numeric_password_warns(self):
        with self.assertLogs("automysqlbackup.config", level="WARNING") as logs:
            cfg = self._load("mysql: {password: 012345}\n")
        self.assertEqual(cfg.password, "5349")
        self.assertIn("quotes", "\n".join(logs.output))

    def test_performance_schema_excluded_by_default(self):
        self.assertIn("performance_schema", Config().db_exclude)


class TestTableWildcards(unittest.TestCase):
    def test_like_wildcards_in_names_are_escaped(self):
        cfg = Config()
        cfg.table_exclude = ["my'db.cache_*"]
        auth = MagicMock()
        auth.common_args.return_value = []
        auth.ssl_args.return_value = []
        db = DatabaseOps(cfg, auth, MagicMock())
        result = MagicMock(returncode=0, stdout="cache_a\n")
        with patch("automysqlbackup.database.subprocess.run", return_value=result) as run:
            db.expand_table_wildcards()
        sql = run.call_args[0][0][-1]
        self.assertIn("LIKE 'cache\\_%'", sql)
        self.assertIn("table_schema='my''db'", sql)
        self.assertEqual(cfg.table_exclude, ["my'db.cache_a"])


# ── Notification ─────────────────────────────────────────────────────────────

class TestNotifier(unittest.TestCase):
    def _cfg(self, **kwargs) -> Config:
        cfg = Config()
        for k, v in kwargs.items():
            setattr(cfg, k, v)
        return cfg

    def test_missing_mail_binary_falls_back_to_stdout(self):
        cfg = self._cfg(mailcontent="log")
        with patch("automysqlbackup.notifier.subprocess.Popen", side_effect=FileNotFoundError), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertLogs("automysqlbackup.notifier", level="ERROR"):
            Notifier(cfg).send("the log", "", [])
        self.assertIn("the log", out.getvalue())

    def test_stdout_mode_prints_only_error_summary(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            Notifier(self._cfg()).send("info line", "", [])
        self.assertEqual(out.getvalue(), "")

    def test_uuencode_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.bin"
            data = bytes(range(256)) * 3
            f.write_bytes(data)
            lines = uuencode(f).splitlines()
            self.assertEqual(lines[0], "begin 644 a.bin")
            self.assertEqual(lines[-1], "end")
            decoded = b"".join(binascii.a2b_uu(line) for line in lines[1:-1])
            self.assertEqual(decoded, data)

    def test_uuencoded_mode_attaches_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "b.sql.gz"
            f.write_bytes(b"data")
            cfg = self._cfg(mailcontent="files", mail_uuencoded=True)
            n = Notifier(cfg)
            with patch.object(n, "_mail") as mail:
                n.send("log", "", [f])
            self.assertIn("begin 644 b.sql.gz", mail.call_args[0][1])

    def test_split_and_tar_respects_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "big.sql.gz"
            f.write_bytes(os.urandom(5000))
            cfg = self._cfg(mail_maxattsize=2, mail_splitandtar=True)
            batches, note = Notifier(cfg)._attachment_batches([f], Path(tmp))
            self.assertGreater(len(batches), 1)
            self.assertTrue(all(b[0].stat().st_size <= 2048 for b in batches))
            self.assertIn("cat backup.tar.part*", note)

    def test_oversized_files_skipped_without_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big"
            small = Path(tmp) / "small"
            big.write_bytes(b"x" * 5000)
            small.write_bytes(b"x" * 100)
            cfg = self._cfg(mail_maxattsize=2, mail_splitandtar=False)
            batches, note = Notifier(cfg)._attachment_batches([big, small], Path(tmp))
            self.assertEqual(batches, [[small]])
            self.assertIn(str(big), note)


if __name__ == "__main__":
    unittest.main()
