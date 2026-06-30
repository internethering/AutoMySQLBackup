"""BackupOrchestrator — schedules and executes daily/weekly/monthly backups."""

from __future__ import annotations

import datetime
import difflib
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .compression import CompressionHandler
from .config import Config
from .database import DatabaseOps
from .directory import BackupDirectory
from .encryption import EncryptionHandler
from .manifest import Manifest

LOG = logging.getLogger(__name__)


class BackupOrchestrator:
    def __init__(
        self,
        cfg: Config,
        db: DatabaseOps,
        comp: CompressionHandler,
        enc: EncryptionHandler,
        dirs: BackupDirectory,
    ) -> None:
        self._cfg = cfg
        self._db = db
        self._comp = comp
        self._enc = enc
        self._dirs = dirs
        self._files: list[Path] = []
        # Capture "now" once at construction so all schedule decisions within a
        # single run share the same clock snapshot — avoids midnight edge cases
        # where the date could change between the weekly/monthly checks and the
        # daily check inside run().
        self._now = datetime.datetime.now()

    # ── Date helpers ──────────────────────────────────────────────────────────

    @property
    def timestamp(self) -> str:
        return self._now.strftime("%Y-%m-%d_%Hh%Mm")

    @property
    def date_stamp(self) -> str:
        return self._now.strftime("%Y-%m-%d")

    @property
    def week_no(self) -> str:
        return str(int(self._now.strftime("%V")))

    @property
    def dow(self) -> int:
        return self._now.isoweekday()  # 1=Monday … 7=Sunday

    @property
    def dow_name(self) -> str:
        return self._now.strftime("%A")

    @property
    def dom(self) -> int:
        return self._now.day

    @property
    def month_name(self) -> str:
        return self._now.strftime("%B")

    def _last_day_of_month(self) -> int:
        n = self._now
        if n.month == 12:
            return (datetime.date(n.year + 1, 1, 1) - datetime.date(n.year, n.month, 1)).days
        return (datetime.date(n.year, n.month + 1, 1) - datetime.date(n.year, n.month, 1)).days

    def _do_monthly(self) -> bool:
        if self._cfg.do_monthly == 0:
            return False
        t = self._cfg.do_monthly
        last = self._last_day_of_month()
        # Fall back to last day of month when the configured day exceeds it
        return self.dom == t or (self.dom == last and last < t)

    def _do_weekly(self) -> bool:
        return self._cfg.do_weekly != 0 and self.dow == self._cfg.do_weekly

    def _already_done_today(self, glob_pattern: str) -> bool:
        # Idempotency guard: if a matching file already exists for today's date,
        # a previous run completed successfully and we should skip this period.
        return bool(list(self._cfg.backup_dir.glob(glob_pattern)))

    # ── Post-processing ───────────────────────────────────────────────────────

    def _postprocess(self, path: Path) -> Path:
        """Encrypt (if configured) then hardlink to latest/. Return final path."""
        final = path
        if self._cfg.encrypt:
            encrypted = self._enc.encrypt(path)
            if encrypted:
                final = encrypted
        self._dirs.hardlink_to_latest(final)
        return final

    # ── Main entry point ──────────────────────────────────────────────────────

    def run(self, daily_dbs: list[str], monthly_dbs: list[str]) -> list[Path]:
        """Execute all configured backup tasks and return the list of output files."""
        cfg = self._cfg
        # Master backups are created once a week; a rotation shorter than 7 days
        # would delete the master before the next one is created, orphaning its
        # differentials.  21 days = 3 complete weekly cycles of safety margin.
        if cfg.differential:
            cfg.rotation_daily = max(cfg.rotation_daily, 21)

        if cfg.full_schema:
            self._run_schema()
        if cfg.dbstatus:
            self._run_status()
        if cfg.backup_local_files:
            self._run_local_files()

        LOG.info("Backup start: %s", datetime.datetime.now())

        if self._do_monthly():
            self._run_period(monthly_dbs, "monthly", f"_{self.month_name}",
                             cfg.rotation_monthly, "month")
        if self._do_weekly():
            self._run_period(daily_dbs, "weekly", f"_{self.week_no}",
                             cfg.rotation_weekly, "week")
        # allow_differential only for daily: weekly/monthly granularity produces
        # too-large diffs to be worth the complexity.
        self._run_period(daily_dbs, "daily", f"_{self.dow_name}",
                         cfg.rotation_daily, "day", allow_differential=True)

        LOG.info("Backup end: %s", datetime.datetime.now())

        if cfg.latest_clean_filenames:
            self._clean_latest_filenames()

        return self._files

    # ── Schema / status / local files ─────────────────────────────────────────

    def _run_aux_dump(
        self,
        label: str,
        subdir: str,
        filename_prefix: str,
        ext: str,
        dump_fn: object,
    ) -> None:
        """Run a non-database dump (schema or status) for each active period."""
        LOG.info("=== %s ===", label)
        base = self._cfg.backup_dir / subdir
        for do, kind, period_label, rotation in [
            (self._do_monthly(), "monthly",
             f"monthly_{self.timestamp}_{self.month_name}", self._cfg.rotation_monthly),
            (self._do_weekly(), "weekly",
             f"weekly_{self.timestamp}_{self.week_no}", self._cfg.rotation_weekly),
            (True, "daily",
             f"daily_{self.timestamp}_{self.dow_name}", self._cfg.rotation_daily),
        ]:
            if not do:
                continue
            if kind != "daily" and self._already_done_today(
                    f"{subdir}/{filename_prefix}_{kind}_{self.date_stamp}_*"):
                continue
            dest = base / f"{filename_prefix}_{period_label}{ext}{self._comp.suffix}"
            if dump_fn(dest) == 0:  # type: ignore[operator]
                self._dirs.rotate(base, rotation)
                self._files.append(self._postprocess(dest))
            else:
                LOG.error("%s failed (%s)", label, kind)

    def _run_schema(self) -> None:
        self._run_aux_dump(
            "Full schema dump", "fullschema", "fullschema", ".sql", self._db.dump_schema
        )

    def _run_status(self) -> None:
        self._run_aux_dump(
            "DB status dump", "status", "status", ".txt", self._db.dump_status
        )

    def _run_local_files(self) -> None:
        if not self._do_weekly():
            return
        if self._already_done_today(f"backup_local_files/bcf_weekly_{self.date_stamp}_*"):
            return
        LOG.info("=== Local files backup ===")
        base = self._cfg.backup_dir / "backup_local_files"
        suf = self._comp.suffix
        archive = base / f"bcf_weekly_{self.timestamp}_{self.week_no}.tar{suf}"
        compress_flag = {"gzip": "z", "bzip2": "j"}.get(self._cfg.compression, "")
        if self._cfg.dryrun:
            LOG.info("[dryrun] tar -c%svf %s %s",
                     compress_flag, archive, self._cfg.backup_local_files)
            return
        rc = subprocess.run(
            # "--" stops tar from interpreting paths starting with "-" as flags.
            ["tar", f"-c{compress_flag}vf", str(archive), "--"] + self._cfg.backup_local_files
        ).returncode
        if rc == 0:
            self._files.append(self._postprocess(archive))
        else:
            LOG.error("Local files backup failed")

    # ── Database backup dispatching ────────────────────────────────────────────

    def _run_period(
        self,
        dbs: list[str],
        period: str,
        midfix: str,
        rotation: int,
        rotation_label: str,
        allow_differential: bool = False,
    ) -> None:
        LOG.info("=== %s backup ===", period.capitalize())
        use_diff = allow_differential and self._cfg.differential and not self._cfg.encrypt
        prefix = f"{period}_"
        ext = ".sql"
        suf = self._comp.suffix

        if self._cfg.use_separate_dirs:
            for db in dbs:
                db_dir = self._cfg.backup_dir / period / db
                db_dir.mkdir(exist_ok=True)
                if period != "daily" and self._already_done_today(
                        f"{period}/{db}/{prefix}{db}_{self.date_stamp}_*{midfix}{ext}{suf}"):
                    LOG.info("Skipping %s/%s (already done today)", period, db)
                    continue
                self._dump_one(db_dir, prefix, midfix, ext, suf,
                               rotation, rotation_label, [db], use_diff)
        else:
            db_dir = self._cfg.backup_dir / period
            if period != "daily" and self._already_done_today(
                    f"{period}/{prefix}all-databases_{self.date_stamp}_*{midfix}{ext}{suf}"):
                LOG.info("Skipping %s/all-databases (already done today)", period)
                return
            self._dump_one(db_dir, prefix, midfix, ext, suf,
                           rotation, rotation_label, dbs, use_diff, multi=True)

    def _dump_one(
        self,
        directory: Path,
        prefix: str,
        midfix: str,
        ext: str,
        suf: str,
        rotation: int,
        rotation_label: str,
        databases: list[str],
        use_diff: bool,
        multi: bool = False,
    ) -> None:
        name = "all-databases" if multi else databases[0]
        fname_base = f"{prefix}{name}_{self.timestamp}{midfix}{ext}"

        if use_diff:
            self._dump_differential(directory, fname_base, suf, rotation, databases, name)
        else:
            dest = directory / (fname_base + suf)
            rc = self._db.dump_databases(databases, dest)
            if rc == 0:
                LOG.info("Backed up [%s/%s] -> %s", rotation_label, name, dest.name)
                self._dirs.rotate(directory, rotation)
                self._files.append(self._postprocess(dest))
            else:
                LOG.error("Dump failed for %s", name)

    # ── Differential backup ───────────────────────────────────────────────────

    def _dump_differential(
        self,
        directory: Path,
        fname_base: str,
        suf: str,
        rotation: int,
        databases: list[str],
        db_name: str,
    ) -> None:
        manifest = Manifest(directory / "Manifest")
        manifest.parse()
        master = manifest.latest_master(db_name)

        # Create a new master on the weekly day, or when no master exists yet
        need_master = master is None or (
            self._do_weekly() and self.date_stamp not in master.filename.name
        )

        # Allocate a unique filename via tempfile (fixes the original bash bug where
        # UID extraction used "${uid:-8:8}" — an invalid slice that always returned "").
        # The prefix strips ".sql" so the mkstemp suffix + final extension are not
        # split by a ".sql" in the middle of the name.
        fd, tmp = tempfile.mkstemp(
            dir=directory,
            prefix=fname_base.replace(".sql", "_"),
            suffix=".sql" + suf,
        )
        os.close(fd)
        out = Path(tmp)

        if need_master:
            rc = self._db.dump_databases(databases, out)
            if rc != 0:
                out.unlink(missing_ok=True)
                LOG.error("Master dump failed for %s", db_name)
                return
            manifest.add(out, "0", db_name)
            self._dirs.hardlink_to_latest(out)
            self._files.append(out)
            LOG.info("Master backup: %s", out.name)
        else:
            assert master is not None
            # mkstemp always creates a file; in the differential path the diff gets
            # its own derived name below, so delete the placeholder first.
            out.unlink(missing_ok=True)

            try:
                current_sql = self._db.dump_databases_raw(databases)
            except RuntimeError as exc:
                LOG.error("Failed to get current dump for diff: %s", exc)
                return

            master_proc = self._comp.decompress_stdout(master.filename)
            master_sql, _ = master_proc.communicate()

            diff_text = "".join(difflib.unified_diff(
                master_sql.decode(errors="replace").splitlines(keepends=True),
                current_sql.decode(errors="replace").splitlines(keepends=True),
                fromfile="master.sql",
                tofile="current.sql",
            )).encode()

            # Derive the diff filename from the master allocation by swapping the
            # extension while preserving the unique 8-char mkstemp ID embedded in
            # the filename stem.
            diff_dest = Path(str(out).replace(".sql" + suf, ".diff" + suf))

            if self._cfg.dryrun:
                LOG.info("[dryrun] would write diff to %s", diff_dest)
                return

            compress_cmd = self._comp.compress_cmd()
            if compress_cmd:
                with diff_dest.open("wb") as fh:
                    proc = subprocess.Popen(
                        compress_cmd,
                        stdin=subprocess.PIPE,
                        stdout=fh,
                    )
                    proc.communicate(diff_text)
                    rc = proc.returncode or 0
            else:
                diff_dest.write_bytes(diff_text)
                rc = 0

            if rc == 0:
                manifest.add(diff_dest, master.diff_id, db_name)
                self._dirs.hardlink_to_latest(diff_dest)
                # Also hardlink the master alongside the diff so latest/ contains
                # everything needed for a full reconstruction without hunting dated dirs.
                self._dirs.hardlink_to_latest(master.filename)
                self._files.append(diff_dest)
                LOG.info("Differential backup: %s (parent: %s)",
                         diff_dest.name, master.filename.name)
            else:
                LOG.error("Failed to write differential backup for %s", db_name)

        self._dirs.rotate(directory, rotation)

    # ── Latest folder filename cleaning ───────────────────────────────────────

    def _clean_latest_filenames(self) -> None:
        pat = re.compile(
            r"_\d{4}-\d{2}-\d{2}_\d{2}h\d{2}m_"
            r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
            r"January|February|March|April|May|June|July|August|September|"
            r"October|November|December|\d{1,2})"
        )
        for f in (self._cfg.backup_dir / "latest").iterdir():
            clean = pat.sub("", f.name)
            if clean != f.name:
                f.rename(f.parent / clean)
