"""DatabaseOps — wraps all subprocess calls to mysql, mysqldump, and mysqlshow."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .auth import AuthContext
from .compression import CompressionHandler
from .config import Config

LOG = logging.getLogger(__name__)


class DatabaseOps:
    def __init__(self, cfg: Config, auth: AuthContext, comp: CompressionHandler) -> None:
        self._cfg = cfg
        self._auth = auth
        self._comp = comp

    # ── Argument builders ─────────────────────────────────────────────────────

    def _base_args(self) -> list[str]:
        return self._auth.common_args() + self._auth.ssl_args()

    def _dump_args(self) -> list[str]:
        cfg = self._cfg
        args = self._base_args()
        # --opt is a shorthand bundle: --add-drop-table --add-locks --create-options
        # --disable-keys --extended-insert --lock-tables --quick --set-charset
        args += ["--quote-names", "--opt"]
        if cfg.commcomp:
            args.append("--compress")
        if cfg.max_allowed_packet:
            args.append(f"--max_allowed_packet={cfg.max_allowed_packet}")
        if cfg.single_transaction:
            args.append("--single-transaction")
        if cfg.master_data in (1, 2):
            args.append(f"--master-data={cfg.master_data}")
        if cfg.use_separate_dirs:
            # --no-create-db: omit CREATE DATABASE (each db lives in its own file,
            # so the CREATE DATABASE statement would be redundant and confusing).
            # --databases: include USE + CREATE DATABASE, needed when a single dump
            # contains multiple databases (combined or create_database override).
            args.append("--no-create-db" if not cfg.create_database else "--databases")
        else:
            args.append("--databases")
        for tbl in cfg.table_exclude:
            args.append(f"--ignore-table={tbl}")
        return args

    def _schema_args(self) -> list[str]:
        cfg = self._cfg
        args = self._base_args()
        args += ["--all-databases", "--routines", "--no-data"]
        if cfg.commcomp:
            args.append("--compress")
        if cfg.max_allowed_packet:
            args.append(f"--max_allowed_packet={cfg.max_allowed_packet}")
        if cfg.single_transaction:
            args.append("--single-transaction")
        return args

    def _status_args(self) -> list[str]:
        args = self._base_args()
        args.append("--status")
        if self._cfg.commcomp:
            args.append("--compress")
        return args

    # ── Public operations ─────────────────────────────────────────────────────

    def list_databases(self) -> list[str]:
        """Return all database names minus any in cfg.db_exclude."""
        cmd = (
            [self._cfg.mysql]
            + self._base_args()
            # --batch: suppress ASCII table borders; --skip-column-names: drop the
            # "Database" header row.  Without both, output parsing would be fragile.
            + ["--batch", "--skip-column-names", "-e", "SHOW DATABASES"]
        )
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Could not list databases: {result.stderr.strip()}")
        dbs = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        for excl in self._cfg.db_exclude:
            dbs = [d for d in dbs if d != excl]
        return dbs

    def expand_table_wildcards(self) -> None:
        """Replace wildcard entries in cfg.table_exclude with concrete names."""
        expanded: list[str] = []
        for entry in self._cfg.table_exclude:
            if "." not in entry:
                LOG.warning("Malformed table_exclude entry (no dot): %s — ignored", entry)
                continue
            db, tbl = entry.split(".", 1)
            if "*" not in tbl:
                expanded.append(entry)
                continue
            like = tbl.replace("*", "%")
            cmd = (
                [self._cfg.mysql]
                + self._base_args()
                + [
                    "--batch", "--skip-column-names", "-e",
                    f"SELECT table_name FROM information_schema.tables "
                    f"WHERE table_schema='{db}' AND table_name LIKE '{like}';",
                ]
            )
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                LOG.warning("Could not expand wildcard %s: %s", entry, r.stderr.strip())
                expanded.append(entry)  # keep original so nothing is silently dropped
                continue
            expanded += [f"{db}.{t.strip()}" for t in r.stdout.splitlines() if t.strip()]
        self._cfg.table_exclude = expanded

    def dump_databases(self, databases: list[str], dest: Path) -> int:
        """Dump one or more databases into dest (compressed). Returns exit code."""
        cmd = [self._cfg.mysql_dump] + self._dump_args() + databases
        if self._cfg.dryrun:
            LOG.info("[dryrun] %s | compress > %s", " ".join(cmd), dest)
            return 0
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self._comp.pipe_compress(proc, dest)

    def dump_databases_raw(self, databases: list[str]) -> bytes:
        """Return uncompressed mysqldump output as bytes (used for diff generation).

        Loads the entire dump into memory — only call this for differential backups
        where the SQL text is needed for difflib comparison.  Use dump_databases()
        for all other paths to keep memory usage flat.
        """
        cmd = [self._cfg.mysql_dump] + self._dump_args() + databases
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"mysqldump failed: {r.stderr.decode()}")
        return r.stdout

    def dump_schema(self, dest: Path) -> int:
        """Dump the full schema (all databases, routines, no data) into dest."""
        cmd = [self._cfg.mysql_dump] + self._schema_args()
        if self._cfg.dryrun:
            LOG.info("[dryrun] %s | compress > %s", " ".join(cmd), dest)
            return 0
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self._comp.pipe_compress(proc, dest)

    def dump_status(self, dest: Path) -> int:
        """Dump mysqlshow --status output into dest."""
        cmd = [self._cfg.mysql_show] + self._status_args()
        if self._cfg.dryrun:
            LOG.info("[dryrun] %s | compress > %s", " ".join(cmd), dest)
            return 0
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self._comp.pipe_compress(proc, dest)
