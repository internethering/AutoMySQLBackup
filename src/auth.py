"""AuthContext — secure credential passing via a temporary my.cnf file."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from .config import Config


def _quote_option(value: str) -> str:
    """Quote a value for a MySQL option file.

    Unquoted, a "#" starts a comment and leading/trailing blanks are stripped,
    so passwords containing them would be silently truncated. Inside double
    quotes the client library understands backslash escapes for \\ and \".
    """
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{escaped}"'


class AuthContext:
    """Context manager that writes credentials to a 0600 temp file and passes
    it to MySQL via --defaults-extra-file, keeping the password out of argv.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._cnf: Optional[Path] = None

    def __enter__(self) -> AuthContext:
        cfg = self._cfg
        if not cfg.encrypted_login and cfg.password:
            # mkstemp creates the file with mode 0600 on Linux, but we call
            # chmod explicitly for portability and defense in depth.
            fd, tmp = tempfile.mkstemp(suffix=".cnf", prefix=".amysqlbkp_")
            self._cnf = Path(tmp)
            os.chmod(tmp, 0o600)
            # os.fdopen takes ownership of fd and closes it on exit, preventing
            # a descriptor leak from the mkstemp fd.
            with os.fdopen(fd, "w") as f:
                f.write(f"[client]\npassword={_quote_option(cfg.password)}\n")
        return self

    def __exit__(self, *_: object) -> None:
        if self._cnf:
            self._cnf.unlink(missing_ok=True)
            self._cnf = None

    def common_args(self) -> list[str]:
        """Auth arguments shared by mysql, mysqldump, and mysqlshow.

        Must only be called inside the `with AuthContext(...)` block — outside it,
        self._cnf is None and the password will not be passed to the subprocess.
        """
        cfg = self._cfg
        if cfg.encrypted_login:
            return [f"--login-path={cfg.login_path}"]
        args: list[str] = []
        if self._cnf:
            args.append(f"--defaults-extra-file={self._cnf}")
        args += [
            f"--user={cfg.username}",
            f"--host={cfg.host}",
            f"--port={cfg.port}",
        ]
        if cfg.socket:
            args.append(f"--socket={cfg.socket}")
        return args

    def ssl_args(self) -> list[str]:
        if not self._cfg.usessl:
            return []
        return ["--ssl-mode=PREFERRED"] if self._cfg.mysql8 else ["--ssl"]
