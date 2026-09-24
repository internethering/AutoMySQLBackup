"""Compression handler — gzip / bzip2 with optional multicore (pigz / pbzip2)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .config import Config

LOG = logging.getLogger(__name__)


class CompressionHandler:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._multicore_ok = self._probe_multicore()

    def _probe_multicore(self) -> bool:
        if not self._cfg.multicore:
            return False
        tool = {"gzip": "pigz", "bzip2": "pbzip2"}.get(self._cfg.compression)
        if tool and shutil.which(tool):
            return True
        if tool:
            LOG.warning("Multicore tool %s not found; falling back to single-core", tool)
        return False

    @property
    def suffix(self) -> str:
        return self._cfg.compression_suffix

    def pipe_compress(
        self,
        source: subprocess.Popen,
        dest: Path,
        source_ok: tuple[int, ...] = (0,),
    ) -> int:
        """Stream source.stdout through the compressor into dest.

        source_ok lists exit codes of the source process that count as success
        (diff(1) exits 1 when the inputs differ). Returns 0 on success,
        otherwise the worst non-zero exit code of either process.
        The caller must not connect source.stderr to a pipe it never reads;
        a full stderr pipe would block the source process forever.
        """
        cmd = self.compress_cmd()
        with dest.open("wb") as f:
            if cmd is None:
                # Stream instead of communicate(): a multi-GB dump must not be
                # held in memory just because compression is disabled.
                shutil.copyfileobj(source.stdout, f, 1024 * 1024)  # type: ignore[arg-type]
                source.stdout.close()  # type: ignore[union-attr]
                source.wait()
                comp_rc = 0
            else:
                comp = subprocess.Popen(cmd, stdin=source.stdout, stdout=f)
                # Closing our reference to source.stdout is required so the
                # compressor receives EOF when the source process exits.
                source.stdout.close()  # type: ignore[union-attr]
                comp.communicate()
                source.wait()
                comp_rc = comp.returncode or 0
        src_rc = source.returncode or 0
        if src_rc in source_ok:
            src_rc = 0
        return max(src_rc, comp_rc)

    def decompress_stdout(self, path: Path) -> subprocess.Popen:
        """Return a Popen whose stdout streams the decompressed content of path.

        Returns a streaming process rather than bytes so callers can pipe the
        output directly into another process without buffering the entire file.
        """
        suffixes = "".join(path.suffixes)
        if ".gz" in suffixes:
            cmd = (["pigz", "-dc"] if self._multicore_ok and shutil.which("pigz")
                   else ["gzip", "-dc"])
        elif ".bz2" in suffixes:
            cmd = (["pbzip2", "-dc"] if self._multicore_ok and shutil.which("pbzip2")
                   else ["bzip2", "-dc"])
        else:
            cmd = ["cat"]
        # The child inherits its own copy of the descriptor, so ours can be
        # closed right away instead of leaking until garbage collection.
        with path.open("rb") as fh:
            return subprocess.Popen(cmd, stdin=fh, stdout=subprocess.PIPE)

    def compress_cmd(self) -> Optional[list[str]]:
        """Return the compression command list, or None if compression is disabled."""
        cfg = self._cfg
        if cfg.compression == "gzip":
            if self._multicore_ok:
                cmd = ["pigz"]
                if cfg.multicore_threads:
                    cmd += [f"-p{cfg.multicore_threads}"]
                return cmd
            return ["gzip"]
        if cfg.compression == "bzip2":
            if self._multicore_ok:
                cmd = ["pbzip2"]
                if cfg.multicore_threads:
                    cmd += [f"-p{cfg.multicore_threads}"]
                return cmd
            return ["bzip2"]
        return None

