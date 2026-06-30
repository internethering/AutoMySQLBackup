"""DiffRecovery — reconstructs a full SQL backup from a master + differential file."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

from .compression import CompressionHandler

LOG = logging.getLogger(__name__)


class DiffRecovery:
    def __init__(self, comp: CompressionHandler) -> None:
        self._comp = comp

    def apply(self, master: Path, diff: Path) -> Optional[Path]:
        """Apply diff to master using patch(1), producing a full SQL file.

        Temporary decompressed copies are cleaned up afterwards.
        Returns the output path on success, None on failure.
        """
        master_dec = self._decompress_to_tmp(master)
        diff_dec = self._decompress_to_tmp(diff)

        if master_dec is None or diff_dec is None:
            LOG.error("Could not decompress input files")
            return None

        # diff_dec has the .diff extension; strip it and add .sql for the output.
        # Two calls are needed because Path.with_suffix replaces only the last suffix.
        out = diff_dec.with_suffix("").with_suffix(".sql")
        try:
            result = subprocess.run(
                ["patch", str(master_dec), str(diff_dec), "-o", str(out)],
                capture_output=True,
            )
        finally:
            # Clean up temp decompressed copies even if patch fails.
            # Guard against unlinking the original when it was not compressed
            # (_decompress_to_tmp returns path unchanged for uncompressed files).
            if master_dec != master:
                master_dec.unlink(missing_ok=True)
            if diff_dec != diff:
                diff_dec.unlink(missing_ok=True)

        if result.returncode != 0:
            LOG.error("patch failed: %s", result.stderr.decode())
            return None

        LOG.info("Reconstructed full backup: %s", out)
        return out

    def _decompress_to_tmp(self, path: Path) -> Optional[Path]:
        """If path is compressed, decompress to a sibling file and return its path.

        Returns path unchanged if it is not compressed.
        Returns None if decompression fails.
        """
        if path.suffix not in (".gz", ".bz2"):
            return path
        out = path.with_suffix("")
        proc = self._comp.decompress_stdout(path)
        data, _ = proc.communicate()
        # gzip exits with rc=1 for warnings (e.g., trailing garbage) but still
        # produces correct output; only rc≥2 signals a real decompression failure.
        if proc.returncode and proc.returncode > 1:
            return None
        out.write_bytes(data)
        return out
