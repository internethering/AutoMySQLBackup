"""Tests for DiffRecovery — decompression handling, patch output path, cleanup."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from automysqlbackup.compression import CompressionHandler
from automysqlbackup.config import Config
from automysqlbackup.recovery import DiffRecovery


def _comp() -> CompressionHandler:
    cfg = Config()
    cfg.compression = ""
    cfg.multicore = False
    return CompressionHandler(cfg)


class TestDecompressToTmp(unittest.TestCase):
    def test_uncompressed_file_returned_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "backup.sql"
            p.write_bytes(b"sql content")
            rec = DiffRecovery(_comp())
            result = rec._decompress_to_tmp(p)
            self.assertEqual(result, p)

    def test_returns_none_on_rc_2_or_higher(self):
        with tempfile.TemporaryDirectory() as tmp:
            gz = Path(tmp) / "backup.sql.gz"
            gz.write_bytes(b"placeholder")
            rec = DiffRecovery(_comp())
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (b"", b"error")
            mock_proc.returncode = 2
            with patch.object(rec._comp, "decompress_stdout", return_value=mock_proc):
                result = rec._decompress_to_tmp(gz)
            self.assertIsNone(result)

    def test_accepts_rc_1_as_gzip_warning(self):
        # gzip exits with 1 for warnings but still produces valid output.
        with tempfile.TemporaryDirectory() as tmp:
            gz = Path(tmp) / "backup.sql.gz"
            gz.write_bytes(b"placeholder")
            rec = DiffRecovery(_comp())
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (b"valid sql", b"trailing garbage")
            mock_proc.returncode = 1
            with patch.object(rec._comp, "decompress_stdout", return_value=mock_proc):
                result = rec._decompress_to_tmp(gz)
            self.assertIsNotNone(result)
            self.assertEqual(result.read_bytes(), b"valid sql")

    def test_rc_zero_is_always_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            gz = Path(tmp) / "backup.sql.gz"
            gz.write_bytes(b"placeholder")
            rec = DiffRecovery(_comp())
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (b"sql content", b"")
            mock_proc.returncode = 0
            with patch.object(rec._comp, "decompress_stdout", return_value=mock_proc):
                result = rec._decompress_to_tmp(gz)
            self.assertIsNotNone(result)

    def test_bz2_suffix_is_decompressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bz2 = Path(tmp) / "backup.sql.bz2"
            bz2.write_bytes(b"placeholder")
            rec = DiffRecovery(_comp())
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (b"sql", b"")
            mock_proc.returncode = 0
            with patch.object(rec._comp, "decompress_stdout", return_value=mock_proc):
                result = rec._decompress_to_tmp(bz2)
            self.assertIsNotNone(result)
            self.assertNotIn(".bz2", result.suffixes)


class TestApply(unittest.TestCase):
    def _run_apply(self, master_content: bytes, diff_content: bytes, patch_rc: int = 0):
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "master.sql"
            diff = Path(tmp) / "patch.diff"
            master.write_bytes(master_content)
            diff.write_bytes(diff_content)

            rec = DiffRecovery(_comp())
            with patch.object(rec, "_decompress_to_tmp", side_effect=lambda p: p):
                mock_result = MagicMock()
                mock_result.returncode = patch_rc
                mock_result.stderr = b"patch error" if patch_rc != 0 else b""
                with patch("automysqlbackup.recovery.subprocess.run", return_value=mock_result):
                    return rec.apply(master, diff)

    def test_returns_path_on_success(self):
        out = self._run_apply(b"sql", b"", patch_rc=0)
        self.assertIsNotNone(out)

    def test_returns_none_on_patch_failure(self):
        out = self._run_apply(b"sql", b"", patch_rc=1)
        self.assertIsNone(out)

    def test_output_suffix_is_sql(self):
        out = self._run_apply(b"sql", b"", patch_rc=0)
        self.assertIsNotNone(out)
        self.assertTrue(str(out).endswith(".sql"))

    def test_output_path_derived_from_diff_not_master(self):
        # The output path replaces .diff with .sql, so its stem should
        # match the diff filename stem, not the master filename stem.
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "master_aaaaaaaa.sql"
            diff = Path(tmp) / "patch_bbbbbbbb.diff"
            master.write_bytes(b"sql")
            diff.write_bytes(b"")
            rec = DiffRecovery(_comp())
            with patch.object(rec, "_decompress_to_tmp", side_effect=lambda p: p):
                mock_result = MagicMock()
                mock_result.returncode = 0
                mock_result.stderr = b""
                with patch("automysqlbackup.recovery.subprocess.run", return_value=mock_result):
                    out = rec.apply(master, diff)
            self.assertIsNotNone(out)
            self.assertIn("bbbbbbbb", out.name)

    def test_cleanup_removes_decompressed_copies_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "master.sql.gz"
            diff = Path(tmp) / "patch.diff.gz"
            master.write_bytes(b"x")
            diff.write_bytes(b"x")
            master_dec = Path(tmp) / "master.sql"
            diff_dec = Path(tmp) / "patch.diff"
            master_dec.write_bytes(b"sql")
            diff_dec.write_bytes(b"")

            rec = DiffRecovery(_comp())

            def fake_decompress(p: Path) -> Path:
                return master_dec if p == master else diff_dec

            with patch.object(rec, "_decompress_to_tmp", side_effect=fake_decompress):
                mock_result = MagicMock()
                mock_result.returncode = 0
                mock_result.stderr = b""
                with patch("automysqlbackup.recovery.subprocess.run", return_value=mock_result):
                    rec.apply(master, diff)

            self.assertFalse(master_dec.exists())
            self.assertFalse(diff_dec.exists())

    def test_cleanup_removes_decompressed_copies_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "master.sql.gz"
            diff = Path(tmp) / "patch.diff.gz"
            master.write_bytes(b"x")
            diff.write_bytes(b"x")
            master_dec = Path(tmp) / "master.sql"
            diff_dec = Path(tmp) / "patch.diff"
            master_dec.write_bytes(b"sql")
            diff_dec.write_bytes(b"")

            rec = DiffRecovery(_comp())

            def fake_decompress(p: Path) -> Path:
                return master_dec if p == master else diff_dec

            with patch.object(rec, "_decompress_to_tmp", side_effect=fake_decompress):
                mock_result = MagicMock()
                mock_result.returncode = 1
                mock_result.stderr = b"failed"
                with patch("automysqlbackup.recovery.subprocess.run", return_value=mock_result):
                    rec.apply(master, diff)

            self.assertFalse(master_dec.exists())
            self.assertFalse(diff_dec.exists())

    def test_originals_not_deleted_when_not_compressed(self):
        # When master/diff are already uncompressed, _decompress_to_tmp returns
        # them unchanged; the finally block must not unlink the originals.
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "master.sql"
            diff = Path(tmp) / "patch.diff"
            master.write_bytes(b"sql")
            diff.write_bytes(b"")

            rec = DiffRecovery(_comp())
            with patch.object(rec, "_decompress_to_tmp", side_effect=lambda p: p):
                mock_result = MagicMock()
                mock_result.returncode = 1
                mock_result.stderr = b"failed"
                with patch("automysqlbackup.recovery.subprocess.run", return_value=mock_result):
                    rec.apply(master, diff)

            # Originals must still exist
            self.assertTrue(master.exists())
            self.assertTrue(diff.exists())


if __name__ == "__main__":
    unittest.main()
