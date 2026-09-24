"""DatabaseOps — wraps all subprocess calls to mysql, mysqldump, and mysqlshow."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
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
            # "_" and "%" are LIKE wildcards themselves; escape them so only "*"
            # matches arbitrary text (cache_* must not match "cacheXfoo").
            # A literal backslash needs four: one level for the SQL string
            # literal, one for the LIKE escape character.
            like = (tbl.replace("\\", "\\\\\\\\").replace("%", "\\%")
                    .replace("_", "\\_").replace("*", "%").replace("'", "''"))
            schema = db.replace("\\", "\\\\").replace("'", "''")
            cmd = (
                [self._cfg.mysql]
                + self._base_args()
                + [
                    "--batch", "--skip-column-names", "-e",
                    f"SELECT table_name FROM information_schema.tables "
                    f"WHERE table_schema='{schema}' AND table_name LIKE '{like}';",
                ]
            )
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                LOG.warning("Could not expand wildcard %s: %s", entry, r.stderr.strip())
                expanded.append(entry)  # keep original so nothing is silently dropped
                continue
            expanded += [f"{db}.{t.strip()}" for t in r.stdout.splitlines() if t.strip()]
        self._cfg.table_exclude = expanded

    def dump_databases(self, databases: list[str], dest: Path, compress: bool = True) -> int:
        """Dump one or more databases into dest. Returns exit code.

        compress=False writes plain SQL (used as diff input for differential
        backups).
        """
        cmd = [self._cfg.mysql_dump] + self._dump_args() + databases
        return self._run_dump(cmd, dest, compress)

    def dump_schema(self, dest: Path) -> int:
        """Dump the full schema (all databases, routines, no data) into dest."""
        return self._run_dump([self._cfg.mysql_dump] + self._schema_args(), dest)

    def dump_status(self, dest: Path) -> int:
        """Dump mysqlshow --status output into dest."""
        return self._run_dump([self._cfg.mysql_show] + self._status_args(), dest)

    def _run_dump(self, cmd: list[str], dest: Path, compress: bool = True) -> int:
        """Run cmd and write its (compressed) stdout to dest atomically.

        Output goes to "<dest>.part" first and is renamed only on success, so a
        failed or interrupted dump never leaves a file that the idempotency
        check would mistake for a finished backup.
        """
        if self._cfg.dryrun:
            LOG.info("[dryrun] %s | compress > %s", " ".join(cmd), dest)
            return 0
        part = dest.with_name(dest.name + ".part")
        # stderr goes to a temp file rather than a pipe: nobody reads the pipe
        # while the dump runs, and a full pipe buffer would hang mysqldump.
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
            if compress:
                rc = self._comp.pipe_compress(proc, part)
            else:
                with part.open("wb") as f:
                    shutil.copyfileobj(proc.stdout, f, 1024 * 1024)  # type: ignore[arg-type]
                proc.stdout.close()  # type: ignore[union-attr]
                rc = proc.wait()
            err.seek(0)
            stderr = err.read().decode(errors="replace").strip()
        tool = Path(cmd[0]).name
        if rc != 0:
            part.unlink(missing_ok=True)
            LOG.error("%s failed (exit %d)%s", tool, rc, f": {stderr}" if stderr else "")
            return rc
        if stderr:
            LOG.warning("%s: %s", tool, stderr)
        part.replace(dest)
        return 0
