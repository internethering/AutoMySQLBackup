"""BackupDirectory — creates and manages the backup directory tree."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Iterable

from .config import Config

LOG = logging.getLogger(__name__)

# Bookkeeping files of differential backups; they are rewritten as needed and
# must survive rotation even when their mtime is old.
_NEVER_ROTATE = frozenset({"Manifest", "Manifest.lock"})


class BackupDirectory:
    _BASE_SUBDIRS = ["daily", "weekly", "monthly", "latest", "tmp"]

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def setup(self) -> None:
        """Create backup_dir and all required subdirectories.

        Raises RuntimeError if the parent directory doesn't exist or the
        backup_dir is not writable after creation.
        """
        base = self._cfg.backup_dir
        if not base.parent.is_dir():
            raise RuntimeError(f"Parent of backup_dir does not exist: {base.parent}")

        if self._cfg.dryrun:
            LOG.info("[dryrun] would create %s and subdirs", base)
            return

        base.mkdir(parents=True, exist_ok=True)

        subdirs = list(self._BASE_SUBDIRS)
        if self._cfg.backup_local_files:
            subdirs.append("backup_local_files")
        if self._cfg.full_schema:
            subdirs.append("fullschema")
        if self._cfg.dbstatus:
            subdirs.append("status")

        for name in subdirs:
            (base / name).mkdir(exist_ok=True)

        if not os.access(base, os.W_OK | os.X_OK):
            raise RuntimeError(f"backup_dir is not writable: {base}")

    def cleanup_latest(self) -> None:
        """Remove all files from the latest/ directory."""
        if not self._cfg.latest:
            return
        latest = self._cfg.backup_dir / "latest"
        if self._cfg.dryrun:
            LOG.info("[dryrun] would clean %s", latest)
            return
        if not latest.is_dir():
            return
        for f in latest.iterdir():
            f.unlink(missing_ok=True)

    def rotate(
        self,
        directory: Path,
        max_age_days: int,
        pattern: str = "*",
        keep: Iterable[Path] = (),
    ) -> None:
        """Delete files matching pattern in directory older than max_age_days.

        pattern restricts rotation to one backup kind when several share a
        directory (fullschema/ and status/ hold daily, weekly and monthly
        files side by side). Paths in keep are never deleted — used to retain
        differential masters that newer diffs still depend on.
        """
        if self._cfg.dryrun:
            LOG.info("[dryrun] would rotate %s older than %d days in %s",
                     pattern, max_age_days, directory)
            return
        if not directory.is_dir():
            return
        cutoff = time.time() - max_age_days * 86400
        protected = {Path(p) for p in keep}
        # rglob descends into per-database subdirectories intentionally —
        # use_separate_dirs creates one subdir per db inside each period directory.
        # st_mtime is the content write time; st_ctime would reflect inode changes
        # (chown, chmod) and could misidentify recently linked files as old.
        for f in directory.rglob(pattern):
            if f.name in _NEVER_ROTATE or f in protected or not f.is_file():
                continue
            if f.stat().st_mtime < cutoff:
                f.unlink()
                LOG.debug("Rotated %s", f)

    def hardlink_to_latest(self, src: Path) -> None:
        """Create a hardlink of src inside the latest/ directory."""
        if not self._cfg.latest or not src.exists():
            return
        dest = self._cfg.backup_dir / "latest" / src.name
        if self._cfg.dryrun:
            LOG.info("[dryrun] hardlink %s -> %s", src, dest)
            return
        dest.unlink(missing_ok=True)
        # os.link (hardlink) shares the inode, so latest/ costs no extra disk space.
        os.link(src, dest)
