"""Manifest — file-locked index for differential backup chains."""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Iterator, Optional

LOG = logging.getLogger(__name__)

# On-disk manifest line format (tab-separated, human-readable labels):
#   <filename>  md5sum  <hex>  diff_id  <8chars>  rel_id  <8chars|0>  db  <name>
# rel_id "0" marks a master backup; any other value is the diff_id of its master.
# IDs come from tempfile.mkstemp, whose alphabet includes "_" — rejecting it
# silently dropped about one entry in five.
_MANIFEST_RE = re.compile(
    r"^(?P<fname>[^\t]+)\tmd5sum\t(?P<md5>[^\t]+)\t"
    r"diff_id\t(?P<did>[A-Za-z0-9_]{8})\t"
    r"rel_id\t(?P<rid>0|[A-Za-z0-9_]{8})\t"
    r"db\t(?P<db>[^\t]*)$"
)


def _file_md5(path: Path) -> str:
    # 64 KB chunks keep memory flat for multi-GB SQL files.
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_diff_id(filename: Path) -> str:
    """Pull the 8-char ID from the filename stem (before any .sql/.diff suffix).

    The ID is the random suffix appended by tempfile.mkstemp, which always lands
    at the end of the stem before the extension begins.
    """
    stem = re.sub(r"\.(sql|diff).*$", "", filename.name)
    return stem[-8:] if len(stem) >= 8 else stem.ljust(8, "0")


@dataclasses.dataclass
class ManifestEntry:
    filename: Path
    md5sum: str
    diff_id: str   # 8-char alphanumeric ID (tail of temp filename)
    rel_id: str    # "0" for master backups; parent diff_id for differentials
    db: str

    @property
    def is_master(self) -> bool:
        return self.rel_id == "0"


class Manifest:
    """Tab-delimited backup chain index with file locking for concurrent safety."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock_path = path.with_suffix(path.suffix + ".lock")
        self.entries: list[ManifestEntry] = []

    # ── Public interface ──────────────────────────────────────────────────────

    def parse(self) -> None:
        """Read and validate the manifest file, populating self.entries.

        Re-computes md5sums on every call so stale entries (files modified
        outside the backup tool) are detected and updated automatically.
        """
        self.entries = []
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return

        seen_files: dict[str, ManifestEntry] = {}
        seen_md5: dict[str, Path] = {}

        for line in self.path.read_text().splitlines():
            m = _MANIFEST_RE.match(line)
            if not m:
                LOG.warning("Corrupted manifest line (ignored): %s", line)
                continue

            fpath = Path(m.group("fname"))
            if not fpath.exists():
                self._remove_line(str(fpath))
                continue

            actual_md5 = _file_md5(fpath)
            key = str(fpath)

            if key in seen_files:
                self._remove_line(key)
                continue

            # Skip the duplicate-md5 check for empty files: a zero-byte diff
            # (no changes since last master) is valid and would collide with
            # any other empty file.
            if actual_md5 in seen_md5 and fpath.stat().st_size > 0:
                LOG.warning("Duplicate md5sum: %s and %s", fpath, seen_md5[actual_md5])
                continue

            entry = ManifestEntry(
                filename=fpath,
                md5sum=actual_md5,
                diff_id=m.group("did"),
                rel_id=m.group("rid"),
                db=m.group("db"),
            )
            seen_files[key] = entry
            seen_md5[actual_md5] = fpath
            self.entries.append(entry)

    def add(self, filename: Path, rel_id: str, db: str) -> None:
        """Append a new entry and re-parse."""
        diff_id = _extract_diff_id(filename)
        md5 = _file_md5(filename)
        line = (
            f"{filename}\tmd5sum\t{md5}\tdiff_id\t{diff_id}\t"
            f"rel_id\t{rel_id or '0'}\tdb\t{db}\n"
        )
        with self._locked():
            with self.path.open("a") as f:
                f.write(line)
        self.parse()

    def remove(self, filename: Path) -> None:
        """Remove the entry for filename and re-parse."""
        with self._locked():
            self._remove_line(str(filename))
        self.parse()

    # ── Queries ───────────────────────────────────────────────────────────────

    def latest_master(self, db: str) -> Optional[ManifestEntry]:
        masters = [e for e in self.entries if e.db == db and e.is_master]
        return masters[-1] if masters else None

    def master_for(self, diff: ManifestEntry) -> Optional[ManifestEntry]:
        """Return the master a differential was created against, if still present.

        Must be used instead of latest_master() for recovery: after the next
        weekly master is taken, older diffs still refer to the previous one.
        """
        return next(
            (e for e in self.entries
             if e.is_master and e.db == diff.db and e.diff_id == diff.rel_id),
            None,
        )

    def diffs_for_db(self, db: str) -> list[ManifestEntry]:
        return [e for e in self.entries if e.db == db and not e.is_master]

    def all_dbs(self) -> list[str]:
        seen: list[str] = []
        for e in self.entries:
            if e.db not in seen:
                seen.append(e.db)
        return seen

    # ── Internals ─────────────────────────────────────────────────────────────

    def _remove_line(self, fname_str: str) -> None:
        if not self.path.exists():
            return
        # Match on fname_str + "\t" (not just fname_str) to avoid accidentally
        # removing an entry whose filename is a prefix of another entry's filename.
        lines = [l for l in self.path.read_text().splitlines()
                 if not l.startswith(fname_str + "\t")]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""))

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
