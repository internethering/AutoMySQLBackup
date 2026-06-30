"""Notifier — sends backup results via stdout, log file, or email."""

from __future__ import annotations

import datetime
import logging
import subprocess
from pathlib import Path

from .config import Config

LOG = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def send(self, log_text: str, error_text: str, files: list[Path]) -> None:
        """Dispatch notification according to cfg.mailcontent.

        Modes:
          stdout — print to terminal (default)
          log    — email the log; email errors separately if any
          quiet  — email only when there are errors
          files  — email log with backup files as attachments (mutt or uuencode)
        """
        host = self._cfg.host_label
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%Hh%Mm")
        has_errors = bool(error_text.strip())
        prefix = "ERRORS REPORTED: " if has_errors else ""
        subject = f"{prefix}MySQL Backup Log for {host} - {ts}"

        mode = self._cfg.mailcontent
        if mode == "stdout":
            print(log_text)
            if has_errors:
                print("\n###### WARNING ######")
                print("Errors reported during AutoMySQLBackup execution.")
                print(error_text)
        elif mode == "log":
            self._mail(subject, log_text)
            if has_errors:
                self._mail(f"ERROR LOG: MySQL Backup for {host} - {ts}", error_text)
        elif mode == "quiet":
            if has_errors:
                self._mail(subject, error_text)
        elif mode == "files":
            existing = [f for f in files if f.exists()]
            if self._cfg.mail_uuencoded:
                self._mail(subject, log_text)
            else:
                self._mutt(subject, log_text, existing)

    def _mail(self, subject: str, body: str) -> None:
        proc = subprocess.Popen(
            ["mail", "-s", subject, self._cfg.mail_address],
            stdin=subprocess.PIPE,
        )
        proc.communicate(body.encode())

    def _mutt(self, subject: str, body: str, attachments: list[Path]) -> None:
        att_args: list[str] = []
        for a in attachments:
            att_args += ["-a", str(a)]
        proc = subprocess.Popen(
            ["mutt", "-s", subject] + att_args + ["--", self._cfg.mail_address],
            stdin=subprocess.PIPE,
        )
        proc.communicate(body.encode())
