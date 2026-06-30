"""Configuration dataclass — loads and holds all runtime settings."""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger(__name__)


@dataclasses.dataclass
class Config:
    """All runtime configuration; loaded from a YAML config file with hardcoded defaults."""

    # Binaries
    mysql: str = "/usr/bin/mysql"
    mysql_dump: str = "/usr/bin/mysqldump"
    mysql_show: str = "/usr/bin/mysqlshow"
    mysql8: bool = False  # enables --ssl-mode=PREFERRED instead of --ssl

    # Authentication
    encrypted_login: bool = False
    login_path: str = ""
    username: str = "root"
    password: str = ""
    host: str = "localhost"
    host_friendly: str = ""
    socket: str = ""
    port: int = 3306

    # Paths
    backup_dir: Path = dataclasses.field(default_factory=lambda: Path("/var/backup/db"))

    # Multicore compression (pigz / pbzip2)
    multicore: bool = True
    multicore_threads: int = 2  # set to 0 for tool auto-detection

    # Schedule
    do_monthly: int = 1   # day-of-month (1–31); 0 disables monthly backups
    do_weekly: int = 5    # ISO weekday (1=Mon … 7=Sun); 0 disables weekly backups

    # Rotation (maximum file age in days)
    rotation_daily: int = 6
    rotation_weekly: int = 35
    rotation_monthly: int = 150

    # Dump options
    usessl: bool = True
    commcomp: bool = False
    create_database: bool = False
    use_separate_dirs: bool = True
    compression: str = "gzip"   # "gzip", "bzip2", or "" (none)
    latest: bool = False        # keep hardlinked copy in latest/
    latest_clean_filenames: bool = False
    max_allowed_packet: str = ""
    single_transaction: bool = False
    master_data: Optional[int] = None  # 1 or 2; None = disabled
    full_schema: bool = True
    dbstatus: bool = True
    differential: bool = False

    # Database / table selection
    db_names: list[str] = dataclasses.field(default_factory=list)
    db_month_names: list[str] = dataclasses.field(default_factory=list)
    db_exclude: list[str] = dataclasses.field(default_factory=lambda: ["information_schema"])
    table_exclude: list[str] = dataclasses.field(default_factory=list)

    # Local file backup
    backup_local_files: list[str] = dataclasses.field(default_factory=list)

    # Notification
    mailcontent: str = "stdout"   # "stdout", "log", "quiet", "files"
    mail_maxattsize: int = 4000   # kilobytes
    mail_splitandtar: bool = True
    mail_uuencoded: bool = False
    mail_address: str = "root"

    # Encryption (openssl AES-256-CBC)
    encrypt: bool = False
    encrypt_password: str = ""

    # Hooks (executed via shell)
    prebackup: str = ""
    postbackup: str = ""

    # Runtime flags
    dryrun: bool = False
    debug: bool = False

    # ── Private constants (not user-settable) ─────────────────────────────────
    # Stored as init=False dataclass fields rather than class variables so that
    # dataclasses.asdict() and type checkers see them without needing a separate
    # __post_init__; compare=False keeps them out of equality checks.

    _GLOBAL_CONFIG: Path = dataclasses.field(
        default=Path("/etc/automysqlbackup/automysqlbackup.yaml"),
        init=False, repr=False, compare=False,
    )

    # ── Class methods ─────────────────────────────────────────────────────────

    @classmethod
    def load(cls, extra: Optional[Path] = None) -> Config:
        """Create a Config from defaults, layering global then extra config file."""
        cfg = cls()
        for path in [cfg._GLOBAL_CONFIG, extra]:
            if path and Path(path).is_file():
                cfg._apply_yaml(Path(path))
                LOG.debug("Loaded config from %s", path)
        return cfg

    # ── Private helpers ───────────────────────────────────────────────────────

    def _apply_yaml(self, path: Path) -> None:
        try:
            import yaml
        except ImportError:
            raise RuntimeError(
                "PyYAML is required: pip install pyyaml"
            ) from None

        with path.open() as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            LOG.warning("Config file %s is empty or not a YAML mapping", path)
            return

        if "umask" in data:
            # umask is a top-level directive, not a backup setting.
            raw = data["umask"]
            os.umask(int(str(raw), 8) if isinstance(raw, str) else int(raw))

        def sec(name: str) -> dict[str, Any]:
            v = data.get(name)
            return v if isinstance(v, dict) else {}

        def apply(field: str, val: Any) -> None:
            # Only overwrite the default when the key was explicitly present in YAML.
            if val is not None:
                setattr(self, field, val)

        b = sec("binaries")
        apply("mysql",      b.get("mysql"))
        apply("mysql_dump", b.get("mysqldump"))
        apply("mysql_show", b.get("mysqlshow"))

        m = sec("mysql")
        apply("username",        m.get("username"))
        apply("host",            m.get("host"))
        apply("host_friendly",   m.get("host_friendly"))
        apply("port",            m.get("port"))
        apply("socket",          m.get("socket"))
        apply("usessl",          m.get("ssl"))
        apply("mysql8",          m.get("mysql8"))
        apply("encrypted_login", m.get("encrypted_login"))
        apply("login_path",      m.get("login_path"))
        # Treat explicit null/empty as an intentional blank password.
        if "password" in m:
            self.password = str(m["password"]) if m["password"] is not None else ""

        bk = sec("backup")
        if "dir" in bk:
            self.backup_dir = Path(bk["dir"])
        if "local_files" in bk:
            self.backup_local_files = list(bk["local_files"] or [])

        sc = sec("schedule")
        apply("do_monthly", sc.get("do_monthly"))
        apply("do_weekly",  sc.get("do_weekly"))

        rot = sec("rotation")
        apply("rotation_daily",   rot.get("daily"))
        apply("rotation_weekly",  rot.get("weekly"))
        apply("rotation_monthly", rot.get("monthly"))

        db = sec("databases")
        if "include" in db:
            self.db_names = list(db["include"] or [])
        if "monthly" in db:
            self.db_month_names = list(db["monthly"] or [])
        if "exclude" in db:
            self.db_exclude = list(db["exclude"] or [])

        tbl = sec("tables")
        if "exclude" in tbl:
            self.table_exclude = list(tbl["exclude"] or [])

        dp = sec("dump")
        apply("commcomp",          dp.get("commcomp"))
        apply("create_database",   dp.get("create_database"))
        apply("use_separate_dirs", dp.get("use_separate_dirs"))
        apply("single_transaction", dp.get("single_transaction"))
        apply("full_schema",       dp.get("full_schema"))
        apply("dbstatus",          dp.get("dbstatus"))
        apply("differential",      dp.get("differential"))
        if "max_allowed_packet" in dp:
            self.max_allowed_packet = str(dp["max_allowed_packet"]) if dp["max_allowed_packet"] else ""
        if "master_data" in dp:
            v = dp["master_data"]
            self.master_data = int(v) if v in (1, 2) else None

        comp = sec("compression")
        apply("compression",       comp.get("algorithm"))
        apply("multicore",         comp.get("multicore"))
        apply("multicore_threads", comp.get("threads"))

        lat = sec("latest")
        apply("latest",                lat.get("enabled"))
        apply("latest_clean_filenames", lat.get("clean_filenames"))

        enc = sec("encryption")
        apply("encrypt", enc.get("enabled"))
        if "password" in enc:
            self.encrypt_password = str(enc["password"]) if enc["password"] else ""

        notif = sec("notification")
        apply("mailcontent",      notif.get("content"))
        apply("mail_maxattsize",  notif.get("max_attachment_size"))
        apply("mail_splitandtar", notif.get("split_and_tar"))
        apply("mail_uuencoded",   notif.get("uuencoded"))
        apply("mail_address",     notif.get("address"))

        hooks = sec("hooks")
        apply("prebackup",  hooks.get("prebackup"))
        apply("postbackup", hooks.get("postbackup"))

        rt = sec("runtime")
        apply("dryrun", rt.get("dryrun"))
        apply("debug",  rt.get("debug"))

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def host_label(self) -> str:
        """Display name for the MySQL host (friendly name if set, otherwise host)."""
        return self.host_friendly or self.host

    @property
    def compression_suffix(self) -> str:
        return {"gzip": ".gz", "bzip2": ".bz2"}.get(self.compression, "")
