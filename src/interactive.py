"""InteractiveManager — TUI for browsing and managing differential backup chains."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .compression import CompressionHandler
from .config import Config
from .manifest import Manifest
from .recovery import DiffRecovery

LOG = logging.getLogger(__name__)


class InteractiveManager:
    def __init__(self, cfg: Config, comp: CompressionHandler) -> None:
        self._cfg = cfg
        self._recovery = DiffRecovery(comp)

    def run(self) -> None:
        """Launch the interactive menu for managing differential backup chains."""
        manifests = list(self._cfg.backup_dir.rglob("Manifest"))
        if not manifests:
            print("No manifest files found.")
            return

        # Group manifest files by database name (parent directory)
        db_map: dict[str, list[Path]] = {}
        for m in manifests:
            db_map.setdefault(m.parent.name, []).append(m)

        selected_dbs = self._select("Select databases", list(db_map))
        if not selected_dbs:
            return

        # (diff_path, master_path, manifest_path)
        selected: list[tuple[Path, Path, Path]] = []

        for db in selected_dbs:
            for mpath in db_map[db]:
                manifest = Manifest(mpath)
                manifest.parse()
                diffs = manifest.diffs_for_db(db)
                if not diffs:
                    continue
                chosen = self._select(
                    f"Differential backups in {mpath}",
                    [str(e.filename) for e in diffs],
                )
                for fname in chosen:
                    entry = next((e for e in diffs if str(e.filename) == fname), None)
                    if entry is None:
                        continue
                    master = manifest.latest_master(db)
                    if master is None:
                        LOG.warning("No master backup found for db=%s", db)
                        continue
                    selected.append((entry.filename, master.filename, mpath))

        if not selected:
            print("Nothing selected.")
            return

        print("\nSelected:")
        for diff, master, _ in selected:
            print(f"  diff:   {diff}")
            print(f"  master: {master}")

        actions = self._select(
            "Actions",
            [
                "Convert diff to full backup",
                "Remove diff files from disk and Manifest",
            ],
        )
        for action in actions:
            if action.startswith("Convert"):
                for diff, master, _ in selected:
                    self._recovery.apply(master, diff)
            elif action.startswith("Remove"):
                for diff, _, mpath in selected:
                    Manifest(mpath).remove(diff)
                    diff.unlink(missing_ok=True)
                    print(f"Removed {diff}")

    # ── Selection helper ──────────────────────────────────────────────────────

    @staticmethod
    def _select(prompt: str, options: list[str]) -> list[str]:
        """Display a numbered menu and return the user's chosen items.

        Supports: single number, comma-separated list, A-B ranges, * for all.
        """
        if not options:
            return []
        print(f"\n── {prompt} ──")
        for i, opt in enumerate(options):
            print(f"  {i}) {opt}")
        print(f"  {len(options)}) ALL / DONE")
        raw = input("#? ").strip()
        if not raw or raw in ("*", str(len(options))):
            return list(options)
        result: list[str] = []
        for part in raw.split(","):
            part = part.strip()
            if re.match(r"^\d+-\d+$", part):
                a, b = part.split("-")
                result += options[int(a): int(b) + 1]
            elif part.isdigit() and int(part) < len(options):
                result.append(options[int(part)])
        return result
