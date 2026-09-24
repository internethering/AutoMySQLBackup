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
        manifests = sorted(self._cfg.backup_dir.rglob("Manifest"))
        if not manifests:
            print("No manifest files found.")
            return

        # One menu entry per (manifest, database). Labelling by the manifest's
        # directory alone breaks with use_separate_dirs=false, where the
        # directory is "daily" but the database is "all-databases".
        chains: dict[str, tuple[Manifest, str]] = {}
        for mpath in manifests:
            manifest = Manifest(mpath)
            manifest.parse()
            rel = mpath.parent.relative_to(self._cfg.backup_dir)
            for db in manifest.all_dbs():
                chains[f"{rel}: {db}"] = (manifest, db)
        if not chains:
            print("No differential backups found.")
            return

        selected_chains = self._select("Select databases", list(chains))
        if not selected_chains:
            return

        # (diff_path, master_path, manifest_path)
        selected: list[tuple[Path, Path, Path]] = []

        for label in selected_chains:
            manifest, db = chains[label]
            diffs = manifest.diffs_for_db(db)
            if not diffs:
                continue
            chosen = self._select(
                f"Differential backups in {manifest.path}",
                [str(e.filename) for e in diffs],
            )
            for fname in chosen:
                entry = next((e for e in diffs if str(e.filename) == fname), None)
                if entry is None:
                    continue
                # The diff was created against a specific master; the newest
                # master may be a later one that this diff does not apply to.
                master = manifest.master_for(entry)
                if master is None:
                    LOG.warning("Master %s for %s is missing — cannot use this diff",
                                entry.rel_id, entry.filename)
                    continue
                selected.append((entry.filename, master.filename, manifest.path))

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
                    out = self._recovery.apply(master, diff)
                    if out:
                        print(f"Created {out}")
            elif action.startswith("Remove"):
                if not self._confirm(f"Permanently delete {len(selected)} diff file(s)?"):
                    print("Nothing removed.")
                    continue
                for diff, _, mpath in selected:
                    Manifest(mpath).remove(diff)
                    diff.unlink(missing_ok=True)
                    print(f"Removed {diff}")

    # ── Selection helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _confirm(question: str) -> bool:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")

    @staticmethod
    def _select(prompt: str, options: list[str]) -> list[str]:
        """Display a numbered menu and return the user's chosen items.

        Supports: single number, comma-separated list, A-B ranges, * for all.
        Empty input selects nothing, so pressing Enter by accident is harmless.
        """
        if not options:
            return []
        print(f"\n── {prompt} ──")
        for i, opt in enumerate(options):
            print(f"  {i}) {opt}")
        print(f"  {len(options)}) ALL")
        raw = input("#? (empty = none) ").strip()
        if not raw:
            return []
        if raw in ("*", str(len(options))):
            return list(options)
        result: list[str] = []
        for part in raw.split(","):
            part = part.strip()
            if re.match(r"^\d+-\d+$", part):
                a, b = (int(x) for x in part.split("-"))
                picked = options[min(a, b): max(a, b) + 1]
            elif part.isdigit() and int(part) < len(options):
                picked = [options[int(part)]]
            else:
                picked = []
            result += [p for p in picked if p not in result]
        return result
