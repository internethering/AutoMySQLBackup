"""Tests for AuthContext — temp credential file, common_args, ssl_args."""

from __future__ import annotations

import unittest
from pathlib import Path

from automysqlbackup.auth import AuthContext
from automysqlbackup.config import Config


def _cfg(**kwargs) -> Config:
    cfg = Config()
    cfg.password = kwargs.get("password", "")
    cfg.encrypted_login = kwargs.get("encrypted_login", False)
    cfg.login_path = kwargs.get("login_path", "")
    cfg.username = kwargs.get("username", "backupuser")
    cfg.host = kwargs.get("host", "dbhost")
    cfg.port = kwargs.get("port", 3306)
    cfg.socket = kwargs.get("socket", "")
    cfg.usessl = kwargs.get("usessl", True)
    cfg.mysql8 = kwargs.get("mysql8", False)
    return cfg


class TestAuthContextTempFile(unittest.TestCase):
    def test_cnf_file_created_when_password_set(self):
        with AuthContext(_cfg(password="secret")) as auth:
            self.assertIsNotNone(auth._cnf)
            self.assertTrue(auth._cnf.exists())

    def test_cnf_file_contains_password(self):
        with AuthContext(_cfg(password="hunter2")) as auth:
            content = auth._cnf.read_text()
            self.assertIn("[client]", content)
            self.assertIn("password=hunter2", content)

    def test_cnf_file_mode_is_0600(self):
        with AuthContext(_cfg(password="secret")) as auth:
            mode = oct(auth._cnf.stat().st_mode)[-4:]
            self.assertEqual(mode, "0600")

    def test_cnf_file_deleted_on_exit(self):
        cnf_path: Path | None = None
        with AuthContext(_cfg(password="secret")) as auth:
            cnf_path = auth._cnf
        self.assertIsNotNone(cnf_path)
        self.assertFalse(cnf_path.exists())

    def test_no_cnf_file_when_password_empty(self):
        with AuthContext(_cfg(password="")) as auth:
            self.assertIsNone(auth._cnf)

    def test_no_cnf_file_when_encrypted_login(self):
        with AuthContext(_cfg(password="ignored", encrypted_login=True, login_path="loc")) as auth:
            self.assertIsNone(auth._cnf)

    def test_exit_is_idempotent_when_cnf_already_gone(self):
        auth = AuthContext(_cfg(password="secret"))
        auth.__enter__()
        auth._cnf.unlink()          # manually delete before __exit__
        auth.__exit__(None, None, None)   # must not raise


class TestCommonArgs(unittest.TestCase):
    def test_includes_defaults_extra_file_when_password_set(self):
        with AuthContext(_cfg(password="secret")) as auth:
            args = auth.common_args()
        self.assertTrue(any("--defaults-extra-file=" in a for a in args))

    def test_password_never_appears_in_args(self):
        with AuthContext(_cfg(password="top_secret")) as auth:
            args = auth.common_args()
        self.assertFalse(any("top_secret" in a for a in args))

    def test_includes_user_host_port(self):
        with AuthContext(_cfg(password="x", username="alice", host="remotehost", port=3307)) as auth:
            args = auth.common_args()
        self.assertIn("--user=alice", args)
        self.assertIn("--host=remotehost", args)
        self.assertIn("--port=3307", args)

    def test_includes_socket_when_set(self):
        with AuthContext(_cfg(password="x", socket="/run/mysql/mysql.sock")) as auth:
            args = auth.common_args()
        self.assertIn("--socket=/run/mysql/mysql.sock", args)

    def test_omits_socket_when_empty(self):
        with AuthContext(_cfg(password="x", socket="")) as auth:
            args = auth.common_args()
        self.assertFalse(any("--socket" in a for a in args))

    def test_encrypted_login_returns_login_path_only(self):
        with AuthContext(_cfg(encrypted_login=True, login_path="mylogin")) as auth:
            args = auth.common_args()
        self.assertEqual(args, ["--login-path=mylogin"])

    def test_no_cnf_arg_when_no_password(self):
        with AuthContext(_cfg(password="")) as auth:
            args = auth.common_args()
        self.assertFalse(any("--defaults-extra-file" in a for a in args))


class TestSslArgs(unittest.TestCase):
    def test_returns_ssl_flag_for_legacy_mysql(self):
        with AuthContext(_cfg(usessl=True, mysql8=False)) as auth:
            self.assertEqual(auth.ssl_args(), ["--ssl"])

    def test_returns_ssl_mode_preferred_for_mysql8(self):
        with AuthContext(_cfg(usessl=True, mysql8=True)) as auth:
            self.assertEqual(auth.ssl_args(), ["--ssl-mode=PREFERRED"])

    def test_returns_empty_when_ssl_disabled(self):
        with AuthContext(_cfg(usessl=False)) as auth:
            self.assertEqual(auth.ssl_args(), [])


if __name__ == "__main__":
    unittest.main()
