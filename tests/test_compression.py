"""Tests for CompressionHandler — suffix, command selection, multicore probing."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from automysqlbackup.config import Config
from automysqlbackup.compression import CompressionHandler


def _cfg(compression: str = "gzip", multicore: bool = True, threads: int = 2) -> Config:
    cfg = Config()
    cfg.compression = compression
    cfg.multicore = multicore
    cfg.multicore_threads = threads
    return cfg


class TestSuffix(unittest.TestCase):
    def test_gzip(self):
        self.assertEqual(CompressionHandler(_cfg("gzip")).suffix, ".gz")

    def test_bzip2(self):
        self.assertEqual(CompressionHandler(_cfg("bzip2")).suffix, ".bz2")

    def test_no_compression(self):
        self.assertEqual(CompressionHandler(_cfg("")).suffix, "")


class TestMulticoreProbe(unittest.TestCase):
    def test_disabled_when_multicore_false_in_config(self):
        with patch("shutil.which", return_value="/usr/bin/pigz"):
            comp = CompressionHandler(_cfg("gzip", multicore=False))
        self.assertFalse(comp._multicore_ok)

    @patch("shutil.which", return_value=None)
    def test_disabled_when_multicore_tool_not_on_path(self, _):
        comp = CompressionHandler(_cfg("gzip", multicore=True))
        self.assertFalse(comp._multicore_ok)

    @patch("shutil.which", return_value=None)
    def test_logs_warning_when_multicore_tool_missing(self, _):
        with self.assertLogs("automysqlbackup.compression", level="WARNING"):
            CompressionHandler(_cfg("gzip", multicore=True))

    def test_enabled_when_pigz_present(self):
        with patch("shutil.which", side_effect=lambda t: "/usr/bin/pigz" if t == "pigz" else None):
            comp = CompressionHandler(_cfg("gzip", multicore=True))
        self.assertTrue(comp._multicore_ok)

    def test_enabled_when_pbzip2_present(self):
        with patch("shutil.which", side_effect=lambda t: "/usr/bin/pbzip2" if t == "pbzip2" else None):
            comp = CompressionHandler(_cfg("bzip2", multicore=True))
        self.assertTrue(comp._multicore_ok)


class TestCompressCmd(unittest.TestCase):
    @patch("shutil.which", return_value=None)
    def test_gzip_single_core(self, _):
        comp = CompressionHandler(_cfg("gzip", multicore=False))
        self.assertEqual(comp.compress_cmd(), ["gzip"])

    @patch("shutil.which", return_value=None)
    def test_bzip2_single_core(self, _):
        comp = CompressionHandler(_cfg("bzip2", multicore=False))
        self.assertEqual(comp.compress_cmd(), ["bzip2"])

    @patch("shutil.which", return_value=None)
    def test_no_compression_returns_none(self, _):
        comp = CompressionHandler(_cfg(""))
        self.assertIsNone(comp.compress_cmd())

    def test_gzip_multicore_uses_pigz(self):
        with patch("shutil.which", side_effect=lambda t: "/usr/bin/pigz" if t == "pigz" else None):
            comp = CompressionHandler(_cfg("gzip", multicore=True, threads=4))
        cmd = comp.compress_cmd()
        self.assertIsNotNone(cmd)
        self.assertIn("pigz", cmd)
        self.assertIn("-p4", cmd)

    def test_gzip_multicore_omits_threads_arg_when_zero(self):
        with patch("shutil.which", side_effect=lambda t: "/usr/bin/pigz" if t == "pigz" else None):
            comp = CompressionHandler(_cfg("gzip", multicore=True, threads=0))
        cmd = comp.compress_cmd()
        self.assertIsNotNone(cmd)
        self.assertFalse(any(a.startswith("-p") for a in cmd))

    def test_bzip2_multicore_uses_pbzip2(self):
        with patch("shutil.which", side_effect=lambda t: "/usr/bin/pbzip2" if t == "pbzip2" else None):
            comp = CompressionHandler(_cfg("bzip2", multicore=True, threads=2))
        cmd = comp.compress_cmd()
        self.assertIsNotNone(cmd)
        self.assertIn("pbzip2", cmd)

    def test_compress_cmd_is_the_only_entry_point(self):
        # _compress_cmd alias was removed; the public method is used everywhere.
        with patch("shutil.which", return_value=None):
            comp = CompressionHandler(_cfg("gzip", multicore=False))
        self.assertFalse(hasattr(comp, "_compress_cmd"))
        self.assertIsNotNone(comp.compress_cmd)


if __name__ == "__main__":
    unittest.main()
