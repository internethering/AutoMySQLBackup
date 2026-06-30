"""Encryption handler — AES-256-CBC via openssl."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

from .config import Config

LOG = logging.getLogger(__name__)


class EncryptionHandler:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def encrypt(self, path: Path) -> Optional[Path]:
        """Encrypt path with AES-256-CBC; remove plaintext on success.

        Returns the .enc path on success, or None when encryption is disabled.
        Raises RuntimeError if openssl fails — never silently falls back to
        plaintext when encryption was explicitly requested.
        """
        if not self._cfg.encrypt:
            return None

        out = Path(str(path) + ".enc")
        if self._cfg.dryrun:
            LOG.info("[dryrun] openssl enc -aes-256-cbc -e -in %s -out %s", path, out)
            return out

        result = subprocess.run(
            [
                "openssl", "enc", "-aes-256-cbc", "-e",
                "-in", str(path),
                "-out", str(out),
                "-pass", f"pass:{self._cfg.encrypt_password}",
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            # Raise rather than return None so the caller cannot silently use
            # the unencrypted plaintext when encryption was explicitly requested.
            raise RuntimeError(
                f"Encryption failed for {path}: {result.stderr.decode().strip()}"
            )

        try:
            path.unlink()
        except OSError as exc:
            LOG.error("Encrypted %s but could not remove plaintext: %s", path, exc)

        return out
