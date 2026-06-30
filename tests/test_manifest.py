"""Tests for Manifest — parsing, add/remove, locking, edge cases."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from automysqlbackup.manifest import (
    Manifest,
    ManifestEntry,
    _extract_diff_id,
    _file_md5,
)

MD5_EMPTY = "d41d8cd98f00b204e9800998ecf8427e"


class TestExtractDiffId(unittest.TestCase):
    def test_returns_last_8_chars_of_stem(self):
        p = Path("daily_mydb_2024-01-15_00h00m_Monday_abcd1234.sql.gz")
        self.assertEqual(_extract_diff_id(p), "abcd1234")

    def test_works_with_diff_extension(self):
        p = Path("daily_mydb_2024-01-15_00h00m_Monday_xyz98765.diff.gz")
        self.assertEqual(_extract_diff_id(p), "xyz98765")

    def test_works_without_compression_suffix(self):
        p = Path("daily_mydb_abcd1234.sql")
        self.assertEqual(_extract_diff_id(p), "abcd1234")

    def test_short_stem_is_padded_to_8_chars(self):
        p = Path("x.sql")
        result = _extract_diff_id(p)
        self.assertEqual(len(result), 8)


class TestFileMd5(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp)

    def _write(self, name: str, data: bytes) -> Path:
        p = Path(self._tmp) / name
        p.write_bytes(data)
        return p

    def test_empty_file_returns_known_digest(self):
        p = self._write("empty", b"")
        self.assertEqual(_file_md5(p), MD5_EMPTY)

    def test_returns_hex_string_of_correct_length(self):
        p = self._write("data", b"hello world")
        h = _file_md5(p)
        self.assertRegex(h, r"^[0-9a-f]{32}$")

    def test_deterministic(self):
        p = self._write("data", b"some content")
        self.assertEqual(_file_md5(p), _file_md5(p))

    def test_different_content_produces_different_digest(self):
        a = self._write("a", b"content a")
        b = self._write("b", b"content b")
        self.assertNotEqual(_file_md5(a), _file_md5(b))


class TestManifestEntry(unittest.TestCase):
    def test_is_master_when_rel_id_is_zero(self):
        e = ManifestEntry(Path("f.sql.gz"), "md5", "abcd1234", "0", "db")
        self.assertTrue(e.is_master)

    def test_is_not_master_when_rel_id_is_nonzero(self):
        e = ManifestEntry(Path("f.diff.gz"), "md5", "abcd1234", "12345678", "db")
        self.assertFalse(e.is_master)


class TestManifest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._dir = Path(self._tmp)
        self._manifest = Manifest(self._dir / "Manifest")

    def tearDown(self):
        shutil.rmtree(self._tmp)

    def _make_file(self, name: str, content: bytes = b"sql content") -> Path:
        p = self._dir / name
        p.write_bytes(content)
        return p

    # ── parse ────────────────────────────────────────────────────────────────

    def test_parse_on_missing_manifest_gives_empty_entries(self):
        self._manifest.parse()
        self.assertEqual(self._manifest.entries, [])

    def test_parse_on_empty_manifest_file_gives_empty_entries(self):
        (self._dir / "Manifest").write_text("")
        self._manifest.parse()
        self.assertEqual(self._manifest.entries, [])

    def test_parse_skips_missing_backup_files(self):
        backup = self._make_file("daily_mydb_abcd1234.sql.gz")
        self._manifest.add(backup, "0", "mydb")
        backup.unlink()
        self._manifest.parse()
        self.assertEqual(self._manifest.entries, [])

    def test_parse_warns_on_corrupted_line(self):
        mf = self._dir / "Manifest"
        mf.write_text("this is not valid manifest content\n")
        with self.assertLogs("automysqlbackup.manifest", level="WARNING"):
            self._manifest.parse()

    # ── add ──────────────────────────────────────────────────────────────────

    def test_add_master_creates_entry(self):
        backup = self._make_file("daily_mydb_abcd1234.sql.gz")
        self._manifest.add(backup, "0", "mydb")
        self.assertEqual(len(self._manifest.entries), 1)
        self.assertTrue(self._manifest.entries[0].is_master)
        self.assertEqual(self._manifest.entries[0].db, "mydb")

    def test_add_differential_creates_non_master_entry(self):
        master = self._make_file("master_abcd1234.sql.gz", b"master")
        diff = self._make_file("diff_abcd1234.diff.gz", b"diff")
        self._manifest.add(master, "0", "mydb")
        self._manifest.add(diff, "abcd1234", "mydb")
        diffs = self._manifest.diffs_for_db("mydb")
        self.assertEqual(len(diffs), 1)
        self.assertFalse(diffs[0].is_master)
        self.assertEqual(diffs[0].rel_id, "abcd1234")

    def test_add_updates_entries_immediately(self):
        backup = self._make_file("backup_abcd1234.sql.gz")
        self.assertEqual(len(self._manifest.entries), 0)
        self._manifest.add(backup, "0", "mydb")
        self.assertEqual(len(self._manifest.entries), 1)

    # ── remove ───────────────────────────────────────────────────────────────

    def test_remove_deletes_entry(self):
        backup = self._make_file("backup_abcd1234.sql.gz")
        self._manifest.add(backup, "0", "mydb")
        self._manifest.remove(backup)
        self.assertEqual(self._manifest.entries, [])

    def test_remove_prefix_guard_does_not_affect_longer_names(self):
        # "foo.sql.gz" and "foobar.sql.gz" share a prefix — removing one
        # must not touch the other.
        fa = self._make_file("foobar_aaaaaaaa.sql.gz", b"a")
        fb = self._make_file("foo_bbbbbbbb.sql.gz", b"b")
        self._manifest.add(fa, "0", "db1")
        self._manifest.add(fb, "0", "db2")
        self._manifest.remove(fb)
        self.assertEqual(len(self._manifest.entries), 1)
        self.assertEqual(self._manifest.entries[0].filename, fa)

    # ── queries ──────────────────────────────────────────────────────────────

    def test_latest_master_returns_last_added_master(self):
        f1 = self._make_file("bk1_aaaaaaaa.sql.gz", b"v1")
        f2 = self._make_file("bk2_bbbbbbbb.sql.gz", b"v2")
        self._manifest.add(f1, "0", "mydb")
        self._manifest.add(f2, "0", "mydb")
        latest = self._manifest.latest_master("mydb")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.filename, f2)

    def test_latest_master_returns_none_for_unknown_db(self):
        self.assertIsNone(self._manifest.latest_master("nosuchdb"))

    def test_diffs_for_db_returns_only_non_masters(self):
        master = self._make_file("master_aaaaaaaa.sql.gz", b"master")
        diff = self._make_file("diff_aaaaaaaa.diff.gz", b"diff")
        self._manifest.add(master, "0", "mydb")
        self._manifest.add(diff, "aaaaaaaa", "mydb")
        diffs = self._manifest.diffs_for_db("mydb")
        self.assertEqual(len(diffs), 1)
        self.assertFalse(diffs[0].is_master)

    def test_all_dbs_returns_unique_db_names(self):
        f1 = self._make_file("db1_aaaaaaaa.sql.gz", b"1")
        f2 = self._make_file("db2_bbbbbbbb.sql.gz", b"2")
        f3 = self._make_file("db1_cccccccc.sql.gz", b"3")
        self._manifest.add(f1, "0", "db1")
        self._manifest.add(f2, "0", "db2")
        self._manifest.add(f3, "0", "db1")
        self.assertEqual(set(self._manifest.all_dbs()), {"db1", "db2"})

    # ── duplicate md5 ────────────────────────────────────────────────────────

    def test_empty_diff_files_allowed_despite_same_md5(self):
        # Two zero-byte diffs are valid: no changes since last master.
        d1 = self._make_file("diff1_aaaaaaaa.diff.gz", b"")
        d2 = self._make_file("diff2_bbbbbbbb.diff.gz", b"")
        self._manifest.add(d1, "aaaaaaaa", "mydb")
        self._manifest.add(d2, "bbbbbbbb", "mydb")
        self.assertEqual(len(self._manifest.diffs_for_db("mydb")), 2)

    def test_non_empty_duplicate_md5_logs_warning(self):
        f1 = self._make_file("bk1_aaaaaaaa.sql.gz", b"identical content")
        f2 = self._make_file("bk2_bbbbbbbb.sql.gz", b"identical content")
        self._manifest.add(f1, "0", "mydb")
        # Force a second entry with the same md5 by writing directly to the manifest
        mf = self._dir / "Manifest"
        md5 = _file_md5(f2)
        with mf.open("a") as fh:
            fh.write(f"{f2}\tmd5sum\t{md5}\tdiff_id\tbbbbbbbb\trel_id\t0\tdb\tmydb\n")
        with self.assertLogs("automysqlbackup.manifest", level="WARNING"):
            self._manifest.parse()


if __name__ == "__main__":
    unittest.main()
