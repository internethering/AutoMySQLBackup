"""AutoMySQLBackup — Automated MySQL/MariaDB backup tool."""

from ._version import __version__
from .auth import AuthContext
from .cli import AutoMySQLBackup, main
from .compression import CompressionHandler
from .config import Config
from .database import DatabaseOps
from .directory import BackupDirectory
from .encryption import EncryptionHandler
from .interactive import InteractiveManager
from .manifest import Manifest, ManifestEntry
from .notifier import Notifier
from .orchestrator import BackupOrchestrator
from .recovery import DiffRecovery

__all__ = [
    "AutoMySQLBackup",
    "AuthContext",
    "BackupDirectory",
    "BackupOrchestrator",
    "CompressionHandler",
    "Config",
    "DatabaseOps",
    "DiffRecovery",
    "EncryptionHandler",
    "InteractiveManager",
    "Manifest",
    "ManifestEntry",
    "Notifier",
    "__version__",
    "main",
]
