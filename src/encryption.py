"""Encryption handler — AES-256-CBC via openssl."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

from .config import Config

LOG = logging.getLogger(__name__)

# PBKDF2 with a high iteration count instead of openssl's legacy single-round
# MD5 key derivation. Decrypt with the same options plus -d (see README).
OPENSSL_ARGS = ("-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000")


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
            LOG.info("[dryrun] openssl enc %s -in %s -out %s",
                     " ".join(OPENSSL_ARGS), path, out)
            return out
        if not self._cfg.encrypt_password:
            raise RuntimeError("Encryption is enabled but no password is configured")

        # The password is fed via stdin: "-pass pass:..." would expose it to
        # every local user through ps(1) and /proc/<pid>/cmdline.
        result = subprocess.run(
            ["openssl", "enc", *OPENSSL_ARGS, "-e",
             "-in", str(path), "-out", str(out), "-pass", "stdin"],
            input=(self._cfg.encrypt_password + "\n").encode(),
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
