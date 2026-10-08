from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "usr/share/calamares-advanced/modules"
SCRIPT = ROOT / "etc/calamares/scripts/configure-selected-desktop"


class DesktopInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = yaml.safe_load((MODULES / "netinstall.yaml").read_text())
        cls.chooser = yaml.safe_load((MODULES / "packagechooser_desktop.conf").read_text())
        cls.desktops = next(group for group in cls.inventory if group.get("name") == "Desktop Environments")
        cls.base = next(group for group in cls.inventory if group.get("name", "").startswith("Base-devel"))

    def test_all_visible_desktop_groups_have_a_working_chooser_mapping(self) -> None:
        visible = {item["name"] for item in self.desktops["subgroups"] if not item.get("hidden", False)}
        choices = {item["id"] for item in self.chooser["items"] if item["id"]}
        self.assertEqual(visible, choices)
        with tempfile.TemporaryDirectory() as tmpdir:
            script_dir = Path(tmpdir)
            wrapper = script_dir / SCRIPT.name
            shutil.copy2(SCRIPT, wrapper)
            log = script_dir / "calls.log"
            for name in ("configure-display-manager", "activate-catdot-profile"):
                helper = script_dir / name
                helper.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n')
                helper.chmod(0o755)
            for choice in sorted(choices):
                log.unlink(missing_ok=True)
                result = subprocess.run(
                    [str(wrapper), choice, "alice"],
                    env={**os.environ, "CALL_LOG": str(log)},
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, f"{choice}: {result.stderr}")
                self.assertNotIn("unsupported desktop selection", result.stderr, choice)
                self.assertTrue(log.exists(), choice)

    def test_desktop_packages_do_not_duplicate_mandatory_base_packages(self) -> None:
        baseline = {pkg for group in self.base["subgroups"] for pkg in group.get("packages", [])}
        for desktop in self.desktops["subgroups"]:
            selected = {pkg for group in desktop.get("subgroups", []) for pkg in group.get("packages", [])}
            with self.subTest(desktop=desktop["name"]):
                self.assertEqual(selected & baseline, set())


if __name__ == "__main__":
    unittest.main()
