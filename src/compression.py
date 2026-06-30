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

    def pipe_compress(self, source: subprocess.Popen, dest: Path) -> int:
        """Pipe source.stdout through the compressor into dest.

        Returns the worst non-zero exit code from either process, or 0.
        """
        cmd = self.compress_cmd()
        if cmd is None:
            with dest.open("wb") as f:
                out, _ = source.communicate()
                f.write(out)
            return source.returncode or 0
        with dest.open("wb") as f:
            comp = subprocess.Popen(cmd, stdin=source.stdout, stdout=f)
            # Closing our reference to source.stdout is required so the compressor
            # receives EOF when the source process exits.  Without this the
            # compressor blocks indefinitely waiting for more input.
            source.stdout.close()  # type: ignore[union-attr]
            comp.communicate()
            source.wait()
        return max(source.returncode or 0, comp.returncode or 0)

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
        return subprocess.Popen(cmd, stdin=path.open("rb"), stdout=subprocess.PIPE)

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

