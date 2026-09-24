"""Notifier — sends backup results via stdout, log file, or email."""

from __future__ import annotations

import binascii
import datetime
import logging
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .config import Config

LOG = logging.getLogger(__name__)

# binascii.b2a_uu encodes at most 45 bytes per line.
_UU_LINE = 45


def uuencode(path: Path) -> str:
    """Return path's content as a uuencoded block (begin … end)."""
    lines = [f"begin 644 {path.name}"]
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_UU_LINE), b""):
            lines.append(binascii.b2a_uu(chunk, backtick=True).decode().rstrip("\n"))
    lines += ["`", "end"]
    return "\n".join(lines) + "\n"


class Notifier:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def send(self, log_text: str, error_text: str, files: list[Path]) -> None:
        """Dispatch notification according to cfg.mailcontent.

        Modes:
          stdout — print an error summary (the log itself is already on the console)
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
            # Log records were already written to the console by the logging
            # handler; repeating the whole log here would print everything twice.
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
            self._send_files(subject, log_text, [f for f in files if f.exists()])

    # ── Attachments ───────────────────────────────────────────────────────────

    def _send_files(self, subject: str, log_text: str, files: list[Path]) -> None:
        with tempfile.TemporaryDirectory(prefix="amysqlbkp_mail_") as tmp:
            batches, note = self._attachment_batches(files, Path(tmp))
            body = log_text + (f"\n\n{note}" if note else "")
            if not batches:
                self._mail(subject, body)
                return
            for i, batch in enumerate(batches, 1):
                subj = subject if len(batches) == 1 else f"{subject} (part {i}/{len(batches)})"
                text = body if i == 1 else f"Attachment part {i} of {len(batches)}."
                if self._cfg.mail_uuencoded:
                    self._mail(subj, text + "\n\n" + "".join(uuencode(f) for f in batch))
                else:
                    self._mutt(subj, text, batch)

    def _attachment_batches(
        self, files: list[Path], tmp: Path,
    ) -> tuple[list[list[Path]], str]:
        """Group files into mails that respect mail_maxattsize.

        With split_and_tar the files are packed into one tar archive that is
        split into chunks of at most mail_maxattsize kilobytes (reassemble
        with: cat backup.tar.part* > backup.tar). Without it, files that are
        too large are left out and listed in the returned note.
        """
        limit = self._cfg.mail_maxattsize * 1024
        if not files or limit <= 0 or sum(f.stat().st_size for f in files) <= limit:
            return ([files] if files else []), ""

        if self._cfg.mail_splitandtar:
            archive = tmp / "backup.tar"
            with tarfile.open(archive, "w") as tar:
                for f in files:
                    tar.add(f, arcname=f.name)
            parts: list[Path] = []
            with archive.open("rb") as src:
                for chunk in iter(lambda: src.read(limit), b""):
                    part = tmp / f"backup.tar.part{len(parts) + 1:03d}"
                    part.write_bytes(chunk)
                    parts.append(part)
            archive.unlink()
            note = ("Attachments were packed into backup.tar and split into "
                    f"{len(parts)} parts; reassemble with: cat backup.tar.part* > backup.tar")
            return [[p] for p in parts], note

        fitting = [f for f in files if f.stat().st_size <= limit]
        skipped = [f for f in files if f not in fitting]
        batches: list[list[Path]] = []
        current: list[Path] = []
        size = 0
        for f in fitting:
            fsize = f.stat().st_size
            if current and size + fsize > limit:
                batches.append(current)
                current, size = [], 0
            current.append(f)
            size += fsize
        if current:
            batches.append(current)
        note = ""
        if skipped:
            note = ("Not attached (larger than max_attachment_size):\n"
                    + "\n".join(f"  {f}" for f in skipped))
        return batches, note

    # ── Transports ────────────────────────────────────────────────────────────

    def _mail(self, subject: str, body: str) -> None:
        if not self._run(["mail", "-s", subject, self._cfg.mail_address], body):
            print(f"{subject}\n\n{body}")

    def _mutt(self, subject: str, body: str, attachments: list[Path]) -> None:
        if not shutil.which("mutt"):
            names = "\n".join(f"  {a}" for a in attachments)
            self._mail(subject, f"{body}\n\n(mutt not installed — files not attached)\n{names}")
            return
        att_args: list[str] = []
        for a in attachments:
            att_args += ["-a", str(a)]
        cmd = ["mutt", "-s", subject] + att_args + ["--", self._cfg.mail_address]
        if not self._run(cmd, body):
            print(f"{subject}\n\n{body}")

    @staticmethod
    def _run(cmd: list[str], body: str) -> bool:
        """Pipe body into cmd. Returns False if the mailer could not be used."""
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        except OSError as exc:
            LOG.error("Cannot send mail via %s: %s", cmd[0], exc)
            return False
        proc.communicate(body.encode())
        if proc.returncode:
            LOG.error("%s exited with status %d", cmd[0], proc.returncode)
            return False
        return True
