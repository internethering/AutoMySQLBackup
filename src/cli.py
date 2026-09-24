"""AutoMySQLBackup — CLI entry point and top-level orchestration."""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ._version import __version__ as VERSION
from .auth import AuthContext
from .compression import CompressionHandler
from .config import Config
from .database import DatabaseOps
from .directory import BackupDirectory
from .encryption import EncryptionHandler
from .interactive import InteractiveManager
from .notifier import Notifier
from .orchestrator import BackupOrchestrator

LOG = logging.getLogger(__name__)
# Parent logger of every module in this package. Handlers attached here see
# records from orchestrator, database, etc.; handlers on LOG would not.
PKG_LOG = logging.getLogger(__name__.rpartition(".")[0] or __name__)


class _LogCapture(logging.Handler):
    """Collects log records so they can be forwarded to the Notifier at exit."""

    def __init__(self) -> None:
        super().__init__()
        self._all: list[str] = []
        self._errors: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self._all.append(msg)
        if record.levelno >= logging.ERROR:
            self._errors.append(msg)

    @property
    def log_text(self) -> str:
        return "\n".join(self._all)

    @property
    def error_text(self) -> str:
        return "\n".join(self._errors)


class AutoMySQLBackup:
    def main(self, argv: Optional[list[str]] = None) -> int:
        args = self._parse_args(argv)
        self._configure_logging(args)

        try:
            cfg = Config.load(extra=args.config)
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if args.dryrun:
            cfg.dryrun = True
        if args.debug:
            cfg.debug = True

        if args.list_manifests:
            return self._cmd_list(cfg)
        return self._cmd_backup(cfg)

    # ── Argument parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
        p = argparse.ArgumentParser(
            prog="automysqlbackup",
            description=f"AutoMySQLBackup v{VERSION} — Automated MySQL/MariaDB backup",
        )
        p.add_argument("-c", "--config", type=Path, metavar="FILE",
                       help="Optional config file (layered on top of global config)")
        p.add_argument("-b", "--backup", action="store_true",
                       help="Run backup (default when no mode flag is given)")
        p.add_argument("-l", "--list-manifests", action="store_true",
                       help="Interactive differential backup manager")
        p.add_argument("-n", "--dry-run", dest="dryrun", action="store_true",
                       help="Show what would be done without making changes")
        p.add_argument("-v", "--verbose", action="store_true")
        p.add_argument("-d", "--debug", action="store_true")
        return p.parse_args(argv)

    @staticmethod
    def _configure_logging(args: argparse.Namespace) -> None:
        level = logging.DEBUG if args.debug else (
            logging.INFO if args.verbose else logging.WARNING
        )
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # The console shows only what -v/-d asked for, but the package logger
        # passes INFO on so the emailed log is complete without -v.
        for handler in logging.getLogger().handlers:
            handler.setLevel(level)
        PKG_LOG.setLevel(min(level, logging.INFO))

    # ── Backup command ────────────────────────────────────────────────────────

    def _cmd_backup(self, cfg: Config) -> int:
        dirs = BackupDirectory(cfg)
        try:
            dirs.setup()
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        capture = _LogCapture()
        capture.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
        PKG_LOG.addHandler(capture)
        backup_files: list[Path] = []
        exit_code = 0

        try:
            self._check_deps(cfg)

            if cfg.prebackup:
                LOG.info("Running prebackup: %s", cfg.prebackup)
                subprocess.run(cfg.prebackup, shell=True, check=False)

            with AuthContext(cfg) as auth:
                comp = CompressionHandler(cfg)
                enc = EncryptionHandler(cfg)
                db = DatabaseOps(cfg, auth, comp)
                db.expand_table_wildcards()
                dirs.cleanup_latest()

                all_dbs = db.list_databases()
                if not all_dbs:
                    LOG.error("No databases found — check connection settings")
                    return 1

                daily_dbs = cfg.db_names or all_dbs
                monthly_dbs = cfg.db_month_names or all_dbs

                print("=" * 70)
                print(f"AutoMySQLBackup v{VERSION}")
                print(f"Host    : {cfg.host_label}")
                print(f"Daily   : {', '.join(daily_dbs)}")
                print(f"Monthly : {', '.join(monthly_dbs)}")
                print("=" * 70)

                orch = BackupOrchestrator(cfg, db, comp, enc, dirs)
                backup_files = orch.run(daily_dbs, monthly_dbs)

            if cfg.postbackup:
                LOG.info("Running postbackup: %s", cfg.postbackup)
                subprocess.run(cfg.postbackup, shell=True, check=False)

            total_bytes = sum(f.stat().st_size for f in backup_files if f.exists())
            print(f"\nBackup complete — {len(backup_files)} files, "
                  f"{total_bytes / 1024 / 1024:.1f} MB total")
            print(f"Location: {cfg.backup_dir}")

        except KeyboardInterrupt:
            LOG.error("Interrupted by user")
            exit_code = 130
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Unexpected error: %s", exc)
            exit_code = 1
        finally:
            PKG_LOG.removeHandler(capture)
            try:
                Notifier(cfg).send(capture.log_text, capture.error_text, backup_files)
            except Exception as exc:  # noqa: BLE001
                # Never let a notification problem mask the backup result.
                print(f"ERROR: notification failed: {exc}", file=sys.stderr)

        # Any error logged during the run (failed dump, rotation, encryption …)
        # must reach cron/systemd as a non-zero exit status.
        if exit_code == 0 and capture.error_text:
            exit_code = 1
        return exit_code

    # ── List / manage manifests command ───────────────────────────────────────

    def _cmd_list(self, cfg: Config) -> int:
        comp = CompressionHandler(cfg)
        InteractiveManager(cfg, comp).run()
        return 0

    # ── Dependency check ──────────────────────────────────────────────────────

    @staticmethod
    def _check_deps(cfg: Config) -> None:
        required = [cfg.mysql, cfg.mysql_dump]
        if cfg.dbstatus:
            required.append(cfg.mysql_show)
        missing = [t for t in required if not shutil.which(t) and not Path(t).is_file()]
        if missing:
            raise RuntimeError(f"Required tools not found: {', '.join(missing)}")
        if cfg.encrypt and not shutil.which("openssl"):
            raise RuntimeError("openssl not found but encryption is enabled")
        if cfg.differential and not cfg.encrypt and not shutil.which("diff"):
            raise RuntimeError("diff not found but differential backups are enabled")
        if cfg.mailcontent in ("log", "quiet", "files"):
            if not shutil.which("mail"):
                LOG.warning("mail command not found; the report will be printed "
                            "to stdout instead of emailed")
            if cfg.mailcontent == "files" and not cfg.mail_uuencoded:
                if not shutil.which("mutt"):
                    LOG.warning("mutt not found; falling back to plain mail "
                                "without attachments")


def main() -> None:
    sys.exit(AutoMySQLBackup().main())
